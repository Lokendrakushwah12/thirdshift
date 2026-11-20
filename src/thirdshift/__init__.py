"""thirdshift: an agent runtime. So far: a raw MCP tool server."""

import argparse


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
    args = parser.parse_args()

    scopes = {scope.strip() for scope in args.allow.split(",") if scope.strip()}
    if unknown := scopes - set(SCOPES):
        parser.error(f"unknown scope: {', '.join(sorted(unknown))}")
    from .server import Server, serve as run

    run(Server(scopes))
