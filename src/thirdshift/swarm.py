"""A small team of agents with typed hand-offs, run by the durable engine.

researcher  reads the documentation and writes notes
coder       writes a script that solves the task and tests itself
runner      executes the script in the sandbox (not a model)
reviewers   three, each with one concern, vote to approve or object
supervisor  a fixed policy, not a model: it reads the run result and the votes and picks the
            next move

Everything an agent hands on is a Pydantic model. The whole state is validated on the way into
every step and on the way out, so a malformed reply from a model stops at the agent that
produced it.
"""

import json
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from .client import ToolFailed, call_tool
from .durable import Gate, Step, Workflow
from .llm import ask, write
from .tools import HEADER

MAX_ROUNDS = 3  # attempts the coder gets before a person is asked


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Research(Strict):
    notes: list[str]
    pages: list[str]


class Code(Strict):
    source: str


class Execution(Strict):
    ok: bool
    output: str


class Review(Strict):
    reviewer: str
    approve: bool
    reason: str


class Decision(Strict):
    outcome: Literal["accept", "revise", "escalate"]
    tally: str  # approvals-objections, e.g. "2-1"
    feedback: list[str]  # what the coder must fix
    dissent: list[str]  # objections that were outvoted, kept on the record


class State(Strict):
    task: str
    function: str  # the name the coder must give its function
    round: int = 0
    research: Research | None = None
    code: Code | None = None
    execution: Execution | None = None
    reviews: list[Review] = []
    decision: Decision | None = None
    result: str = ""
    approvals: dict = {}  # the engine's bookkeeping for the escalation gate
    approved: list = []


# One concern per reviewer, so the votes are not three copies of the same opinion.
LENSES = {
    "correctness": "Does the function do exactly what the task asks, including the edge cases the task implies?",
    "grounding": "Does the script use the standard-library API described in the research notes, rather than inventing one or reimplementing it badly?",
    "simplicity": "Is the script short, with nothing the task does not need: no network, no files, no extra features?",
}

RESEARCH_PROMPT = """You are the researcher on a small programming team. From the documentation \
passages, write the two or three facts most useful for the task: which standard-library module \
and function to use, and how it is called. Every note must come from the passages."""

CODER_PROMPT = """You are the coder on a small programming team. Write one complete Python \
script for the task, using only the standard library. Define a function with exactly the name \
you are given. After the function, add two or three assert statements that test it, then \
print('ok'). Keep the script short. Reply with the script only: no explanation and no Markdown."""

REVIEW_PROMPT = """You are a code reviewer with one concern: {lens} You are given the task, the \
script, and what happened when it ran. Approve only if you have no objection on your concern. \
Give your reason in one specific sentence."""

