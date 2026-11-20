"""Evals that need the local model."""

import json
import tempfile
import time
from pathlib import Path

MARK = "HIDDEN_CHECK_OK"


def _passes(source: str, check: str) -> bool:
    """Judge the function alone, in the sandbox. The script's own asserts are allowed to fail:
    a small model often writes a correct function and then a wrong test for it."""
    from .sandbox import run_python

    harness = (
        f"scope = {{}}\ntry:\n    exec({source!r}, scope)\nexcept BaseException:\n    pass\n"
        f"exec({check!r}, scope)\nprint({MARK!r})"
    )
    run = run_python(harness, seconds=10)
    return run.exit_code == 0 and MARK in run.stdout


def embedder():
    """The same small embedding model footnoted uses, as a text -> vector function."""
    from fastembed import TextEmbedding

    model = TextEmbedding("BAAI/bge-small-en-v1.5")
    return lambda text: next(iter(model.embed([text])))


def run_cache(path: Path) -> str:
    """How safe is a semantic cache? Pairs that mean the same should hit; pairs that differ by
    one word that changes the answer must not."""
    import numpy as np

    embed = embedder()
    unit = lambda text: (lambda v: v / np.linalg.norm(v))(np.asarray(embed(text), dtype=np.float32))
    pairs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for pair in pairs:
        pair["score"] = float(unit(pair["a"]) @ unit(pair["b"]))
    same = sorted(p["score"] for p in pairs if p["same"])
    different = sorted(p["score"] for p in pairs if not p["same"])
    out = [
        f"same meaning:      {' '.join(f'{s:.3f}' for s in same)}",
        f"different answer:  {' '.join(f'{s:.3f}' for s in different)}",
        "",
        "| threshold | paraphrases served from cache | wrong answers served from cache |",
        "|---|---|---|",
    ]
    for threshold in (0.85, 0.90, 0.95, 0.97, 0.99):
        out.append(f"| {threshold} | {sum(s >= threshold for s in same)} of {len(same)} | {sum(s >= threshold for s in different)} of {len(different)} |")
    return "\n".join(out)


