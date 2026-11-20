"""A minimal MCP client: one tool call to this project's server over stdio."""

import json
import subprocess
import sys

from .durable import Fatal
from .server import META, MODERN, NAME, VERSION


def call_tool(name: str, arguments: dict, scopes: str = "search", timeout: float = 300) -> str:
    """Call one tool and return its text. A protocol error is Fatal; a tool error can be retried."""
    # ponytail: starts a server per call, which costs a few seconds while the index loads.
    # The protocol is stateless, so this is correct; keep one server open if calls get frequent.
    meta = {META + "protocolVersion": MODERN[0], META + "clientCapabilities": {}, META + "clientInfo": {"name": NAME, "version": VERSION}}
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments, "_meta": meta}}
    done = subprocess.run(
        [sys.executable, "-m", "thirdshift", "serve", "--allow", scopes],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if not done.stdout.strip():
        raise RuntimeError(f"the server gave no reply (exit {done.returncode}): {done.stderr.strip()[-300:]}")
    reply = json.loads(done.stdout.splitlines()[0])
    if "error" in reply:
        raise Fatal(f"{name}: {reply['error']['message']}")
    text = reply["result"]["content"][0]["text"]
    if reply["result"]["isError"]:
        raise RuntimeError(f"{name}: {text}")
    return text