NOTES_SCHEMA = {"type": "object", "properties": {"notes": {"type": "array", "items": {"type": "string"}}}, "required": ["notes"]}
VOTE_SCHEMA = {"type": "object", "properties": {"approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["approve", "reason"]}


def researcher(state: State) -> Research:
    passages = call_tool("search_docs", {"query": state.task, "corpus": "python", "k": 4})
    pages = list(dict.fromkeys(HEADER.findall(passages)))
    try:
        notes = ask(RESEARCH_PROMPT, f"Task: {state.task}\n\nPassages:\n{passages[:5000]}", NOTES_SCHEMA)["notes"]
    except ValueError:
        notes = []  # research is advice: an unusable reply means the coder works without notes, not that the run dies
    return Research(notes=notes, pages=pages)


def coder(state: State) -> Code:
    prompt = f"Task: {state.task}\nFunction name: {state.function}\n\nResearch notes:\n" + "\n".join(f"- {n}" for n in state.research.notes)
    if state.decision and state.code:  # a revision: show what was wrong with the last attempt
        prompt += f"\n\nYour previous script:\n{state.code.source}\n\nIt must be fixed:\n" + "\n".join(f"- {f}" for f in state.decision.feedback)
    # Plain text, not JSON. Asked for the script as a JSON string, a small model either returned
    # a URL for a field named "source" or ran on in escaped newlines until the reply was cut off.
    script = write(CODER_PROMPT, prompt).strip()
    if script.startswith("```"):  # models add fences however they are told not to
        script = script.split("\n", 1)[-1].rsplit("```", 1)[0]
    return Code(source=script)


def runner(state: State) -> Execution:
    try:
        return Execution(ok=True, output=call_tool("run_python", {"code": state.code.source}, scopes="execute"))
    except ToolFailed as failed:  # the script failed; that is a result, not a problem with the run
        return Execution(ok=False, output=str(failed))


def reviewers(state: State) -> list[Review]:
    notes = "\n".join(f"- {n}" for n in state.research.notes)
    ran = "It ran and printed" if state.execution.ok else "It FAILED with"
    evidence = f"Task: {state.task}\n\nResearch notes:\n{notes}\n\nScript:\n{state.code.source}\n\n{ran}:\n{state.execution.output[-600:]}"
    return [Review(reviewer=name, **ask(REVIEW_PROMPT.format(lens=lens), evidence, VOTE_SCHEMA)) for name, lens in LENSES.items()]


def supervisor(state: State) -> Decision:
    """Turn the run result and the votes into the next move. Rules, in order:

    1. A script that failed when run is revised, whatever the reviewers said: evidence beats votes.
    2. Otherwise the majority decides. A tie counts as an objection.
    3. Outvoted objections are kept as dissent on an accepted result.
    4. When the coder is out of rounds, a person decides.
    """
    objections = [f"{r.reviewer}: {r.reason}" for r in state.reviews if not r.approve]
    approvals = len(state.reviews) - len(objections)
    tally = f"{approvals}-{len(objections)}"
    if not state.execution.ok:
        feedback = [f"The script failed when it was run: {state.execution.output[-400:]}"]
    elif approvals > len(objections):
        return Decision(outcome="accept", tally=tally, feedback=[], dissent=objections)
    else:
        feedback = objections
    outcome = "escalate" if state.round >= MAX_ROUNDS else "revise"
    return Decision(outcome=outcome, tally=tally, feedback=feedback, dissent=[])


def typed(agent: Callable[[State], dict]) -> Callable[[dict], dict]:
    """Wrap an agent as an engine step: validate the state coming in and the state going out."""

    def step(raw: dict) -> dict:
        state = State.model_validate_json(json.dumps(raw))
        change = agent(state)
        goto = change.pop("_next", None)
        after = State.model_validate_json(state.model_copy(update=change).model_dump_json())
        return after.model_dump() | ({"_next": goto} if goto else {})

    return step


def _decide(state: State) -> dict:
    decision = supervisor(state)
    goto = {"accept": "finish", "revise": "code", "escalate": None}[decision.outcome]
    return {"decision": decision, "_next": goto}


def _escalation(raw: dict) -> str:
    state = State.model_validate_json(json.dumps(raw))
    problems = "\n".join(f"- {f}" for f in state.decision.feedback)
    return (
        f"Task: {state.task}\nAfter {state.round} attempts the reviewers voted {state.decision.tally} "
        f"(approve-object).\nOpen problems:\n{problems}\n\nLast script:\n{state.code.source}\n\nAccept it anyway?"
    )


def _finish(state: State) -> dict:
    if state.decision.outcome == "accept":
        dissent = f", with dissent: {'; '.join(state.decision.dissent)}" if state.decision.dissent else ""
        return {"result": f"accepted by the reviewers {state.decision.tally} in round {state.round}{dissent}"}
    return {"result": f"accepted by a person after {state.round} rounds; the reviewers voted {state.decision.tally}"}


BUILD = Workflow(
    "build",
    (
        Step("research", typed(lambda s: {"research": researcher(s)})),
        Step("code", typed(lambda s: {"code": coder(s), "round": s.round + 1})),
        Step("run", typed(lambda s: {"execution": runner(s)})),
        Step("review", typed(lambda s: {"reviews": reviewers(s)})),
        Step("decide", typed(_decide)),
        Gate("escalate", _escalation),
        Step("finish", typed(_finish)),
    ),
)
