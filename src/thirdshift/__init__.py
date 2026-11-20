"""thirdshift: an agent runtime. A raw MCP tool server, and a durable engine that runs workflows on it."""

import argparse
import json
import os
from pathlib import Path

RUNS = Path("data/runs.db")


def _show(engine, run_id: str) -> None:
    run = engine.get(run_id)
    print(f"{run['id']}  {run['workflow']}  {run['status']}  at step {run['step']!r}")
    if run["status"] == "waiting_approval":
        print(f"\n{run['waiting_for']}\n\nDecide with: thirdshift approve {run['id']}   (add --reject to refuse)")
    elif run["status"] == "waiting_retry":
        print(f"  will retry: {run['error']}")
    elif run["status"] == "failed":
        print(f"  error: {run['error']}")
    elif run["status"] == "done":
        state = json.loads(run["state"])
        print(f"\n{state.get('answer', json.dumps(state, indent=1))}")


def main() -> None:
    from .tools import SCOPES

    parser = argparse.ArgumentParser(prog="thirdshift")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the MCP server on stdio")
    serve.add_argument(
        "--allow",
        default="search",
        help=f"comma-separated scopes to grant, from: {', '.join(SCOPES)} (default: search only)",
    )
    run = sub.add_parser("run", help="start a workflow and run it until it finishes or has to wait")
    run.add_argument("workflow")
    run.add_argument("--input", default="{}", help='starting state as JSON, e.g. \'{"question": "..."}\'')
    sub.add_parser("runs", help="list every run and its status")
    approve = sub.add_parser("approve", help="decide a run that is waiting at a gate, then continue it")
    approve.add_argument("run_id")
    approve.add_argument("--reject", action="store_true")
    approve.add_argument("--note", default="")
    sub.add_parser("resume", help="continue every run that can make progress: retries now due, and runs a dead worker left behind")
    versus = sub.add_parser("eval-swarm", help="one coder against the full team on tasks with hidden checks (needs the local model)")
    versus.add_argument("file", type=Path)
    pairs = sub.add_parser("eval-cache", help="how often a semantic cache would serve a wrong answer, by threshold")
    pairs.add_argument("file", type=Path)
    sub.add_parser("eval-injection", help="can a planted comment talk the reviewers into approving wrong code? (needs the local model)")
    routing = sub.add_parser("eval-routing", help="compare routing policies on the coding tasks (needs both local models)")
    routing.add_argument("file", type=Path)
    args = parser.parse_args()

    if args.cmd == "serve":
        scopes = {scope.strip() for scope in args.allow.split(",") if scope.strip()}
        if unknown := scopes - set(SCOPES):
            parser.error(f"unknown scope: {', '.join(sorted(unknown))}")
        from .server import Server, serve as run_server

        run_server(Server(scopes))
        return

    if args.cmd == "eval-cache":
        from .evals import run_cache

        print(run_cache(args.file))
        return

    if args.cmd == "eval-injection":
        from .evals import run_injection

        print(run_injection())
        return

    if args.cmd == "eval-routing":
        from .evals import run_routing

        print(run_routing(args.file))
        return

    if args.cmd == "eval-swarm":
        from .evals import run_swarm

        print(run_swarm(args.file))
        return

    from .durable import Engine
    from .workflows import WORKFLOWS

    if os.environ.get("THIRDSHIFT_PROXY"):  # put the agent team behind the security proxy
        from . import swarm
        from .proxy import Proxy

        RUNS.parent.mkdir(exist_ok=True)
        swarm.guard = Proxy(RUNS.with_name("proxy.db"))

    if os.environ.get("THIRDSHIFT_GATEWAY"):  # send every workflow's model calls through the gateway
        from . import llm
        from .gateway import Gateway, Tier

        RUNS.parent.mkdir(exist_ok=True)
        limit = float(os.environ.get("THIRDSHIFT_BUDGET", "inf"))
        tiers = [Tier("small", llm.SMALL_MODEL, 1.0), Tier("large", llm.MODEL, 2.0)]
        llm.use(Gateway(tiers, llm.complete, RUNS.with_name("gateway.db"), soft_limit=limit * 0.8, hard_limit=limit))

    RUNS.parent.mkdir(exist_ok=True)
    engine = Engine(RUNS, WORKFLOWS)
    if args.cmd == "run":
        if args.workflow not in engine.workflows:
            parser.error(f"unknown workflow; choose from: {', '.join(engine.workflows)}")
        run_id = engine.start(args.workflow, json.loads(args.input))
        engine.work()
        _show(engine, run_id)
    elif args.cmd == "approve":
        engine.approve(args.run_id, not args.reject, args.note)
        engine.work()
        _show(engine, args.run_id)
    elif args.cmd == "resume":
        for run_id in engine.work() or [None]:
            _show(engine, run_id) if run_id else print("nothing is runnable right now")
    else:
        for row in engine.db.execute("select id, workflow, status, step from runs order by created"):
            print(f"{row['id']}  {row['workflow']:10}  {row['status']:17}  {row['step']}")
