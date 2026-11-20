"""The workflows this runtime knows how to run."""

from .client import call_tool
from .durable import Fatal, Gate, Step, Workflow


def _search(state: dict) -> dict:
    if not state.get("question", "").strip():
        raise Fatal("no question was given")
    arguments = {"query": state["question"], "k": 3} | ({"corpus": state["corpus"]} if state.get("corpus") else {})
    return {"passages": call_tool("search_docs", arguments)}


def _review(state: dict) -> str:
    pages = "\n".join(line for line in state["passages"].splitlines() if line.startswith("["))
    return f"Question: {state['question']}\nFound:\n{pages}\nAnswer from the documentation?"


def _answer(state: dict) -> dict:
    arguments = {"question": state["question"]} | ({"corpus": state["corpus"]} if state.get("corpus") else {})
    return {"answer": call_tool("ask_docs", arguments, scopes="search,ask")}


# Search the docs, stop for a person to look at what was found, then answer with citations.
RESEARCH = Workflow("research", (Step("search", _search), Gate("review", _review), Step("answer", _answer)))

WORKFLOWS = [RESEARCH]
