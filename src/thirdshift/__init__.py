"""thirdshift: an agent runtime. A raw MCP tool server, and a durable engine that runs workflows on it."""

import argparse
import json
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
    args = parser.parse_args()

    if args.cmd == "serve":
        scopes = {scope.strip() for scope in args.allow.split(",") if scope.strip()}
        if unknown := scopes - set(SCOPES):
            parser.error(f"unknown scope: {', '.join(sorted(unknown))}")
        from .server import Server, serve as run_server

        run_server(Server(scopes))
        return

    from .durable import Engine
    from .workflows import WORKFLOWS

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
