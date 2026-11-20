# thirdshift

An agent runtime, built one layer at a time. The first layer is a Model Context Protocol
server written directly against the JSON-RPC wire format, with no MCP SDK.

## Run it

```sh
uv run python tests/test_server.py

# Serve the documentation tools from a built footnoted checkout:
THIRDSHIFT_DOCS=../footnoted uv run thirdshift serve --allow search,ask,execute
```

To try it with the official Inspector:

```sh
npx @modelcontextprotocol/inspector --cli uv run thirdshift serve --method tools/list
```

## The MCP server

**Both protocol eras, on stdio.** The 2026-07-28 revision of MCP removed the `initialize`
handshake: the protocol is stateless, every request carries its protocol version in `_meta`,
and `server/discover` reports what a server supports. Clients built for 2025-11-25 and earlier
still open with `initialize`. This server answers both. A request is modern if its `_meta`
names a protocol version, and legacy otherwise; modern results carry `resultType`, legacy ones
do not.

**Strict schemas.** Every message is validated by a Pydantic model in strict mode before
anything else looks at it, and so are the arguments of every tool call. `"5"` is not an
integer, a boolean is not a request id, and an argument the tool does not declare is an error.
The JSON Schema each tool advertises is generated from the same model that validates it.

**Two kinds of failure.** A malformed request or an unknown tool is a JSON-RPC error. Bad
arguments and failures inside a tool come back as a tool result with `isError: true`, so the
model that made the call can read the message and try again.

**Scopes.** Each tool needs a scope, granted when the server starts. A tool outside the granted
scopes is not listed, and calling it returns the same "Unknown tool" error as a name that never
existed. The default grant is `search` only.

| tool | scope | what it does |
|---|---|---|
| `search_docs` | `search` | hybrid search over the footnoted index |
| `ask_docs` | `ask` | footnoted's cited answer loop (needs the local model) |
| `run_python` | `execute` | runs a script in the sandbox below |

**Sandbox.** `run_python` executes inside the macOS sandbox (Seatbelt) in a temporary folder,
with an empty environment, a time limit and a 1 MB cap on files it writes. On other platforms
the tool refuses; it never falls back to running unsandboxed.

| attempted from inside | result |
|---|---|
| open a network connection, resolve a hostname | blocked |
| read or list a file under `/Users` | blocked |
| write outside its temporary folder | blocked |
| start another program | blocked |
| run past the time limit | killed |
| import a third-party package | not available: standard library only |

## What was checked

- `tests/test_server.py` covers malformed messages, both eras, version errors, strict argument
  validation, scopes, every sandbox row above, and one full session over real stdio.
- `search_docs` was called over stdio against the real footnoted index and returned the MCP
  cancellation page for "how does a client cancel a request".
- Not checked: the server has not been run against a third-party MCP client.

## Limits

- **The sandbox is not airtight.** A script can tell whether a path under `/Users` exists, though
  not read or list it, and can read system files such as `/etc/hosts`. There is no memory cap:
  macOS does not enforce one per process. Code from strangers belongs in a container or VM.
- **One request at a time.** `notifications/cancelled` is accepted and ignored, because the
  request it names has finished by the time it is read.
- **Tools only.** No resources, prompts, subscriptions or multi round-trip requests, and stdio
  is the only transport.