def run_routing(path: Path) -> str:
    """Does the complexity score send the right tasks to the capable model? Every task is tried
    once on each model; the routing policies are then compared on those same results."""
    from . import llm, swarm
    from .gateway import complexity

    tasks = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    price = {llm.SMALL_MODEL: 1.0, llm.MODEL: 2.0}  # per 1,000 tokens, in proportion to model size
    rows = []
    for task in tasks:
        blank = swarm.State(task=task["task"], function=task["function"], research=swarm.Research(notes=[], pages=[]))
        row = {"function": task["function"], "complexity": complexity(task["task"])}
        for name, model in (("small", llm.SMALL_MODEL), ("large", llm.MODEL)):
            before = len(llm.calls)
            swarm.write = lambda system, user, model=model: llm.write(system, user, model=model)
            try:
                row[name] = _passes(swarm.coder(blank).source, task["check"])
            except Exception:
                row[name] = False
            used = llm.calls[before:]
            row[name + "_cost"] = sum((c["prompt"] + c["completion"]) / 1000 * price[model] for c in used)
        rows.append(row)
        print(f"{row['function']:12} complexity={row['complexity']:.2f} small={'pass' if row['small'] else 'FAIL'} large={'pass' if row['large'] else 'FAIL'}", flush=True)
    swarm.write = llm.write

    ranked = sorted(rows, key=lambda r: r["complexity"])
    half = {r["function"] for r in ranked[len(ranked) // 2 :]}  # the more complex half goes to the capable model
    policies = {
        "always the small model": lambda r: "small",
        "always the large model": lambda r: "large",
        "by complexity: top half to large": lambda r: "large" if r["function"] in half else "small",
        "oracle: large only where small fails": lambda r: "small" if r["small"] else "large",
    }
    out = ["", "| policy | tasks passed | cost | sent to large |", "|---|---|---|---|"]
    for name, pick in policies.items():
        picks = [pick(r) for r in rows]
        passed = sum(r[p] for r, p in zip(rows, picks))
        cost = sum(r[p + "_cost"] for r, p in zip(rows, picks))
        if name.startswith("oracle"):  # the oracle has to try small first, so it pays for both where small fails
            cost = sum(r["small_cost"] + (0 if r["small"] else r["large_cost"]) for r in rows)
        out.append(f"| {name} | {passed} of {len(rows)} | {cost:.2f} | {picks.count('large')} |")
    hard = [r["complexity"] for r in rows if not r["small"]]
    easy = [r["complexity"] for r in rows if r["small"]]
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    out += ["", f"mean complexity of tasks the small model failed: {mean(hard):.2f} ({len(hard)} tasks); passed: {mean(easy):.2f} ({len(easy)} tasks)"]
    return "\n".join(out)


def run_swarm(path: Path) -> str:
    """One coder, one attempt, against the full team, on tasks whose check the agents never see."""
    from . import llm, swarm
    from .durable import Engine

    tasks = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rows = []
    for task in tasks:
        row = {"function": task["function"]}
        calls, start = len(llm.calls), time.perf_counter()
        blank = swarm.State(task=task["task"], function=task["function"], research=swarm.Research(notes=[], pages=[]))
        try:
            row["solo"] = _passes(swarm.coder(blank).source, task["check"])
        except Exception:
            row["solo"] = False  # an unusable reply is a failed attempt
        row["solo_calls"], row["solo_seconds"] = len(llm.calls) - calls, time.perf_counter() - start

        calls, start = len(llm.calls), time.perf_counter()
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine(Path(folder) / "runs.db", [swarm.BUILD])
            run = engine.start("build", {"task": task["task"], "function": task["function"]})
            while engine.work() or engine.get(run)["status"] == "waiting_retry":
                if engine.get(run)["status"] == "waiting_retry":
                    time.sleep(1)
            state, status = json.loads(engine.get(run)["state"]), engine.get(run)["status"]
        row["team_calls"], row["team_seconds"] = len(llm.calls) - calls, time.perf_counter() - start
        row["status"], row["rounds"] = status, state.get("round", 0)
        row["tally"] = (state.get("decision") or {}).get("tally", "")
        row["team"] = bool(state.get("code")) and _passes(state["code"]["source"], task["check"])
        row["self_test_ok"] = bool((state.get("execution") or {}).get("ok"))
        rows.append(row)
        print(f"{row['function']:12} solo={'pass' if row['solo'] else 'FAIL'}  team={'pass' if row['team'] else 'FAIL'} "
              f"({status}, round {row['rounds']}, votes {row['tally']})", flush=True)

    n = len(rows)
    accepted = [r for r in rows if r["status"] == "done"]
    total = lambda key: sum(r[key] for r in rows)
    return "\n".join(
        [
            "",
            f"| | passes the hidden check | model calls | seconds |",
            "|---|---|---|---|",
            f"| one coder, one attempt | {total('solo')} of {n} | {total('solo_calls')} | {total('solo_seconds'):.0f} |",
            f"| the team | {total('team')} of {n} | {total('team_calls')} | {total('team_seconds'):.0f} |",
            "",
            f"team outcomes: {len(accepted)} accepted by the reviewers "
            f"({sum(r['team'] for r in accepted)} correct, {sum(not r['team'] for r in accepted)} wrong), "
            f"{sum(r['status'] == 'waiting_approval' for r in rows)} escalated to a person, "
            f"{sum(r['status'] == 'failed' for r in rows)} failed",
            f"final scripts whose own asserts failed although the function was correct: "
            f"{sum(r['team'] and not r['self_test_ok'] for r in rows)}",
            f"tasks the team fixed that the single coder got wrong: {sum(r['team'] and not r['solo'] for r in rows)}",
            f"tasks the team got wrong that the single coder got right: {sum(r['solo'] and not r['team'] for r in rows)}",
        ]
    )
