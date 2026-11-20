"""The gateway every model call can be sent through. In order, it:

1. answers from the cache: an identical prompt, or one whose embedding is close enough
2. checks the budget: past the soft limit everything goes to the cheapest model, past the hard
   limit the call is refused
3. routes: a complexity score for the prompt picks the cheap or the capable model
4. degrades: if the chosen model fails, the next one in the chain is tried
5. records what was spent and stores the reply
"""

import hashlib
import json
import math
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .durable import Fatal

SCHEMA = """
create table if not exists cache(exact text primary key, scope text, prompt text, reply text, vector blob, hits integer default 0);
create table if not exists spend(day text, tier text, calls integer, tokens integer, cost real, primary key (day, tier));
"""


class BudgetExceeded(Fatal):
    """The kill-switch. Fatal, so a durable run fails at once instead of retrying into the wall."""


@dataclass(frozen=True)
class Tier:
    name: str
    model: str
    price: float  # cost per 1,000 tokens, in whatever unit the budget uses


def entropy(text: str) -> float:
    """Shannon entropy of the prompt's words, in bits. Low for short or repetitive prompts, high
    for long ones with a varied vocabulary: a cheap stand-in for how much the prompt asks."""
    words = re.findall(r"\w+", text.lower())
    counts = Counter(words)
    return -sum(n / len(words) * math.log2(n / len(words)) for n in counts.values()) if words else 0.0


def complexity(prompt: str) -> float:
    """0 to 1. Entropy carries most of the score; code and a long list of constraints add to it."""
    code = len(re.findall(r"[{}()\[\]=<>]|\bdef\b|\bclass\b", prompt))
    constraints = len(re.findall(r"\b(must|exactly|only|never|unless|except|at most|at least)\b", prompt.lower()))
    score = 0.7 * (entropy(prompt) - 3.5) / 3.5 + 0.15 * min(1, code / 20) + 0.15 * min(1, constraints / 4)
    return max(0.0, min(1.0, score))


class Gateway:
    def __init__(
        self,
        tiers: list[Tier],  # cheapest first
        complete: Callable[..., tuple[str, int]],  # (system, user, max_tokens, schema, model) -> (text, tokens used)
        path: Path | str = "data/gateway.db",
        embed: Callable[[str], np.ndarray] | None = None,  # None turns the semantic cache off
        route_above: float = 0.5,  # complexity at or above this goes to the most capable tier
        similar: float = 0.97,  # cosine similarity that counts as the same prompt
        soft_limit: float = math.inf,  # daily spend past which everything uses the cheapest tier
        hard_limit: float = math.inf,  # daily spend past which calls are refused
        today: Callable[[], str] = lambda: time.strftime("%Y-%m-%d"),
    ) -> None:
        self.tiers, self.complete, self.embed = tiers, complete, embed
        self.route_above, self.similar = route_above, similar
        self.soft_limit, self.hard_limit, self.today = soft_limit, hard_limit, today
        self.stats: Counter = Counter()
        self.db = sqlite3.connect(path, timeout=30)
        self.db.executescript(SCHEMA)

    def spent(self) -> float:
        return self.db.execute("select coalesce(sum(cost), 0) from spend where day = ?", (self.today(),)).fetchone()[0]

    def _vector(self, text: str) -> np.ndarray:
        vector = np.asarray(self.embed(text), dtype=np.float32)
        return vector / np.linalg.norm(vector)

    def _cached(self, exact: str, scope: str, user: str) -> tuple[str | None, np.ndarray | None]:
        row = self.db.execute("select reply from cache where exact = ?", (exact,)).fetchone()
        if row:
            self.stats["cache_exact"] += 1
            return row[0], None
        if self.embed is None:
            return None, None
        vector = self._vector(user)
        # ponytail: compares against every cached prompt in the scope. Index the vectors when
        # a scope holds more than a few thousand.
        best, reply = 0.0, None
        for other, cached in self.db.execute("select vector, reply from cache where scope = ? and vector is not null", (scope,)):
            score = float(np.frombuffer(other, dtype=np.float32) @ vector)
            if score > best:
                best, reply = score, cached
        if best >= self.similar:
            self.stats["cache_semantic"] += 1
            return reply, vector
        return None, vector

    def write(self, system: str, user: str, max_tokens: int = 700, schema: dict | None = None, model: str | None = None) -> str:
        # Replies are only shared between calls with the same instructions, schema and length
        # cap: inside that scope, only the user prompt has to match.
        scope = hashlib.sha256(json.dumps([system, schema, max_tokens], sort_keys=True).encode()).hexdigest()[:16]
        exact = hashlib.sha256((scope + user).encode()).hexdigest()
        reply, vector = self._cached(exact, scope, user)
        if reply is not None:
            with self.db:
                self.db.execute("update cache set hits = hits + 1 where exact = ?", (exact,))
            return reply

        spent = self.spent()
        if spent >= self.hard_limit:
            self.stats["refused"] += 1
            raise BudgetExceeded(f"model budget used up: spent {spent:.2f} of {self.hard_limit:.2f} today")
        if spent >= self.soft_limit:
            self.stats["capped_to_cheapest"] += 1
            chosen = 0
        else:
            chosen = len(self.tiers) - 1 if complexity(user) >= self.route_above else 0
        # The chosen tier first, then every other tier, cheapest first: if the capable model is
        # down the cheap one answers, and the other way round.
        chain = [chosen] + [i for i in range(len(self.tiers)) if i != chosen]
        failures = []
        for position, index in enumerate(chain):
            tier = self.tiers[index]
            try:
                reply, tokens = self.complete(system, user, max_tokens, schema, tier.model)
            except Exception as failed:
                failures.append(f"{tier.name}: {failed!r}")
                continue
            self.stats[f"routed_{tier.name}"] += 1
            self.stats["degraded"] += position > 0
            cost = tokens / 1000 * tier.price
            with self.db:
                self.db.execute(
                    "insert into spend values (?,?,1,?,?) on conflict(day, tier) do update set "
                    "calls = calls + 1, tokens = tokens + excluded.tokens, cost = cost + excluded.cost",
                    (self.today(), tier.name, tokens, cost),
                )
                self.db.execute(
                    "insert or replace into cache(exact, scope, prompt, reply, vector) values (?,?,?,?,?)",
                    (exact, scope, user, reply, None if vector is None else vector.tobytes()),
                )
            return reply
        raise RuntimeError("every model in the chain failed: " + "; ".join(failures))
