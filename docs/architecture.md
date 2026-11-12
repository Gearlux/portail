# portaide — architecture

Decision records, each Context → Decision → Consequences: serving, the lifecycle commands and the page's shell
(§1-12), the MCP server base (§13-16), the filter language (§17) and the viewer's protocols (§18).

## 1. One seam: `AppSpec`, `Served`, `Hooks`

**Context.** The lifecycle of a workspace web app — serve, find again, stop, restart, tell its pages — is the same
for every app; what the workspace holds is not.
**Decision.** An app hands portaide an `AppSpec` (its name, CLI module, default address, state folder, hooks) and a
workspace implementing `Served` (`host`, `port`, `path`, `template`, `title`, `state_dir()`, `info()`). Every message
says `spec.name`, every file lives in `spec.state_dir()`, every environment variable is `spec.env(word)`.
**Consequences.** portaide imports nothing of an app (`tests/test_no_private_names.py`); the suite proves it on a
demo app of its own (`tests/demo_cli.py`).

## 2. A server is found by its identity, from its LIVE answer

**Context.** A port says nothing about what serves it. Measured on an app with this lifecycle: a stop of a type that
was not running fell back to that type's default port and stopped a server serving another workspace file, reporting
a line that named no workspace.
**Decision.** `/api/meta` answers `{"app": spec.name, "title", "workspace": served.info(), "pid"}`. A server is the
app's when `app` is its name (`serving.app_at`); its identity is its file, else `type:<template>`, read from the LIVE
answer — the registry (`<state>/servers/<port>.json`, written around serving) only says where to ask. A fallback stop
of a port compares identities; a listener that does not answer as the app is refused, never signalled.
**Consequences.** A server too wedged to answer its status check is refused like a foreign program, and is ended with
`kill`. Do not bring back an identity-less SIGTERM to cover it. Pins: `tests/test_stop_choice.py`.

## 3. A stop signals only the pids LISTENING on the ports it stops

**Context.** Measured: a stop that believed it ended the last server swept every process of the app's MCP server
found by command line — the user's other sessions lost their tools. And a hung `lsof` read as "nothing listens"
reported a running server as stopped.
**Decision.** `pids_listening` asks `ss` first and `lsof` as the fallback; neither answering is an error, never an
empty port. `stop_port` sends SIGTERM to those pids, SIGKILL after five seconds. Nothing is ever found by `ps`.
**Consequences.** A process that binds no port outlives every stop; its life is whoever started it. Pins:
`tests/test_pids_listening.py`, `tests/test_stop_choice.py::TestAStopSignalsOnlyTheServersItStops`.

## 4. A new workspace finds its own port, a saved one does not

**Context.** A second workspace started on a port the first one serves silently got the first one's URL; a type
workspace is a new workspace each time, while a saved file's port is what other commands talk to.
**Decision.** `resolve_port_conflict`: the same saved workspace keeps the already-running report; another workspace
asks `[Y/n]` (no terminal: refuse, naming `--auto-port`); a program that is not the app is left to `Server.serve`'s
located `PortInUseError`. `workspace <type>` turns `--auto-port` on unless `--port` names the port: it then takes the
next free port whoever holds the default. The ask lives in the CLI side, never in `Server`.
**Consequences.** The suite reads the default port free (`conftest._the_default_port_reads_free`), so a machine's own
listener never decides a test. Pins: `tests/test_auto_port.py`.

## 5. Serving detached waits for an answer

**Context.** A detached process that died on a bad workspace left a prompt, no server, and nothing said; a child
started while the same workspace already answered printed `serving … background pid N` for a child that had exited.
**Decision.** The port conflict settles BEFORE the fork, in the terminal; the child's argv is REBUILT
(`python -m <spec.cli_module> open|workspace … --port N --browser never`), never replayed from `sys.argv`; the child
runs from `neutral_cwd()` (a mono-repo checkout shadows installed packages when a child runs from it); the parent
returns once `/api/meta` answers, raises with the child's last output when it dies first, and says "still starting"
after 30 s. A workspace already answering starts no child.
**Consequences.** Pins: `tests/test_background.py`, `tests/test_browser_open.py`.

## 6. The browser opens on an ANSWER

**Context.** A tab opened before werkzeug binds shows the browser's "cannot connect" page; a restart that opened a tab
each time left one more per edit.
**Decision.** `--browser new` (default of the serving commands) opens a tab only on a server THIS command started,
from a thread (`<name>-browser`) that asks `/api/meta` until it answers, ended and joined when serving returns;
`always` also opens the already-running report's server; `restart` defaults to `never`. A background start's terminal
opens the tab; the child gets `--browser never`.
**Consequences.** The suite records every address (`conftest.browser_opens`). Pins: `tests/test_browser_open.py`.

## 7. `prepare` runs in the terminal, before the registry

