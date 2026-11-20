"""The tools this server offers. Each has a strict input model and the scope needed to see it."""

import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import sandbox

Corpus = Literal["python", "mcp", "claude"]


class ToolError(Exception):
    """A failure the calling model can act on. Its message is returned as the tool result."""


class Strict(BaseModel):
    # strict: "5" is not an integer. forbid: an argument the tool does not know is an error,
    # not something silently dropped.
    model_config = ConfigDict(strict=True, extra="forbid")


class SearchDocs(Strict):
    """Search the indexed documentation (Python, MCP, Claude) and return the best passages, each
    with the page it comes from."""

    query: str = Field(min_length=3, max_length=500, description="What to look for, in plain words.")
    corpus: Corpus | None = Field(default=None, description="Limit the search to one doc set.")
    k: int = Field(default=5, ge=1, le=10, description="How many passages to return.")


class AskDocs(Strict):
    """Answer a question from the indexed documentation. Every claim cites its page; if the
    documentation does not contain the answer, the reply says so."""

    question: str = Field(min_length=3, max_length=500)
    corpus: Corpus | None = Field(default=None, description="Limit the answer to one doc set.")


class RunPython(Strict):
    """Run a short Python script in a sandbox with no network, no access to the user's files and
    a time limit. Returns what it printed."""

    code: str = Field(min_length=1, max_length=10_000)
    timeout_seconds: int = Field(default=5, ge=1, le=10)


_index = None


def _docs():
    """The footnoted index, loaded on first use so the server starts instantly."""
    global _index
    if _index is None:
        home = Path(os.environ.get("THIRDSHIFT_DOCS", "../footnoted")).resolve()
        if not (home / "data/index.db").exists():
            raise ToolError(f"no documentation index at {home}; set THIRDSHIFT_DOCS to a built footnoted checkout")
        os.chdir(home)  # footnoted resolves data/ relative to the working directory
        from footnoted.index import Index

        _index = Index()
    return _index


def search_docs(args: SearchDocs) -> str:
    hits = _docs().search(args.query, args.corpus, k=args.k)
    return "\n\n".join(f"[{n}] {h['corpus']}: {h['doc']} | {h['heading']}\n{h['text']}" for n, h in enumerate(hits, 1))


def ask_docs(args: AskDocs) -> str:
    index = _docs()
    from footnoted.answer import answer

    return answer(index, args.question, args.corpus).render()


def run_python(args: RunPython) -> str:
    try:
        run = sandbox.run_python(args.code, args.timeout_seconds)
    except sandbox.SandboxUnavailable as unavailable:
        raise ToolError(str(unavailable)) from None
    if run.exit_code is None:
        raise ToolError(f"timed out after {args.timeout_seconds} seconds\n{run.stdout}{run.stderr}")
    if run.exit_code != 0:
        raise ToolError(f"exited with code {run.exit_code}\n{run.stdout}{run.stderr}")
    return run.stdout or "(no output)"


@dataclass(frozen=True)
class Tool:
    name: str
    scope: str  # the grant a server needs before this tool exists for its clients
    model: type[BaseModel]
    run: Callable[[Any], str]

    def describe(self) -> dict:
        return {
            "name": self.name,
            "description": inspect.cleandoc(self.model.__doc__ or ""),
            "inputSchema": self.model.model_json_schema(),
        }


TOOLS = [
    Tool("search_docs", "search", SearchDocs, search_docs),
    Tool("ask_docs", "ask", AskDocs, ask_docs),
    Tool("run_python", "execute", RunPython, run_python),
]
SCOPES = sorted({tool.scope for tool in TOOLS})
