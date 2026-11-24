# portail

The framework a workspace web app is built on. An app serves ONE object — its *workspace* — behind a Flask page and
is managed from the command line. portail holds everything that does not depend on what the workspace holds:

- **serving** in the foreground or detached (`--background`), the port settled before anything starts, the browser
  opened once the server answers;
- **`stop` / `restart` / `list`** by the server's IDENTITY (its live `/api/meta` answer), never by a port alone;
- **the page's shell**: `/api/meta`, the ⏻ and ↻ buttons, and every open page told when its server goes;
- the MCP server base ([docs/mcp.md](https://github.com/Gearlux/portail/blob/main/docs/mcp.md)) and the filter
  language a records list is searched with ([docs/query.md](https://github.com/Gearlux/portail/blob/main/docs/query.md)).

What each lifecycle command does, in user terms:
[docs/serving.md](https://github.com/Gearlux/portail/blob/main/docs/serving.md). Why it works this way:
[docs/architecture.md](https://github.com/Gearlux/portail/blob/main/docs/architecture.md).

## Install

```bash
pip install portail          # serving, the CLI commands, the page's shell
pip install "portail[mcp]"   # + the MCP server base
```

## Quickstart: an app on portail

### 1. Say who the app is — `AppSpec`

```python
from portail import AppSpec, Hooks

SPEC = AppSpec(
    name="demoapp",               # every message, file and log name: `demoapp stop`, demoapp.log
    cli_module="demoapp.cli",     # what `python -m` runs for a --background child and the page's ↻
    default_port=8710,
    hooks=Hooks(),                # optional, see below
)
```

The per-user state (the server registry, the last served workspace) lives in `~/.demoapp` unless `state_folder`
names another folder; environment variables are named through `SPEC.env("continue_folder")` →
`DEMOAPP_CONTINUE_FOLDER`.

### 2. Implement `Served` — the workspace as portail sees it

```python
class DemoWorkspace:
    def __init__(self, path: str = "", template: str = "", port: int = 8710) -> None:
        self.host, self.port, self.path, self.template, self.title = "127.0.0.1", port, path, template, "workspace"

    def state_dir(self) -> Path: ...             # its folder (a never-saved one: a temp folder); cheap, never raises

    def info(self) -> dict:                      # what /api/meta answers under "workspace"
        return {"path": self.path, "template": self.template, "state_dir": str(self.state_dir()), "dirty": False}
```

`path` is the workspace's file (absolute, `""` when never saved), `template` the type it was opened from (`""` for
none). `info()["dirty"]` with a `path` makes ⏻ and ↻ ask before losing unsaved changes.

### 3. Add the lifecycle commands to the app's own CLI

```python
from liquifai import LiquifyApp
from portail.cli import add_lifecycle_commands

app = LiquifyApp(name="demoapp", description="The demo workspace app.", strict_flags=True)
lifecycle = add_lifecycle_commands(
    app,
    SPEC,
    open_file=open_file,              # (absolute path) -> Served; raise a LiquifaiError for a file it refuses
    open_empty=DemoWorkspace,         # () -> Served
    open_type=open_type,              # (type name) -> Served; leave out for an app without types
    type_names=lambda: ["classification", "detection"],
    create_app=create_app,            # (Server) -> Flask; leave out to serve the bare shell
)

if __name__ == "__main__":
    app.run()
```

This registers `demoapp [workspace.yaml]` (the default command), `demoapp workspace <type>`, `demoapp stop [target]
[--all]`, `demoapp restart [target]` and `demoapp list`, with `--auto-port`, `--port`, `--background`/`-b` and
`--browser always|new|never` on the serving commands. `strict_flags=True` is the app's choice — and the recommended
one: a misspelled option is then refused instead of silently dropped.

### 4. Mount the shell on the app's Flask app

```python
from flask import Flask
from portail.serving import Server
from portail.web import mount_shell

def create_app(server: Server) -> Flask:
    app = Flask("demoapp", static_folder=None)

    @app.get("/")
    def index():
        return PAGE   # the app's own page

    return mount_shell(app, server)   # AFTER the app's own "/" — the shell's minimal page only fills a gap
```

The page loads the shell with relative addresses and names where ⏻ and ↻ go:

```html
<link rel="stylesheet" href="portail/shell.css">
<header id="top">…</header>
<script src="portail/shell.js"></script>
<script>Portail.start({ buttons: "#top" });</script>
```

`Portail.beforeLeaving = async (what) => true` lets the page ask its own question before ⏻ / ↻ (return `false` to
stay); `Portail.toast` is how a refusal is said. `create_shell(server)` is a Flask app holding only the shell.

## Hooks

| hook | runs | for |
| --- | --- | --- |
| `prepare(served)` | in the TERMINAL, before the server registers or a `--background` child starts | checks the person must read, a page to build |
| `starting(served)` | in the serving process, before the page answers | startup jobs, a cleanup |
| `leaving(served)` | on every way out, after the open pages were told | keep unsaved work |
| `holds_work(folder)` | when a never-saved workspace's folder is looked at | whether a restart should continue it |
| `kept_until()` | in messages | how long such work is kept, in words |
| `continued(folder, template)` | on a restart that continues a never-saved workspace | open it from its folder |

A hook that fails is logged and serving goes on. Without `continued` a restart never continues a never-saved
workspace: it opens what it names.

## For a Python caller

`portail.serving.running_servers(spec)` lists every server of the app that answers right now, lowest port first —
each entry carries `host`, `port`, `label`, `identity`, `path`, `template`, `state_dir` and `pid`.
`portail.serving.post_json` / `get_json` talk to one of them, with every failure one `ServeError` line.

## The MCP server base, the filter language, the protocols

- **MCP server base** (`pip install portail[mcp]`): `portail.mcp.make_server(spec, instructions)` and
  `ServerChoice(spec, start_hint=...)` give an app tools that relay to its running page. The user picks the server —
  a `port` argument, a question to the user, or a kept "always" answer — and every failure answers in-band. See
  [docs/mcp.md](https://github.com/Gearlux/portail/blob/main/docs/mcp.md).
- **Filter language**: `portail.query.parse(text, aliases=..., flags=...)` reads `class = 7`, `class has 3, 7`,
  `snr 5..20`, `class != @pred`, a name search, and the app's own flag words (`flag_pairs("edited")`). See
  [docs/query.md](https://github.com/Gearlux/portail/blob/main/docs/query.md).
- **Protocols**: `portail.protocols` holds the structural `Canvas`, `Recording`, `Recordings` and `Opener` a domain
  package implements without importing the app.

## License

MIT — see [LICENSE](https://github.com/Gearlux/portail/blob/main/LICENSE).