**Context.** Measured: a `list` during an 8 s page build pruned the registry entry of the server still building, and
that server was invisible to `list`, `stop` and the MCP tools from then on; a check run in a background child spoke
where nobody read.
**Decision.** `Hooks.prepare` runs in `serve_workspace` before `register_server` — for `--background` in the terminal,
and the child is handed `spec.env("prepared")` so it does not run it again. `Hooks.starting` runs in the serving
process before the app answers. A port already taken prepares nothing.
**Consequences.** Pins: `tests/test_background.py::TestWhatTheChildIsAskedToDo`.

## 8. One way out — and the pages are told first

**Context.** Before, a stop left every tab looking alive until a click failed; SIGTERM and SIGHUP ended the process at
once, losing unsaved work.
**Decision.** `Server.serve` (on the main thread) maps SIGTERM and SIGHUP to Ctrl+C's way out. Its `finally` calls
`tell_pages_leaving` FIRST — `leaving {why, pid, kept_in, kept_until}` down every `/api/events` stream, at most a
second, never raising — then `Hooks.leaving`. `why` is `exit` unless `<name> restart` POSTed
`/api/leaving {"why": "restart"}` before its SIGTERM, and only when the workspace comes back on the same host and
port. The page listens through ONE shared worker per server (six HTTP/1.1 connections per server for ALL tabs),
falling back to a dedicated worker when the shared one never opened while the page's own request answers (an
embedded browser pane keeps shared workers off the network). A drop with no word shows "not answering" after three
seconds, hidden when the stream answers again.
**Consequences.** A kill -9 cannot tell anything. Pins: `tests/test_pages_told_when_leaving.py`.

## 9. ⏻ is the stop's own signal; ↻ is the restart command, launched

**Context.** A second shutdown path (an `os._exit`, a socket close) would skip what the one way out does; a restart
logic of the page's own would drift from the command line's.
**Decision.** `POST /api/exit` answers `Server.exiting()` FIRST, then `call_on_close(stop_serving)` sends this process
SIGTERM. `POST /api/restart` answers `restarting()` (`{pid, log}`), then launches `python -m <cli_module> restart
<port> --background` in a session of its own, from `neutral_cwd()`, its output in `<name>-restart.log`. Both are
refused (400) unless `serve` holds the signal handler (`_ends_on_sigterm`): a server inside another program is never
signalled. The page asks only before losing a SAVED workspace's changes, replaces itself BEFORE `window.close()`
(Chromium refuses to close a tab with history), and after ↻ polls `/api/meta` until its `pid` changes.
**Consequences.** Pins: `tests/test_exit.py`, `tests/test_restart_button.py` (each with a real child process).

## 10. A restart continues a never-saved workspace — when the app says how

**Context.** Measured on an app with this lifecycle: a restart of a never-saved workspace came back EMPTY in a new
temp folder, and the next start removed the old folder with the person's work in it.
**Decision.** With `Hooks.continued`, a restart that names a never-saved workspace — bare (the last served was one),
by its port, or by the type of the one running or served last — continues it in its own folder. The serving process
records it (`unsaved-workspaces.json`, most recent first, only when it binds); `last_unsaved` walks past untouched
stopped ones (`Hooks.holds_work`) and gives up when the head's folder is gone or EMPTIED (a Save moved it — an older
one is not "the one served last"). A running one is stopped first (it keeps its work on its way out); a stopped one
is refused when a SAVED workspace serves its old port now; `--port` moving it stops what serves the destination,
and a foreign holder there is refused before anything stops. `--background` hands the folder down in
`spec.env("continue_folder")`, popped by the first start reading it. A stop says where the work is and which restart
continues it; a fresh start warns once and starts.
**Consequences.** Without the hook nothing is recorded, kept or continued. What the folder holds, and the refusal of a
document the app cannot load, are the app's (`serving.continue_command` names the restart that reaches it). Pins:
`tests/test_continue_unsaved.py`.

## 11. Every address the page uses is relative

**Context.** A path proxy serves an app under a prefix (`/proxy/8710/`); an absolute `/api/meta` leaves it.
**Decision.** The shell's files are served under `/portaide/` (never an app's `/static`), and `shell.js` resolves every
address against its own location, one level up; the worker gets the stream's address from the page.
**Consequences.** Pins: `tests/test_shell.py::TestEveryAddressIsRelative`.

## 12. Strict options

