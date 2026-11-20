"""Checks for the MCP server and the sandbox. Run: uv run python tests/test_server.py"""

import json
import subprocess
import sys

from pydantic import BaseModel, ConfigDict, Field

from thirdshift.server import META, Server
from thirdshift.tools import TOOLS, Tool, ToolError

MODERN_META = {META + "protocolVersion": "2026-07-28", META + "clientCapabilities": {}}


class Echo(BaseModel):
    """Repeat a word."""

    model_config = ConfigDict(strict=True, extra="forbid")
    word: str = Field(max_length=10)
    times: int = Field(default=1, ge=1, le=3)


def _echo(args: Echo) -> str:
    if args.word == "boom":
        raise ToolError("the word boom is not allowed")
    return " ".join([args.word] * args.times)


ECHO = Tool("echo", "search", Echo, _echo)


def ask(server: Server, method: str, params: dict | None = None, modern: bool = True, **extra) -> dict | None:
    params = dict(params or {})
    if modern:
        params["_meta"] = MODERN_META
    return server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params} | extra))


def test_malformed_messages_get_the_right_json_rpc_errors():
    server = Server({"search"}, [ECHO])
    code = lambda line: server.handle_line(line)["error"]["code"]
    assert code("{not json") == -32700
    assert code("[]") == -32600  # batches are not part of MCP
    assert code('{"jsonrpc": "1.0", "id": 1, "method": "ping"}') == -32600
    assert code('{"jsonrpc": "2.0", "id": null, "method": "ping"}') == -32600
    assert code('{"jsonrpc": "2.0", "id": true, "method": "ping"}') == -32600  # a boolean is not an id
    assert code('{"jsonrpc": "2.0", "id": 1, "method": "ping", "extra": 1}') == -32600
    assert code('{"jsonrpc": "2.0", "id": 1, "method": "no/such"}') == -32601
    # Notifications and stray responses never get a reply, even when they are wrong.
    assert server.handle_line('{"jsonrpc": "2.0", "method": "notifications/initialized"}') is None
    assert server.handle_line('{"jsonrpc": "2.0", "method": "no/such"}') is None
    assert server.handle_line('{"jsonrpc": "2.0", "id": 9, "result": {}}') is None


def test_modern_era_is_stateless_and_versioned():
    server = Server({"search"}, [ECHO])
    found = ask(server, "server/discover")["result"]
    assert found["resultType"] == "complete" and "2026-07-28" in found["supportedVersions"]
    assert found["_meta"][META + "serverInfo"]["name"] == "thirdshift"
    # No handshake is needed: the very first request can be a tool listing.
    assert ask(server, "tools/list")["result"]["resultType"] == "complete"

    unsupported = server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {META + "protocolVersion": "1900-01-01"}}})
    )["error"]
    assert unsupported["code"] == -32022 and unsupported["data"]["requested"] == "1900-01-01"
    assert "2026-07-28" in unsupported["data"]["supported"]

    no_capabilities = server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {META + "protocolVersion": "2026-07-28"}}})
    )["error"]
    assert no_capabilities["code"] == -32602
    assert ask(server, "initialize")["error"]["code"] == -32601  # the handshake is not a modern method


def test_legacy_era_handshake_still_works():
    server = Server({"search"}, [ECHO])
    hello = ask(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, modern=False)["result"]
    assert hello["protocolVersion"] == "2025-06-18" and hello["serverInfo"]["name"] == "thirdshift"
    newer = ask(server, "initialize", {"protocolVersion": "2099-01-01"}, modern=False)["result"]
    assert newer["protocolVersion"] == "2025-11-25"  # an unknown version is answered with the newest legacy one
    listing = ask(server, "tools/list", modern=False)["result"]
    assert "resultType" not in listing and [t["name"] for t in listing["tools"]] == ["echo"]
    assert ask(server, "server/discover", modern=False)["error"]["code"] == -32601


