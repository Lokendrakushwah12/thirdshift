"""A durable state machine for long-running workflows.

A run is one row in SQLite: the step it is on, its state, and its status. The row is rewritten
in a single transaction after every step, so a process that dies loses at most the step it was
in the middle of. That step runs again when the run is picked up, which means steps run at
least once and should be safe to repeat.
"""

import copy
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BACKOFF = 2.0  # seconds before the first retry; doubles with each further attempt
LEASE = 300.0  # seconds a worker owns a run for one step before another worker may take it over
MAX_STEPS = 100  # step executions per run; a workflow that loops forever is failed, not followed

# The legal moves between statuses. Anything else is a bug and raises.
TRANSITIONS = {
    "pending": {"running"},
    "running": {"waiting_retry", "waiting_approval", "done", "failed"},
    "waiting_retry": {"running"},
    "waiting_approval": {"running", "rejected"},
    "done": set(),
    "failed": set(),
    "rejected": set(),
}

SCHEMA = """
create table if not exists runs(
    id text primary key, workflow text, status text, step text, state text,
    attempt integer default 0,      -- attempts started on the current step
    executed integer default 0,     -- step executions in the whole run
    next_at real default 0,         -- when a waiting_retry run may run again
    leased_until real default 0,    -- a worker owns the run until this time
    worker text, waiting_for text, error text, created real, updated real);
create table if not exists events(run_id text, at real, step text, kind text, detail text);
"""


class Fatal(Exception):
    """Raised by a step to fail the run at once. Retrying would not help."""


@dataclass(frozen=True)
class Step:
    name: str
    fn: Callable[[dict], dict | None]  # takes the state, returns the keys to change
    max_attempts: int = 3


@dataclass(frozen=True)
class Gate:
    """A point where the run stops until a person approves or rejects it."""

    name: str
    prompt: Callable[[dict], str]  # what to show the person, built from the state


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: tuple[Step | Gate, ...]

    def step(self, name: str) -> Step | Gate:
        return next(s for s in self.steps if s.name == name)

    def after(self, name: str) -> str | None:
        names = [s.name for s in self.steps]
        position = names.index(name) + 1
        return names[position] if position < len(names) else None


