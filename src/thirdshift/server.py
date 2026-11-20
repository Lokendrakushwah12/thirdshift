"""A Model Context Protocol server written against the JSON-RPC wire format, with no SDK.

It speaks both eras of the protocol over newline-delimited stdio:

modern  2026-07-28. Stateless: every request carries its protocol version in `_meta`, and
        `server/discover` replaces the handshake.
legacy  2025-11-25 and earlier. A session opened by an `initialize` handshake.

A request is modern if its `_meta` names a protocol version, and legacy otherwise.
"""

import json
import sys
from typing import IO, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from .tools import TOOLS, Tool, ToolError

NAME, VERSION = "thirdshift", "0.1.0"
MODERN = ["2026-07-28"]
LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26"]
META = "io.modelcontextprotocol/"
INSTRUCTIONS = "Search and question answering over indexed documentation, with citations."

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
UNSUPPORTED_VERSION = -32022


class Message(BaseModel):
    """A JSON-RPC request or notification. Anything else on the wire is rejected."""

    model_config = ConfigDict(strict=True, extra="forbid")
    jsonrpc: Literal["2.0"]
    method: str = Field(min_length=1)
    id: StrictInt | StrictStr | None = None
    params: dict[str, Any] = {}


class CallParams(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")  # `_meta` rides along
    name: str
    arguments: dict[str, Any] = {}


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code, self.message, self.data = code, message, data


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    error = {"code": code, "message": message} | ({} if data is None else {"data": data})
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _brief(invalid: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in invalid.errors())


def _text(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class Server:
    def __init__(self, scopes: set[str], tools: list[Tool] = TOOLS) -> None:
        # Least privilege: a tool outside the granted scopes does not exist for this server.
        # It is not listed, and calling it is the same error as calling a name that was never real.
        self.tools = {tool.name: tool for tool in sorted(tools, key=lambda t: t.name) if tool.scope in scopes}

    def handle_line(self, line: str) -> dict | None:
        """One line in, at most one line out. Never raises."""
        try:
            raw = json.loads(line)
        except ValueError:
            return _error(None, PARSE_ERROR, "Parse error")
        if not isinstance(raw, dict):  # batches were removed from MCP in 2025-06-18
            return _error(None, INVALID_REQUEST, "Invalid request: expected one JSON object")
        if "method" not in raw and ("result" in raw or "error" in raw):
            return None  # a response; this server never sends requests, so there is nothing to match
        notification = "id" not in raw
        request_id = raw.get("id") if type(raw.get("id")) in (int, str) else None
        try:
            message = Message.model_validate(raw)
            if not notification and message.id is None:
                raise RpcError(INVALID_REQUEST, "Invalid request: id must not be null")
            result = self.dispatch(message)
        except ValidationError as invalid:
            return None if notification else _error(request_id, INVALID_REQUEST, f"Invalid request: {_brief(invalid)}")
        except RpcError as failed:
            return None if notification else _error(request_id, failed.code, failed.message, failed.data)
        except Exception as crashed:  # a bug here must not take the connection down
            print(f"internal error handling {raw.get('method')!r}: {crashed!r}", file=sys.stderr)
            return None if notification else _error(request_id, INTERNAL_ERROR, "Internal error")
        return None if notification else {"jsonrpc": "2.0", "id": message.id, "result": result}

    def dispatch(self, message: Message) -> dict:
        if message.method.startswith("notifications/"):
            # ponytail: notifications/cancelled is accepted and ignored. Requests are handled one
            # at a time, so by the time it is read the request it names has already finished.
            return {}
        meta = message.params.get("_meta")
        modern = isinstance(meta, dict) and META + "protocolVersion" in meta
        if modern:
            requested = meta[META + "protocolVersion"]
            if requested not in MODERN:
                detail = {"supported": MODERN + LEGACY, "requested": requested}
                raise RpcError(UNSUPPORTED_VERSION, "Unsupported protocol version", detail)
            if META + "clientCapabilities" not in meta:
                raise RpcError(INVALID_PARAMS, f"Invalid params: _meta is missing {META}clientCapabilities")
        handler = {
            "server/discover": self.discover if modern else None,
            "initialize": None if modern else self.initialize,
            "ping": lambda params: {},
            "tools/list": self.list_tools,
            "tools/call": self.call_tool,
        }.get(message.method)
        if handler is None:
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {message.method}")
        result = handler(message.params)
        return {"resultType": "complete", **result} if modern else result

    def discover(self, params: dict) -> dict:
        return {
            "supportedVersions": MODERN + LEGACY,
            "capabilities": {"tools": {}},
            "_meta": {META + "serverInfo": {"name": NAME, "version": VERSION}},
            "instructions": INSTRUCTIONS,
        }

    def initialize(self, params: dict) -> dict:
        wanted = params.get("protocolVersion")
        return {
            "protocolVersion": wanted if wanted in LEGACY else LEGACY[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": INSTRUCTIONS,
        }

    def list_tools(self, params: dict) -> dict:
        return {"tools": [tool.describe() for tool in self.tools.values()]}

    def call_tool(self, params: dict) -> dict:
        try:
            call = CallParams.model_validate(params)
        except ValidationError as invalid:
            raise RpcError(INVALID_PARAMS, f"Invalid params: {_brief(invalid)}") from None
        tool = self.tools.get(call.name)
        if tool is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {call.name}")
        # From here on, failures go back as tool results with isError, not protocol errors:
        # the model that made the call can read them and try again.
        try:
            arguments = tool.model.model_validate(call.arguments)
        except ValidationError as invalid:
            return _text(f"Invalid arguments: {_brief(invalid)}", is_error=True)
        try:
            return _text(tool.run(arguments))
        except ToolError as failed:
            return _text(str(failed), is_error=True)
        except Exception as crashed:
            print(f"tool {call.name} crashed: {crashed!r}", file=sys.stderr)
            return _text(f"The {call.name} tool failed unexpectedly.", is_error=True)


def serve(server: Server, lines: IO[str] = sys.stdin) -> None:
    """Read requests until the input closes, which is how a stdio client asks a server to stop."""
    out = sys.stdout
    sys.stdout = sys.stderr  # stdout carries only protocol messages; a stray print goes to stderr
    for line in lines:
        if not line.strip():
            continue
        reply = server.handle_line(line)
        if reply is not None:
            out.write(json.dumps(reply, separators=(",", ":"), ensure_ascii=False) + "\n")
            out.flush()
