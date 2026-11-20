# thirdshift

An agent runtime, built one layer at a time. So far: a Model Context Protocol server written
directly against the JSON-RPC wire format, and a durable engine that runs workflows on it.

## Run it

```sh
uv run python tests/test_server.py
uv run python tests/test_durable.py

# Run a workflow: search the docs, wait for approval, answer with citations.
export THIRDSHIFT_DOCS=../footnoted
uv run thirdshift run research --input '{"question": "How does a client cancel a request?", "corpus": "mcp"}'
uv run thirdshift approve <run id>          # or --reject
uv run thirdshift runs                      # every run and its status
uv run thirdshift resume                    # continue whatever can make progress

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

## Durable workflows

`durable.py` is a state machine for workflows that take a long time and must not be lost.

A workflow is a list of steps over a JSON state. A run is one row in SQLite: the step it is on,
its state and its status. After every step the new state and the next step are written in one
transaction.

```
pending -> running -> done
              |  \-> failed
              |-> waiting_retry -> running
              \-> waiting_approval -> running | rejected
```

- **Checkpointing and crash recovery.** A worker leases a run before working on it. If the
  worker dies, the lease runs out and another worker picks the run up at the step it died in.
  Finished steps are never repeated; the interrupted step runs again, so steps run at least
  once and should be safe to repeat.
- **Retries.** A step that raises is retried after 2 s, then 4 s, doubling up to its attempt
  limit, then the run fails with the error kept. A step can raise `Fatal` to fail at once.
  Attempts are counted before a step runs, so a step that kills its worker also runs out of
  attempts instead of being retried forever.
- **Human gates.** A `Gate` stops the run and stores a question built from the state.
  `thirdshift approve` records the decision and continues the run; `--reject` ends it.
- **Loops, bounded.** A step may name the step to run next. A run is failed after 100 step
  executions, so a workflow that loops forever stops.
- **An event log.** Every claim, attempt, failure, decision and checkpoint is appended to an
  `events` table.

The `research` workflow uses all of it: a step calls `search_docs` on the MCP server, a gate
shows the pages found and waits for a person, and a last step calls `ask_docs`.

## What was checked

- `tests/test_server.py` covers malformed messages, both eras, version errors, strict argument
  validation, scopes, every sandbox row above, and one full session over real stdio.
- `tests/test_durable.py` covers ordering, the 2 s then 4 s retry schedule, giving up, fatal
  errors, gates approved and rejected, two workers contending for one lease, and bounded loops.
- **A real crash.** A worker process is killed with `os._exit` in the middle of a step. The run
  stays frozen at that step with the earlier step's result intact; after the lease expires a new
  process finishes it. The first step ran once, the interrupted one twice.
- The `research` workflow was run for real: search, stop at the gate, approval from a separate
  process, then a cited answer from the local model.
- `search_docs` was called over stdio against the real footnoted index and returned the MCP
  cancellation page for "how does a client cancel a request".
- Not checked: the server has not been run against a third-party MCP client.

## Limits

- **The sandbox is not airtight.** A script can tell whether a path under `/Users` exists, though
  not read or list it, and can read system files such as `/etc/hosts`. There is no memory cap:
  macOS does not enforce one per process. Code from strangers belongs in a container or VM.
- **One request at a time.** `notifications/cancelled` is accepted and ignored, because the
  request it names has finished by the time it is read.
- **Steps run at least once, not exactly once.** A step with an outside effect, such as sending
  a message, must tolerate being repeated after a crash. The engine does not pass steps an
  idempotency key yet.
- **A slow step can be run twice at the same time.** A lease lasts 300 seconds per step and is
  not renewed while the step runs, so a step slower than that looks dead to another worker.
- **No retry jitter, one database file.** Fine for one machine; many workers retrying against
  one service would want jitter.
- **Tools only.** No resources, prompts, subscriptions or multi round-trip requests, and stdio
  is the only transport.