class Engine:
    def __init__(self, path: Path | str, workflows: list[Workflow], now: Callable[[], float] = time.time) -> None:
        self.workflows = {w.name: w for w in workflows}
        self.now = now
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def get(self, run_id: str) -> sqlite3.Row:
        return self.db.execute("select * from runs where id = ?", (run_id,)).fetchone()

    def events(self, run_id: str) -> list[tuple[str, str, str]]:
        rows = self.db.execute("select step, kind, detail from events where run_id = ? order by rowid", (run_id,))
        return [tuple(r) for r in rows]

    def _update(self, run_id: str, status: str, kind: str, detail: str = "", **fields) -> None:
        """Move a run to a status, change its fields and log the event, all in one transaction."""
        run = self.get(run_id)
        if status != run["status"] and status not in TRANSITIONS[run["status"]]:
            raise AssertionError(f"illegal transition {run['status']} -> {status} for run {run_id}")
        fields |= {"status": status, "updated": self.now()}
        with self.db:
            self.db.execute(f"update runs set {', '.join(f'{k} = ?' for k in fields)} where id = ?", (*fields.values(), run_id))
            self.db.execute("insert into events values (?,?,?,?,?)", (run_id, self.now(), run["step"], kind, detail))

    def start(self, workflow: str, state: dict | None = None) -> str:
        run_id, now = uuid.uuid4().hex[:12], self.now()
        first = self.workflows[workflow].steps[0].name
        with self.db:
            self.db.execute(
                "insert into runs(id, workflow, status, step, state, created, updated) values (?,?,?,?,?,?,?)",
                (run_id, workflow, "pending", first, json.dumps(state or {}), now, now),
            )
            self.db.execute("insert into events values (?,?,?,?,?)", (run_id, now, first, "started", ""))
        return run_id

    def claim(self, worker: str = "worker") -> str | None:
        """Lease one runnable run to this worker. One statement, so two workers cannot both get it.

        Runnable means: never started, due for a retry, or marked running with an expired lease.
        The last case is crash recovery: the worker that owned the run is gone.
        """
        now = self.now()
        with self.db:
            row = self.db.execute(
                """update runs set status = 'running', leased_until = ?, worker = ? where id = (
                       select id from runs
                       where (status in ('pending', 'running') and leased_until <= ?)
                          or (status = 'waiting_retry' and next_at <= ?)
                       order by updated limit 1)
                   returning id, step""",
                (now + LEASE, worker, now, now),
            ).fetchone()
            if row:
                self.db.execute("insert into events values (?,?,?,?,?)", (row["id"], now, row["step"], "claimed", worker))
        return row["id"] if row else None

    def advance(self, run_id: str) -> str:
        """Run steps until the run finishes or has to wait. Returns the status it stopped in."""
        while True:
            run = self.get(run_id)
            if run["status"] != "running":
                return run["status"]
            workflow = self.workflows[run["workflow"]]
            step, state = workflow.step(run["step"]), json.loads(run["state"])

            if isinstance(step, Gate):
                decision = state.get("approvals", {}).pop(step.name, None)
                if decision is None:
                    self._update(run_id, "waiting_approval", "gate_waiting", waiting_for=step.prompt(state), leased_until=0)
                else:  # approved: a rejection ends the run in approve() and never reaches here
                    state.setdefault("approved", []).append({"gate": step.name, **decision})
                    self._move(run_id, workflow, state, None)
                continue

            if run["executed"] >= MAX_STEPS:
                self._update(run_id, "failed", "failed", error=f"stopped after {MAX_STEPS} step executions")
                continue
            if run["attempt"] >= step.max_attempts:
                # Only a crash gets here: an exception on the last attempt already failed the run.
                self._update(run_id, "failed", "failed", error=run["error"] or "the process died on every attempt")
                continue
            # The attempt is counted before it runs, so a process that dies mid-step has still
            # used one up. Otherwise a step that kills its worker would be retried forever.
            attempt = run["attempt"] + 1
            self._update(run_id, "running", "step_started", str(attempt), attempt=attempt,
                         executed=run["executed"] + 1, leased_until=self.now() + LEASE)
            try:
                change = step.fn(copy.deepcopy(state)) or {}
            except Fatal as fatal:
                self._update(run_id, "failed", "failed", str(fatal), error=str(fatal))
            except Exception as failed:
                if attempt >= step.max_attempts:
                    self._update(run_id, "failed", "failed", repr(failed), error=repr(failed))
                else:
                    # ponytail: no jitter. With many runs retrying against one service, add a
                    # random factor so they do not all come back at the same instant.
                    delay = BACKOFF * 2 ** (attempt - 1)
                    self._update(run_id, "waiting_retry", "step_failed", f"{failed!r}; retry in {delay:g}s",
                                 error=repr(failed), next_at=self.now() + delay, leased_until=0)
            else:
                goto = change.pop("_next", None)  # a step may name the step to run next, to loop or branch
                self._move(run_id, workflow, state | change, goto)

    def _move(self, run_id: str, workflow: Workflow, state: dict, goto: str | None) -> None:
        """Checkpoint: the new state and the next step are written together."""
        run = self.get(run_id)
        following = goto or workflow.after(run["step"])
        if following is not None:
            workflow.step(following)  # an unknown step name fails here, loudly
        fields = {"state": json.dumps(state), "attempt": 0, "error": None, "waiting_for": None}
        if following is None:
            self._update(run_id, "done", "done", leased_until=0, **fields)
        else:
            self._update(run_id, "running", "step_done", following, step=following, **fields)

    def approve(self, run_id: str, approved: bool, note: str = "") -> None:
        run = self.get(run_id)
        if run["status"] != "waiting_approval":
            raise ValueError(f"run {run_id} is {run['status']}, not waiting for approval")
        if not approved:
            self._update(run_id, "rejected", "rejected", note)
            return
        state = json.loads(run["state"])
        state.setdefault("approvals", {})[run["step"]] = {"note": note, "at": self.now()}
        self._update(run_id, "running", "approved", note, state=json.dumps(state), leased_until=0)

    def work(self, worker: str = "worker") -> list[str]:
        """Claim and advance runs until none is runnable right now."""
        touched = []
        while run_id := self.claim(worker):
            self.advance(run_id)
            touched.append(run_id)
        return touched
