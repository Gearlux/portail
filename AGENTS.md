# portail Mandates

Rules only. The reasons are in [`docs/architecture.md`](docs/architecture.md), usage is in the [README](README.md).
Root `AGENTS.md` rules are not repeated here.

## Current state

- **What it is:** the framework a workspace web app is built on. An app serves ONE object (its workspace) behind a
  Flask page and is managed from the command line; portail holds everything that does not depend on what the
  workspace holds: serving (foreground or detached), `stop` / `restart` / `list` by the server's identity, the
  browser opened on an answer, the page's shell (`/api/meta`, ⏻, ↻, every open page told when its server goes), the
  MCP server base, and the filter language a records list is searched with.
- **The seam** (`portail/spec.py`): an app supplies an `AppSpec` (its name, CLI module, default port, state folder,
  `Hooks`) and a workspace implementing the `Served` protocol (`host`, `port`, `path`, `template`, `title`,
  `state_dir()`, `info()`). portail imports nothing of any app.
- **Module map:** `spec` · `errors` · `serving` · `cli` · `web` (the shell, `pages`) · `web/static/` (vanilla JS, no
  build step) · `mcp` · `query` · `protocols`.

## Rules

- **portail names no app built on it** — not in code, tests or docs (`tests/test_no_private_names.py`). An example
  uses the demo app of the tests.
- **Every message names the app by `AppSpec.name`**, every file by the spec's state folder, every environment
  variable through `AppSpec.env` — never a literal app name.
- **A server is found by its IDENTITY, from the LIVE `/api/meta` answer** (`app` = the spec's name), never from the
  registry copy alone, and a stop signals only the pids LISTENING on the ports of the servers it stops.
- **Hooks never raise into serving**: a hook that fails is logged and serving goes on.

## Serving and the lifecycle commands

Usage: [`docs/serving.md`](docs/serving.md); reasons: [`docs/architecture.md`](docs/architecture.md) §2-12.

- **A stop never signals what does not answer as the app.** The fallback of a stop compares the IDENTITY (file, else
  `type:<template>`) the live `/api/meta` gives with what was named; a listener that does not answer as `spec.name` is
  refused (`refuse_a_foreign_holder`, the one wording for `stop` and `restart <port>`), never SIGTERMed — with no
  identity to compare too. (`tests/test_stop_choice.py::TestAPortServingSomethingElseIsRefused`,
  `::TestAStopSignalsOnlyTheServersItStops`, `::TestRestartingAPortSomethingElseHolds`) arch §2.
- **Pids come from `ss`, else `lsof`, and only for the ports stopped** — never `ps`; a lookup no tool can make is an
  error, never "nothing listens". (`tests/test_pids_listening.py`, `tests/test_stop_choice.py::TestARealProcessThatBindsNoPortOutlivesTheStop`) arch §3.
- **A port names a server exactly as its workspace does**; a file or type spelled like a number wins.
  (`tests/test_stop_choice.py::TestAPortNumberNamesTheServerOnIt`)
- **The port-conflict ask lives in the CLI side (`serving.resolve_port_conflict`), never in `Server`.** A saved
  workspace keeps its port (another workspace asks `[Y/n]`, a foreign program is `serve()`'s located error); a type
  workspace takes the next free port unless `--port` names it; a busy port is never registered.
  (`tests/test_auto_port.py`) arch §4.
- **`--background`: the conflict settles before the fork, the child's argv is REBUILT, it runs from `neutral_cwd()`,
  the parent returns only on an answer; a workspace already answering starts no child.** (`tests/test_background.py`,
  `tests/test_browser_open.py::TestABackgroundStartWhileTheSameWorkspaceRuns`) arch §5.
- **The browser opens only on an ANSWER**; `--browser` defaults `new` (serving) / `never` (restart); the suite never
  opens a real tab (`conftest.browser_opens`). (`tests/test_browser_open.py`) arch §6.
- **`Hooks.prepare` runs in the terminal before `register_server`** (a `-b` child is handed `spec.env("prepared")`);
  `Hooks.starting` before the app answers. (`tests/test_background.py::TestWhatTheChildIsAskedToDo`) arch §7.
- **One way out:** SIGTERM / SIGHUP take Ctrl+C's path; the `finally` tells the pages FIRST, then `Hooks.leaving`;
  never a second shutdown path. (`tests/test_pages_told_when_leaving.py::TestServeTellsThePagesOnEveryWayOut`) arch §8.
- **⏻ is SIGTERM to itself, ↻ is `<name> restart <port> --background` launched** — each after its answer is sent, both
  refused unless `serve()` holds the handler (`_ends_on_sigterm`). (`tests/test_exit.py`,
  `tests/test_restart_button.py`) arch §9.
- **Every page hears `leaving`** through ONE shared worker per server (dedicated-worker fallback); `why=restart` only
  when the workspace comes back on the same host and port. (`tests/test_pages_told_when_leaving.py`) arch §8.
- **A never-saved workspace is continued only through `Hooks.continued`**; the record is written by the process that
  binds; an emptied head ends the walk; a saved workspace's port is never taken by a stopped one unless `--port` names
  it. (`tests/test_continue_unsaved.py`) arch §10.
- **Every address the page uses is RELATIVE**; the shell's files live under `/portail/`.
  (`tests/test_shell.py::TestEveryAddressIsRelative`) arch §11.
- **The suite never reaches a live server**: every request of the lifecycle goes through `serving._urlopen`, which
  `conftest._no_live_http` closes; a test that needs HTTP opens it itself. Real processes in tests run on free ports
  with their own HOME — never a port of the operator's, never a stop of a process the test did not start.

## The MCP server base, the filter language, the protocols

Usage: [`docs/mcp.md`](docs/mcp.md), [`docs/query.md`](docs/query.md); reasons: `docs/architecture.md` §13-18.

- **The MCP server is a CLIENT of the served page:** every tool answers in-band and never raises, never starts the
  app, and the process always starts. The user picks the server — `port`, else the SDK's `Resolve`/`Elicit` question,
  never `ctx.elicit()`; an "always" answer is kept per client name in `spec.state_dir() / "mcp-clients.json"`.
  (`tests/test_mcp.py`) arch §13-14.
- **`mcp` is the `[mcp]` extra:** only `portail/mcp.py` imports the SDK, nothing in portail imports `portail.mcp`,
  and nothing imports `anthropic` / `pydantic_ai`. (`tests/test_llm_free.py`) arch §16.
- **The filter language has no flags of its own:** an app declares its flag words (`flag_pairs`) mapped to state keys.
  (`tests/test_query.py`) arch §17.
- **The protocols are structural** (`typing.Protocol`): a domain implements them without importing portail.
  (`tests/test_protocols.py`) arch §18.