def test_tool_calls_validate_strictly_and_report_errors_to_the_model():
    server = Server({"search"}, [ECHO])
    call = lambda arguments, name="echo": ask(server, "tools/call", {"name": name, "arguments": arguments})
    assert call({"word": "hi", "times": 2})["result"] == {
        "resultType": "complete",
        "content": [{"type": "text", "text": "hi hi"}],
        "isError": False,
    }
    # Bad arguments come back as a tool result the model can read, not as a protocol error.
    for bad in ({"word": "hi", "times": "2"}, {"word": "hi", "times": 9}, {"word": "hi", "nope": 1}, {}):
        result = call(bad)["result"]
        assert result["isError"] and result["content"][0]["text"].startswith("Invalid arguments"), bad
    assert call({"word": "boom"})["result"]["content"][0]["text"] == "the word boom is not allowed"
    # An unknown tool, or a malformed call, is a protocol error.
    assert call({}, name="nope")["error"] == {"code": -32602, "message": "Unknown tool: nope"}
    assert ask(server, "tools/call", {"arguments": {}})["error"]["code"] == -32602
    schema = ask(server, "tools/list")["result"]["tools"][0]["inputSchema"]
    assert schema["additionalProperties"] is False and schema["required"] == ["word"]


def test_scopes_decide_which_tools_exist():
    names = lambda scopes: [t["name"] for t in ask(Server(scopes), "tools/list")["result"]["tools"]]
    assert names({"search"}) == ["search_docs"]
    assert names({"search", "ask", "execute"}) == ["ask_docs", "run_python", "search_docs"]
    assert names(set()) == []
    # Without the grant, the tool is indistinguishable from one that was never there.
    denied = ask(Server({"search"}), "tools/call", {"name": "run_python", "arguments": {"code": "print(1)"}})
    assert denied["error"] == {"code": -32602, "message": "Unknown tool: run_python"}
    assert {t.name for t in TOOLS} == {"search_docs", "ask_docs", "run_python"}


def test_sandbox_blocks_network_files_and_runaway_code():
    if sys.platform != "darwin":
        print("  (sandbox checks skipped: not macOS)")
        return
    from thirdshift.sandbox import run_python

    assert run_python("print(6 * 7)").stdout.strip() == "42"
    inside = run_python("open('note.txt', 'w').write('x'); print(open('note.txt').read())")
    assert inside.stdout.strip() == "x", inside  # its own temporary folder is writable

    blocked = {
        "network": "import socket; socket.create_connection(('1.1.1.1', 53), 2)",
        "reading the user's files": f"open({__file__!r}).read()",
        "writing outside its folder": "open('/tmp/thirdshift-escape', 'w')",
        "starting other programs": "import subprocess; subprocess.run(['/bin/ls'])",
    }
    for what, code in blocked.items():
        run = run_python(code)
        assert run.exit_code not in (0, None) and "Error" in run.stderr, (what, run)

    assert run_python("while True: pass", seconds=1).exit_code is None  # killed at the time limit


def test_end_to_end_over_real_stdio():
    lines = [
        {"jsonrpc": "2.0", "id": "d", "method": "server/discover", "params": {"_meta": MODERN_META}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": MODERN_META}},
    ]
    if sys.platform == "darwin":
        call = {"name": "run_python", "arguments": {"code": "print(6 * 7)"}, "_meta": MODERN_META}
        lines.append({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call})
    done = subprocess.run(
        [sys.executable, "-m", "thirdshift", "serve", "--allow", "execute"],
        input="".join(json.dumps(line) + "\n" for line in lines),
        capture_output=True,
        text=True,
        timeout=60,
    )
    replies = [json.loads(line) for line in done.stdout.splitlines()]  # stdout is protocol messages only
    assert done.returncode == 0  # closing stdin is the shutdown signal
    assert [r["id"] for r in replies] == ["d", 2, 3][: len(replies)] and len(replies) == len(lines) - 1
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["run_python"]
    if sys.platform == "darwin":
        assert replies[2]["result"]["content"][0]["text"].strip() == "42"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