**Context.** Measured: `stop --port 8799` — an option `stop` did not have — fell through to the config overrides and
stopped the only running server.
**Decision.** The app builds its CLI with `LiquifyApp(strict_flags=True)` (its choice; portaide's docs recommend it):
an undeclared option is refused naming the closest one.
**Consequences.** Pins: `tests/test_cli.py::TestAnUnknownOptionIsRefused`.

## 13. The MCP server is a client of the served page

**Context.** What an app's person opened, edited and is looking at lives only in the serving process. An MCP server
that rebuilt the workspace from its file would answer about a different workspace than the one on screen.

**Decision.** `portaide.mcp` is a base for an MCP server that RELAYS to the page's HTTP API (`ServerChoice.call`).
The process always starts, never starts the app, and answers every failure in-band (`{"error": ...}`); with nothing
running the error names the app's start command (`start_hint`). Servers are found by `portaide.serving.running_servers`
— the same identity-based discovery `stop` / `restart` / `list` use — imported at call time.

**Consequences.** A tool is a few lines: a route and its arguments. A tool reaches a client only when the client
reconnects the MCP process; restarting the web app does not refresh it. The page's routes are the one API both the
browser and the agent use, so the agent sees what the person sees.

## 14. The user picks the server, through the SDK's resolvers

**Context.** Several servers of one app may run. Picking one by a rule (the lowest port) talks to the wrong workspace
whenever the person works in another, silently.

**Decision.** `ServerChoice` settles a `Target` per call: a `port` argument names a server and becomes the
connection's; else the one chosen earlier while it runs (same host, port AND identity); else several running → the
client's user is asked, one running → used after a one-time "connect?" whose "always" answer is kept per client name
in `<state folder>/mcp-clients.json`. A client that cannot ask is refused while several run (naming the ports) and
connects when one runs. The question is a `Resolve`/`Elicit` resolver pair, never `ctx.elicit()`; the target
parameter (`Annotated[Target, choice.resolve]`) is filled by the resolvers and never advertised.

**Consequences.** The same question works on the older protocol (a request mid-call) and the newer one (an
input-required result and a retry) — the SDK carries both, so it is asked once. The choice lives on the `ServerChoice`
instance — one MCP process per client connection — so a reconnect starts with none, and two apps in one process keep
their own. A state folder that cannot be written loses the "always", never the connection.

## 15. The advertised schemas are LLM-safe, the capabilities tools-only

**Context.** Strict function-calling APIs (an OpenAPI-3.0 subset) reject `$ref`, `const`, `additionalProperties`;
and the SDK advertises resources and prompts unconditionally, which baits clients into `resources/list` and makes some
models report the server as not connected.

**Decision.** `LLMSafeMCPServer` (MCP SDK 2.x `mcp.server.mcpserver.MCPServer`) runs every advertised
`input_schema` / `output_schema` through `confluid.sanitize_schema`, and drops the unimplemented request handlers,
deriving each method name from `request_type.model_fields["method"].default` so the tuple of request types is the one
source.

**Consequences.** Real calls are still validated against the original argument model. The subclass is one of several
copies in the workspace's MCP servers; hoisting it into liquifai is tracked in `TASKS.md`.

## 16. `mcp` is an optional extra

**Context.** Most uses of portaide (serving, the lifecycle commands, the page's shell) need no MCP SDK, and the SDK
pins a major version that has moved module paths before.

**Decision.** `portaide[mcp]` carries `mcp>=2.0`; `portaide/mcp.py` imports it at module scope and nothing else in
portaide imports `portaide.mcp`. portaide imports no LLM package at all (`tests/test_llm_free.py`).

**Consequences.** An app without the extra never loads the SDK; an app with an MCP server imports `portaide.mcp` from
its own server module only.

## 17. One filter language, with the app's flags declared

**Context.** A records list is searched by what each record carries (`class = 7`, `class has 3, 7`, `snr 5..20`,
`class != @pred`) and by state the app keeps beside the record (whether the person edited it). The entries are typed
items of whatever data library the app uses; the state words differ per app.

**Decision.** `portaide.query.parse(text, aliases=None, flags=None)` reads one grammar into a `Filter` applied to
`(record, state)`. Entries are unwrapped duck-typed (`values` / `labels`, else `value`, else the entry:
`entry_values`), so portaide depends on no data library. FLAGS are a parameter: a mapping of word → state key,
`!key` for false (`flag_pairs` writes both words of a key); the default declares none, and an undeclared bare word is
a NAME term (`state["name"]`). `numeric` is public so an app decides numeric-ness by the comparison's own rule.

**Consequences.** An app declares its flags once and passes them on every parse; `Filter.keys`, `state_keys` and
`names` tell it exactly which entries, state and names to work out. State never enters the record, so it never reaches
what the app writes back. A declaration naming no state key is refused at parse time.

## 18. The viewer's shapes are structural protocols

**Context.** A records viewer draws canvases of recordings from collections opened from paths; the domain packages
that provide them must not import the app (the app depends on them).

**Decision.** `portaide.protocols` defines `Extent` (a `{"x0","x1","y0","y1"}` dict) and the runtime-checkable
`Canvas`, `Recording`, `Recordings` and `Opener` protocols. `Canvas.tile` is typed `Any` so portaide needs no array
library.

**Consequences.** A domain package conforms by shape alone; `isinstance` checks member presence, never signatures, so
the protocols document the contract and the tests of each implementation hold it.
