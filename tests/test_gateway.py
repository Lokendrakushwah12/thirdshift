"""Checks for the cache and routing gateway, with a scripted model. Run: uv run python tests/test_gateway.py"""

import tempfile
import zlib
from pathlib import Path

import numpy as np

from thirdshift.durable import Engine, Step, Workflow
from thirdshift.gateway import BudgetExceeded, Gateway, Tier, complexity, entropy

SMALL, LARGE = Tier("small", "tiny-model", price=1.0), Tier("large", "big-model", price=4.0)
EASY = "What is two plus two?"
HARD = (
    "Write a function that parses an ISO 8601 timestamp with an optional UTC offset, converts it to the "
    "caller's timezone, and returns exactly the Monday of that week; it must never mutate its input, must "
    "handle leap years and daylight saving transitions, and must raise ValueError unless the string is valid."
)


class Model:
    """Records which model was called; each call 'uses' 1,000 tokens. Models in `down` raise."""

    def __init__(self, down: tuple[str, ...] = ()) -> None:
        self.calls, self.down = [], down

    def __call__(self, system, user, max_tokens, schema, model):
        if model in self.down:
            raise ConnectionError(f"{model} is unreachable")
        self.calls.append(model)
        return f"reply from {model} to: {user}", 1000


def words(text: str) -> np.ndarray:
    vector = np.zeros(128, dtype=np.float32)
    for word in text.lower().replace("?", "").split():
        vector[zlib.crc32(word.encode()) % 128] += 1
    return vector


def gateway(folder: str, model: Model, **options) -> Gateway:
    return Gateway([SMALL, LARGE], model, Path(folder) / "gateway.db", **options)


def test_complexity_separates_a_trivial_prompt_from_a_demanding_one():
    assert entropy("") == 0 and entropy("a a a a") == 0 and entropy("a b c d") == 2
    assert complexity(EASY) < 0.2 < 0.5 < complexity(HARD)


def test_routing_sends_easy_prompts_to_the_cheap_model():
    with tempfile.TemporaryDirectory() as folder:
        model = Model()
        g = gateway(folder, model)
        g.write("sys", EASY)
        g.write("sys", HARD)
        assert model.calls == ["tiny-model", "big-model"]
        assert g.spent() == 5.0  # 1,000 tokens at 1.0 plus 1,000 at 4.0


def test_cache_answers_identical_and_near_identical_prompts_without_a_call():
    with tempfile.TemporaryDirectory() as folder:
        model = Model()
        g = gateway(folder, model, embed=words, similar=0.9)
        first = g.write("sys", "How do I reverse a list in Python please?")
        assert g.write("sys", "How do I reverse a list in Python please?") == first  # identical
        assert g.write("sys", "how do I reverse a list in python please") == first  # same words, different typing
        assert len(model.calls) == 1 and g.stats["cache_exact"] == 1 and g.stats["cache_semantic"] == 1
        # A different question, different instructions or a different schema is a miss.
        g.write("sys", "How do I sort a dictionary by value?")
        g.write("other instructions", "How do I reverse a list in Python please?")
        g.write("sys", "How do I reverse a list in Python please?", schema={"type": "object"})
        assert len(model.calls) == 4
        # The cache outlives the process that filled it.
        again = gateway(folder, Model(), embed=words, similar=0.9)
        assert again.write("sys", "How do I reverse a list in Python please?") == first and again.complete.calls == []


def test_budget_caps_to_the_cheap_model_then_refuses():
    with tempfile.TemporaryDirectory() as folder:
        model = Model()
        g = gateway(folder, model, soft_limit=4.0, hard_limit=6.0)
        g.write("sys", HARD)  # large: spends 4.0, reaching the soft limit
        g.write("sys", HARD + " Also document it.")  # would be large, but is capped to small: 5.0
        assert model.calls == ["big-model", "tiny-model"] and g.stats["capped_to_cheapest"] == 1
        g.write("sys", EASY)  # small: 6.0, reaching the hard limit
        try:
            g.write("sys", "One more question?")
            raise AssertionError("the kill-switch should have refused this call")
        except BudgetExceeded as refused:
            assert "6.00 of 6.00" in str(refused)
        assert g.write("sys", EASY).startswith("reply")  # cached answers still cost nothing and are served
        day = ["2026-01-01"]
        g.today = lambda: day[0]
        assert g.spent() == 0  # a new day starts a new budget


def test_a_failing_model_degrades_to_the_next_in_the_chain():
    with tempfile.TemporaryDirectory() as folder:
        model = Model(down=("big-model",))
        g = gateway(folder, model)
        assert g.write("sys", HARD) == f"reply from tiny-model to: {HARD}"  # large is down: small answers
        assert g.stats["degraded"] == 1 and g.spent() == 1.0
        dead = gateway(folder + "/", Model(down=("big-model", "tiny-model")))
        try:
            dead.write("sys", "Anything at all here?")
            raise AssertionError("expected an error when every model is down")
        except RuntimeError as failed:
            assert "tiny-model" in str(failed) and "big-model" in str(failed)


def test_the_kill_switch_fails_a_durable_run_without_retries():
    with tempfile.TemporaryDirectory() as folder:
        g = gateway(folder, Model(), hard_limit=0.0)
        engine = Engine(Path(folder) / "runs.db", [Workflow("one", (Step("call", lambda s: {"reply": g.write("sys", EASY)}),))])
        run = engine.start("one")
        engine.work()
        row = engine.get(run)
        assert row["status"] == "failed" and "budget used up" in row["error"]
        assert [kind for _, kind, _ in engine.events(run)].count("step_started") == 1  # not retried


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
