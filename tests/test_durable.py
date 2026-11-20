"""Checks for the durable state machine. Run: uv run python tests/test_durable.py"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from thirdshift import durable
from thirdshift.durable import Engine, Fatal, Gate, Step, Workflow


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def engine(folder: str, *workflows: Workflow, clock: Clock | None = None) -> Engine:
    return Engine(Path(folder) / "runs.db", list(workflows), now=clock or Clock())


def kinds(e: Engine, run_id: str) -> list[str]:
    return [kind for _, kind, _ in e.events(run_id)]


def test_steps_run_in_order_and_state_is_checkpointed():
    flow = Workflow("sum", (Step("a", lambda s: {"n": s["n"] + 1}), Step("b", lambda s: {"n": s["n"] * 10})))
    with tempfile.TemporaryDirectory() as folder:
        e = engine(folder, flow)
        run = e.start("sum", {"n": 1})
        assert e.work() == [run]
        row = e.get(run)
        assert row["status"] == "done" and json.loads(row["state"]) == {"n": 20}
        assert kinds(e, run) == ["started", "claimed", "step_started", "step_done", "step_started", "done"]
        assert e.work() == []  # a finished run is never picked up again


def test_failures_back_off_exponentially_then_give_up():
    calls = []

    def flaky(state):
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("upstream down")
        return {"ok": True}

    always = Step("always", lambda s: 1 / 0, max_attempts=2)
    with tempfile.TemporaryDirectory() as folder:
        clock = Clock()
        e = engine(folder, Workflow("flaky", (Step("call", flaky),)), Workflow("doomed", (always,)), clock=clock)
        run = e.start("flaky")
        e.work()
        assert e.get(run)["status"] == "waiting_retry" and e.get(run)["next_at"] == clock.t + 2  # first retry: 2 s
        assert e.work() == []  # not due yet, so nothing is runnable
        clock.t += 2
        e.work()
        assert e.get(run)["next_at"] == clock.t + 4  # second retry: 4 s
        clock.t += 4
        e.work()
        assert e.get(run)["status"] == "done" and len(calls) == 3 and e.get(run)["error"] is None

        doomed = e.start("doomed")
        e.work()
        clock.t += 2
        e.work()
        row = e.get(doomed)
        assert row["status"] == "failed" and "ZeroDivisionError" in row["error"]  # out of attempts


def test_fatal_errors_are_not_retried():
    def refuse(state):
        raise Fatal("the question is empty")

    with tempfile.TemporaryDirectory() as folder:
        e = engine(folder, Workflow("strict", (Step("check", refuse),)))
        run = e.start("strict")
        e.work()
        assert e.get(run)["status"] == "failed" and e.get(run)["error"] == "the question is empty"
        assert kinds(e, run).count("step_started") == 1


def test_gate_stops_the_run_until_a_person_decides():
    flow = Workflow(
        "publish",
        (
            Step("draft", lambda s: {"text": "hello"}),
            Gate("review", lambda s: f"Publish {s['text']!r}?"),
            Step("send", lambda s: {"sent": True}),
        ),
    )
    with tempfile.TemporaryDirectory() as folder:
        e = engine(folder, flow)
        run, other = e.start("publish"), e.start("publish")
        e.work()
        assert e.get(run)["status"] == "waiting_approval" and e.get(run)["waiting_for"] == "Publish 'hello'?"
        assert e.work() == []  # waiting on a person is not runnable
        assert "sent" not in json.loads(e.get(run)["state"])

        e.approve(run, True, "looks fine")
        e.work()
        state = json.loads(e.get(run)["state"])
        assert e.get(run)["status"] == "done" and state["sent"] and state["approved"][0]["note"] == "looks fine"

        e.approve(other, False, "not yet")
        assert e.get(other)["status"] == "rejected" and e.work() == []
        try:
            e.approve(other, True)
            raise AssertionError("a decided run must not be decided again")
        except ValueError:
            pass


def test_a_lease_keeps_two_workers_off_the_same_run_until_it_expires():
    with tempfile.TemporaryDirectory() as folder:
        clock = Clock()
        flow = Workflow("one", (Step("only", lambda s: {}),))
        first, second = engine(folder, flow, clock=clock), engine(folder, flow, clock=clock)
        run = first.start("one")
        assert first.claim("first") == run
        assert second.claim("second") is None  # leased
        clock.t += durable.LEASE + 1  # the first worker went silent
        assert second.claim("second") == run
        assert second.advance(run) == "done"


def test_loops_are_allowed_but_bounded():
    again = Step("again", lambda s: {"n": s.get("n", 0) + 1, "_next": "again"})
    twice = Step("twice", lambda s: {"n": s.get("n", 0) + 1} | ({"_next": "twice"} if s.get("n", 0) < 2 else {}))
    with tempfile.TemporaryDirectory() as folder:
        e = engine(folder, Workflow("forever", (again,)), Workflow("thrice", (twice,)))
        forever, thrice = e.start("forever"), e.start("thrice")
        e.work()
        assert e.get(thrice)["status"] == "done" and json.loads(e.get(thrice)["state"])["n"] == 3
        row = e.get(forever)
        assert row["status"] == "failed" and row["executed"] == durable.MAX_STEPS and "step executions" in row["error"]


# --- a real crash: the worker process is killed in the middle of a step ---


def _log(state: dict, word: str) -> None:
    with open(state["log"], "a") as handle:
        handle.write(word + "\n")


def _prepare(state):
    _log(state, "prepare")
    return {"prepared": True}


def _charge(state):
    _log(state, "charge")
    if not os.path.exists(state["marker"]):
        Path(state["marker"]).touch()
        os._exit(9)  # die at once: no exception, no cleanup, nothing written back
    return {"charged": True}


CRASHY = Workflow("crashy", (Step("prepare", _prepare), Step("charge", _charge), Step("finish", lambda s: {"finished": True})))
DEADLY = Workflow("deadly", (Step("die", lambda s: os._exit(9), max_attempts=2),))


def test_a_killed_worker_loses_only_the_step_it_was_in():
    with tempfile.TemporaryDirectory() as folder:
        db, log = Path(folder) / "runs.db", Path(folder) / "log.txt"
        clock = Clock()
        e = Engine(db, [CRASHY, DEADLY], now=clock)
        run = e.start("crashy", {"log": str(log), "marker": str(Path(folder) / "marker")})
        dead = subprocess.run([sys.executable, __file__, "work", str(db)])
        assert dead.returncode == 9  # the worker really died
        row = e.get(run)
        assert row["status"] == "running" and row["step"] == "charge"  # frozen where it died
        assert json.loads(row["state"])["prepared"] is True  # the finished step's result survived

        assert e.work() == []  # the dead worker's lease still holds
        clock.t = __import__("time").time() + durable.LEASE + 1
        e.work()  # a new worker takes over
        assert e.get(run)["status"] == "done" and json.loads(e.get(run)["state"])["finished"]
        # prepare ran once and was not repeated; charge ran twice: at least once, not exactly once.
        assert log.read_text().split() == ["prepare", "charge", "charge"]

        # A step that kills every worker uses up its attempts and fails instead of looping.
        deadly = e.start("deadly")
        for _ in range(2):
            assert subprocess.run([sys.executable, __file__, "work", str(db)]).returncode == 9
            with e.db:  # stand in for the lease running out; the child workers use the real clock
                e.db.execute("update runs set leased_until = 0 where id = ?", (deadly,))
        e.work()
        assert e.get(deadly)["status"] == "failed" and "died on every attempt" in e.get(deadly)["error"]


if __name__ == "__main__":
    if sys.argv[1:2] == ["work"]:  # the child process of the crash test
        Engine(sys.argv[2], [CRASHY, DEADLY]).work()
        sys.exit(0)
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
