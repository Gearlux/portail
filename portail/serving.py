"""Serving an app's workspace — in the foreground or detached — and finding, stopping and restarting it again.

Everything here is parameterised by the app's :class:`~portail.spec.AppSpec`: its name is in every message and file
name, its state folder holds the registry (``<state>/servers/<port>.json``), the last served workspace
(``<state>/last-workspace``) and the never-saved workspaces served (``<state>/unsaved-workspaces.json``).

A server is found by its IDENTITY, read from its LIVE ``/api/meta`` answer (``{"app": spec.name, "workspace": …}``),
never from the registry copy alone; a stop signals only the pids LISTENING on the ports of the servers it stops
(:func:`pids_listening`), and a listener that does not answer as the app is never signalled.

:class:`Server` serves one :class:`~portail.spec.Served` workspace behind the app's Flask app (the page's shell —
``portail.web`` — mounted in it): its way out (Ctrl+C, SIGTERM, SIGHUP, the page's ⏻) tells every open page first,
then runs the app's ``leaving`` hook. The command-line side (:func:`serve_workspace`) settles the port, registers the
server, opens the browser on an answer and starts a ``--background`` child.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Tuple, Union, cast, get_args

from loggair import get_logger

from portail.errors import PortInUseError, ServeError
from portail.spec import AppSpec, Served

if TYPE_CHECKING:  # annotation only: the web package imports this module
    from flask import Flask

logger = get_logger(__name__)

#: What ``--browser`` takes: ``new`` opens the default browser on the address once a server THIS command started
#: answers, ``always`` also opens the address of a server of the app already answering there, ``never`` opens none.
#: The serving commands default to ``new``; ``restart`` defaults to ``never`` (the edit-and-restart loop would leave
#: one more tab per restart beside the one still open).
BrowserChoice = Literal["always", "new", "never"]
BROWSER_CHOICES: Tuple[str, ...] = get_args(BrowserChoice)

#: The file in the state folder that remembers what was last served (``empty`` / ``type:<name>`` / ``file:<path>``),
#: so a bare ``restart`` / ``stop`` mean "the workspace I am working on".
LAST_WORKSPACE = "last-workspace"

#: Beside :data:`LAST_WORKSPACE`: the NEVER-SAVED workspaces served, most recent first — each one's folder, type, host
#: and port, written by the process that serves it (:func:`record_unsaved`). What lets ``restart`` continue a
#: never-saved workspace in its own folder, and a stop or a fresh start say where its work is.
UNSAVED_RECORD = "unsaved-workspaces.json"

#: How many never-saved workspaces :data:`UNSAVED_RECORD` remembers — enough to walk past a few untouched starts.
_UNSAVED_KEPT = 20

#: How often a foreground start's browser thread asks the server whether it answers yet (seconds).
_BROWSER_POLL_SECONDS = 0.2

#: How long a background start waits for its child to answer before reporting it as still coming up. Generous: a
#: first serve may import heavy packages.
_BACKGROUND_WAIT_SECONDS = 30.0

#: How much of a dead background child's output rides in the error — the tail, where the cause is.
_BACKGROUND_TAIL_LINES = 15

#: How long a stop waits for the listeners it signalled before it sends SIGKILL (seconds).
_STOP_WAIT_SECONDS = 5.0

#: The environment words (``AppSpec.env``) a start hands to the child it starts.
CONTINUE_FOLDER = "continue_folder"
PREPARED = "prepared"


@dataclass
class Address:
    """Where a server runs, as a stop sees it: its host and port."""

    host: str
    port: int


# -- names ------------------------------------------------------------------------------------------------------------


def browser_thread(spec: AppSpec) -> str:
    """The name of the thread a foreground start waits on its own server with, before it opens the browser."""
    return f"{spec.name}-browser"


def server_log(spec: AppSpec) -> str:
    """The log file a background server writes (its loggair log, named by the app)."""
    return f"{spec.name}.log"


def restart_log(spec: AppSpec) -> str:
    """What the page's ↻ launches prints — the restart's own report, and the start-up error of a server that did not
    come back — in :func:`log_dir`. One file, rewritten by each restart: only the last one is asked about."""
    return f"{spec.name}-restart.log"


def kind(template: str) -> str:
    """How a never-saved workspace is named in a message: ``signal type workspace`` / ``empty workspace``."""
    return f"{template} type workspace" if template else "empty workspace"


def label_of(path: str, template: str) -> str:
    """How a workspace is named in a listing: its file, else ``the <type> type workspace``, else an empty one."""
    return path or (f"the {template} type workspace" if template else "an empty workspace")


def workspace_identity(path: str, template: str) -> str:
    """One comparable label per workspace: its resolved file, else ``type:<template>``, else empty."""
    return path or (f"type:{template}" if template else "")


def identity_label(identity: str) -> str:
    """How a workspace identity reads in a message: a type by its name, a file by its path."""
    if identity.startswith("type:"):
        return f"the {identity[5:]} type workspace"
    return identity or "the workspace"


# -- where a detached child runs and logs ---------------------------------------------------------------------------


def neutral_cwd() -> str:
    """A directory to run a detached child from (``--background``, the page's ↻) — NEVER the server's own.

    In a mono-repo whose project folders match package names, the launch directory decides what an import finds: run
    from the workspace root, ``python -m`` puts the cwd on ``sys.path`` and the child imports the FOLDER, dying with
    ``cannot import name 'LiquifyApp' from 'liquifai' (unknown location)`` (measured). Every path handed to a child is
    absolute, so the working directory carries no other meaning.
    """
    return tempfile.gettempdir()


def log_dir() -> Path:
    """The folder a detached child's loggair writes to — ``LOGGAIR_DIR``, else ``logs`` in the neutral spawn directory
    (:func:`log_path` says why), resolved."""
    configured = os.environ.get("LOGGAIR_DIR", "")
    base = Path(configured).expanduser() if configured else (Path(neutral_cwd()) / "logs")
    return base.resolve()


def log_path(name: str) -> str:
    """Where a detached child's loggair will write — EXPANDED, so the log line is clickable.

    The child inherits this process's environment, and loggair resolves ``LOGGAIR_DIR`` first, falling back to
    ``./logs`` against the child's own cwd (the neutral spawn directory). The ``$``-form alone sent the reader off to
    expand it by hand.
    """
    return f"$LOGGAIR_DIR/{name}: {log_dir() / name}"


# -- the probe --------------------------------------------------------------------------------------------------------


def _urlopen(*args: Any, **kwargs: Any) -> Any:
    """``urllib.request.urlopen`` — the one door every request of the lifecycle goes through (a test suite closes it,
    so no test ever reaches a live server)."""
    return urllib.request.urlopen(*args, **kwargs)


def port_in_use(host: str, port: int) -> bool:
    """Whether ``host:port`` cannot be bound right now — something listens there."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def port_holder(port: int) -> str:
    """Best effort: who listens on ``port`` (``lsof``), as ``" (pid 123 python)"``; empty when unknown."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"], capture_output=True, text=True, timeout=3
        ).stdout
    except Exception:  # noqa: BLE001 - best effort: an unknown holder is said as such
        return ""
    pid, cmd = "", ""
    for line in out.splitlines():
        if line.startswith("p") and not pid:
            pid = line[1:]
        elif line.startswith("c") and not cmd:
            cmd = line[1:]
    return f" (pid {pid} {cmd})".rstrip() if pid else ""


def meta_at(host: str, port: int) -> Optional[Dict[str, Any]]:
    """The JSON object ``GET /api/meta`` answers on ``host:port`` — None when nothing answers one (within a second)."""
    try:
        with _urlopen(f"http://{host}:{port}/api/meta", timeout=1.0) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - no answer, an error page, a non-JSON body: none of them is a server of ours
        return None
    return dict(body) if isinstance(body, dict) else None


def app_at(spec: AppSpec, host: str, port: int) -> Optional[Dict[str, Any]]:
    """The ``/api/meta`` of a server of THIS app already serving on ``host:port``, or None.

    "The port is taken" and "the app is already running" are different events with different remedies, and only the
    probe can tell them apart: this asks the holder whether it answers as ``spec.name`` (``{"app": spec.name}``).
    Anything else — no answer, a different app, a different JSON — is None, and the caller reports a taken port.
    """
    meta = meta_at(host, port)
    if meta is None or meta.get("app") != spec.name:
        return None
    return meta


# -- the registry -----------------------------------------------------------------------------------------------------


def servers_dir(spec: AppSpec) -> Path:
    """The folder holding one JSON per port a server of the app serves on."""
    return spec.state_dir() / "servers"


def register_server(spec: AppSpec, served: Served) -> None:
    """Record the LIVE port this process serves on — what lets ``stop`` find every server.

    One JSON per port, written right before the server binds and removed when it returns; a crash leaves a stale file,
    which discovery prunes by probing. State is a convenience — serving never fails on it.
    """
    try:
        directory = servers_dir(spec)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{served.port}.json").write_text(
            json.dumps(
                {
                    "host": served.host,
                    "port": served.port,
                    "path": served.path,
                    "template": served.template,
                    "pid": os.getpid(),
                }
            )
        )
    except OSError as exc:
        logger.debug(f"could not register the server: {exc}")


def unregister_server(spec: AppSpec, served: Served) -> None:
    """Remove the registry entry :func:`register_server` wrote. Never raises."""
    try:
        (servers_dir(spec) / f"{served.port}.json").unlink(missing_ok=True)
    except OSError as exc:
        logger.debug(f"could not unregister the server: {exc}")


def server_entry(host: str, port: int, meta: Dict[str, Any]) -> Dict[str, Any]:
    """One discovered server, its identity taken from its LIVE meta answer.

    Keys: ``host``, ``port``, ``path``, ``template``, ``identity``, ``label``, ``state_dir`` (the folder it works out
    of — a never-saved workspace's is what a restart continues), ``pid`` (the serving process, 0 when unsaid) and
    ``options`` (the app's own serving options it runs with).
    """
    info = meta.get("workspace") or {}
    path, template = str(info.get("path") or ""), str(info.get("template") or "")
    try:
        pid = int(meta.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    return {
        "host": host,
        "port": port,
        "path": path,
        "template": template,
        "identity": workspace_identity(path, template),
        "label": label_of(path, template),
        "state_dir": str(info.get("state_dir") or ""),
        "pid": pid,
        "options": as_options(info.get("options")),
    }


def running_servers(spec: AppSpec) -> List[Dict[str, Any]]:
    """Every server of the app actually serving right now, verified by the live meta probe — lowest port first.

    Candidates are the registry entries plus the DEFAULT port (a server started through the Python API never
    registers); each must answer as ``spec.name``, and identity comes from that LIVE answer — never the registry
    copy. A registered port that no longer answers is a leftover and is pruned. Entries: :func:`server_entry`.
    """
    candidates: Dict[Tuple[str, int], Optional[Path]] = {}
    directory = servers_dir(spec)
    if directory.is_dir():
        for entry in sorted(directory.glob("*.json")):
            try:
                data = json.loads(entry.read_text())
                candidates[(str(data.get("host") or spec.default_host), int(data.get("port") or 0))] = entry
            except (OSError, ValueError, AttributeError):
                entry.unlink(missing_ok=True)
    candidates.setdefault((spec.default_host, spec.default_port), None)
    servers = []
    for (host, port), registered in sorted(candidates.items(), key=lambda item: item[0][1]):
        meta = app_at(spec, host, port)
        if meta is None:
            if registered is not None:
                registered.unlink(missing_ok=True)
            continue
        servers.append(server_entry(host, port, meta))
    return servers


def server_on_port(spec: AppSpec, port: int, servers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The server of the app serving on ``port``: a discovered one, else whatever ANSWERS there as the app.

    A server the registry does not know (started through the Python API) is still asked directly on the default
    host — the live meta answer decides, as in every discovery.
    """
    for server in servers:
        if int(server["port"]) == port:
            return server
    meta = app_at(spec, spec.default_host, port)
    return None if meta is None else server_entry(spec.default_host, port, meta)


def answering_at(spec: AppSpec, address: Address) -> List[Dict[str, Any]]:
    """The server of the app answering on ``address``, as a discovered server — or none."""
    meta = app_at(spec, address.host, address.port)
    return [] if meta is None else [server_entry(address.host, address.port, meta)]


# -- the last served workspace ----------------------------------------------------------------------------------------


def remember(spec: AppSpec, what: str) -> None:
    """Record what is being served (``empty`` / ``type:<name>`` / ``file:<abs path>``). Never raises."""
    try:
        state = spec.state_dir()
        if not state.exists():
            state.mkdir(parents=True)
        (state / LAST_WORKSPACE).write_text(what + "\n")
    except OSError as exc:  # state is a convenience — serving must never fail on it
        logger.debug(f"could not record the last workspace: {exc}")


def remember_workspace(spec: AppSpec, served: Served) -> None:
    """:func:`remember` the workspace about to be served: its type, else its file, else an empty one."""
    if served.template:
        remember(spec, f"type:{served.template}")
    elif served.path:
        remember(spec, f"file:{served.path}")
    else:
        remember(spec, "empty")


def last_spec(spec: AppSpec) -> str:
    """What :func:`remember` recorded last — ``""`` when nothing was served yet."""
    state = spec.state_dir() / LAST_WORKSPACE
    try:
        return state.read_text().strip() if state.is_file() else ""
    except OSError:
        return ""


def never_saved_spec(what: str) -> bool:
    """Whether a remembered spec is a NEVER-SAVED workspace (a type or an empty one) — what a bare restart continues."""
    return what.startswith("type:") or what == "empty"


# -- never-saved workspaces -------------------------------------------------------------------------------------------


def continues(spec: AppSpec) -> bool:
    """Whether a restart continues a never-saved workspace in its own folder — the app gave ``Hooks.continued``."""
    return spec.hooks.continued is not None


def _same_folder(one: Union[str, Path], other: Union[str, Path]) -> bool:
    try:
        return Path(one).resolve() == Path(other).resolve()
    except OSError:  # pragma: no cover - a path that cannot be resolved names no folder of ours
        return False


def still_there(folder: Union[str, Path]) -> bool:
    """Whether a recorded never-saved workspace's folder is still one — there, and not EMPTIED.

    An app's first Save may move every file of the folder to the saved file's own folder and leave the folder itself
    behind, empty; a purge or a startup cleanup removes it. A folder a workspace still works out of is never empty.
    """
    path = Path(folder)
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:  # pragma: no cover - unreadable: not a folder a restart could continue
        return False


def unsaved_entries(spec: AppSpec) -> List[Dict[str, Any]]:
    """:data:`UNSAVED_RECORD`, most recent first — ``[{folder, template, host, port}]``; unreadable reads as empty."""
    record = spec.state_dir() / UNSAVED_RECORD
    if not record.is_file():
        return []
    try:
        loaded = json.loads(record.read_text())
    except (OSError, ValueError):
        return []
    return (
        [dict(entry) for entry in loaded if isinstance(entry, dict) and entry.get("folder")]
        if isinstance(loaded, list)
        else []
    )


def record_unsaved(spec: AppSpec, served: Served) -> None:
    """Remember the never-saved workspace THIS process is about to serve — its folder, at the head of the record.

    Written by the process that serves it — for ``--background`` the child — and only when it actually binds the
    port. Entries whose folder is gone are dropped; the same folder is never listed twice. Nothing is recorded for an
    app whose restarts continue nothing (:func:`continues`). State is a convenience: serving never fails on it.
    """
    if not continues(spec):
        return
    try:
        folder = served.state_dir()
        entries = [
            entry
            for entry in unsaved_entries(spec)
            if still_there(entry["folder"]) and not _same_folder(entry["folder"], folder)
        ]
        entries.insert(
            0, {"folder": str(folder), "template": served.template, "host": served.host, "port": served.port}
        )
        state = spec.state_dir()
        if not state.exists():
            state.mkdir(parents=True)
        (state / UNSAVED_RECORD).write_text(json.dumps(entries[:_UNSAVED_KEPT], indent=1) + "\n")
    except OSError as exc:
        logger.debug(f"could not record the never-saved workspace: {exc}")


def never_saved(server: Dict[str, Any]) -> bool:
    """Whether a discovered server serves a NEVER-SAVED workspace whose folder is there — one a restart can continue."""
    folder = str(server.get("state_dir") or "")
    return not server.get("path") and bool(folder) and Path(folder).is_dir()


def holds_work(spec: AppSpec, folder: Path) -> bool:
    """``Hooks.holds_work``, never raising: a hook that fails reads as "no work"."""
    try:
        return bool(spec.hooks.holds_work(folder))
    except Exception as exc:  # noqa: BLE001 - a hook never raises into serving
        logger.warning(f"{spec.name}: the holds_work hook failed on {folder}: {exc}")
        return False


def kept_until(spec: AppSpec) -> str:
    """``Hooks.kept_until``, never raising: a hook that fails says nothing."""
    try:
        return str(spec.hooks.kept_until())
    except Exception as exc:  # noqa: BLE001 - a hook never raises into serving
        logger.warning(f"{spec.name}: the kept_until hook failed: {exc}")
        return ""


def _kept(spec: AppSpec) -> str:
    """`` kept_until`` with its leading space, or nothing."""
    until = kept_until(spec)
    return f" {until}" if until else ""


def last_unsaved(spec: AppSpec, servers: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """The never-saved workspace served last that is still worth continuing — its record entry plus ``server``.

    The most recent entry of :data:`UNSAVED_RECORD` whose folder is there and that either RUNS (``server`` = its live
    discovery entry) or holds work its stop left behind (``Hooks.holds_work``; ``server`` = ``None``). An untouched
    one that stopped holds nothing to continue and is walked past, which is what makes a fresh start's warning true:
    start a new one, stop it untouched, and a bare restart continues the one with the work.

    When the folder of the one served LAST is gone or emptied (:func:`still_there`), nothing is continued: a Save may
    move a never-saved workspace's files away, and an older entry is not "the one served last". Measured before: after
    the page saved a workspace file, a bare restart continued an OLDER never-saved workspace onto its port — stopping
    the server of the file just saved.
    """
    if not continues(spec):
        return None
    recorded = unsaved_entries(spec)
    if not recorded or not still_there(recorded[0]["folder"]):
        return None
    entries = [entry for entry in recorded if still_there(entry["folder"])]
    if servers is None:
        servers = running_servers(spec)
    running = [server for server in servers if never_saved(server)]
    for entry in entries:
        server = next((s for s in running if _same_folder(entry["folder"], s["state_dir"])), None)
        if server is not None or holds_work(spec, Path(entry["folder"])):
            return {**entry, "server": server}
    return None


def warn_about_work_left_behind(spec: AppSpec) -> None:
    """A FRESH never-saved start: say so when the never-saved workspace served last holds work and is not running.

    Then the start goes on — no question asked. The advice is true by construction: stopped untouched, the new one is
    walked past by :func:`last_unsaved`, and a bare restart continues the one named here.
    """
    last = last_unsaved(spec)
    if last is None or last["server"] is not None:
        return
    name = spec.name
    print(
        f"{name}: the never-saved {kind(str(last.get('template') or ''))} served last holds work in "
        f"{last['folder']}, kept{_kept(spec)} — starting a new one; to continue that one instead, stop this"
        f" one ('{name} stop') and run '{name} restart'"
    )


def continue_command(spec: AppSpec, folder: Path, template: str) -> str:
    """The restart that continues the never-saved workspace in ``folder`` — ``""`` when none reaches it.

    Only the one a restart would continue (:func:`last_unsaved`): a bare ``<name> restart`` while the last served
    workspace was a never-saved one, else ``<name> restart <type>``. An app's own refusal of a document it cannot load
    may name it.
    """
    last = last_unsaved(spec)
    if last is None or not _same_folder(last["folder"], folder):
        return ""
    if never_saved_spec(last_spec(spec)):
        return f"{spec.name} restart"
    return f"{spec.name} restart {template}" if template else ""


def never_saved_note(spec: AppSpec, server: Dict[str, Any]) -> str:
    """What ``stop`` adds after stopping a never-saved workspace with work: where it is, and what continues it.

    ``""`` for a saved workspace, an untouched one (nothing was kept), or one no restart would reach.
    """
    if not continues(spec) or not never_saved(server):
        return ""
    folder = Path(str(server["state_dir"]))
    if not holds_work(spec, folder):
        return ""
    template = str(server.get("template") or "")
    command = continue_command(spec, folder, template)
    if not command:
        return ""
    return (
        f"{spec.name}: the {kind(template)} was never saved — its work is kept in {folder}{_kept(spec)};"
        f" '{command}' continues it"
    )


def handed_down_folder(spec: AppSpec, fresh: str) -> Optional[Path]:
    """The never-saved workspace's folder a ``--background`` restart handed to THIS start.

    Read from ``spec.env("continue_folder")`` and removed from the environment as it is read: it is meant for this
    start only (an app whose restarts continue nothing drops it).

    Args:
        fresh: The command that starts a NEW one instead — what the refusal tells the user to type.

    Raises:
        ServeError: the folder handed down is gone — nothing to continue.
    """
    given = os.environ.pop(spec.env(CONTINUE_FOLDER), None)
    if not given or not continues(spec):
        return None
    folder = Path(given)
    if not folder.is_dir():
        raise ServeError(
            f"the never-saved workspace's folder {folder} handed to this start is gone — nothing to continue;"
            f" '{fresh}' starts a new one"
        )
    return folder


# -- who listens, and the stop ----------------------------------------------------------------------------------------


def _ss_pids(port: int) -> Optional[List[int]]:
    """Pids ``ss`` shows LISTENING on the port; None when ``ss`` cannot say.

    ``ss`` reads the kernel's socket table, so it answers at once where ``lsof`` (which walks every process's open
    files) hangs on a stuck mount. A listener shown without a pid belongs to a process ``ss`` may not see: that is
    "cannot say", not "nothing".
    """
    out = subprocess.run(
        ["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=5, check=True
    ).stdout
    lines = [line for line in out.splitlines() if line.strip()]
    pids = sorted({int(pid) for line in lines for pid in re.findall(r"pid=(\d+)", line)})
    return pids if pids or not lines else None


def _lsof_pids(port: int) -> List[int]:
    out = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"], capture_output=True, text=True, timeout=5
    ).stdout
    return sorted({int(line[1:]) for line in out.splitlines() if line.startswith("p") and line[1:].isdigit()})


def pids_listening(port: int) -> List[int]:
    """Pids LISTENING on the TCP port: ``ss`` first, ``lsof`` when ``ss`` cannot say.

    Raises :class:`ServeError` when neither tool could answer — "nothing listens" is only ever said when a tool
    actually saw an empty port (measured: a hung ``lsof`` read as a free port made a stop report "nothing listening"
    for a server that was up).
    """
    failures: List[str] = []
    lookups: Tuple[Tuple[str, Callable[[int], Optional[List[int]]]], ...] = (("ss", _ss_pids), ("lsof", _lsof_pids))
    for name, lookup in lookups:
        try:
            pids = lookup(port)
        except FileNotFoundError:
            failures.append(f"{name}: not installed")
            continue
        except subprocess.TimeoutExpired:
            failures.append(f"{name}: timed out")
            continue
        except (OSError, subprocess.CalledProcessError) as exc:
            failures.append(f"{name}: {exc}")
            continue
        if pids is not None:
            return pids
        failures.append(f"{name}: showed a listener without its pid")
    raise ServeError(f"could not look up what listens on port {port} ({'; '.join(failures)})")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_port(name: str, port: int) -> str:
    """SIGTERM whatever listens on the port (SIGKILL after 5 s), one report line.

    The pids LISTENING on the port are the only processes a stop ever signals: a process that binds no port (an MCP
    server talking to the app, say) is never among them — its life belongs to whoever started it.
    """
    pids = pids_listening(port)
    if not pids:
        return f"{name}: nothing listening on port {port}"
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + _STOP_WAIT_SECONDS
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.2)
    for pid in pids:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
    return f"{name}: stopped pid {', '.join(str(p) for p in pids)} (port {port})"


def stop_servers(spec: AppSpec, chosen: List[Dict[str, Any]]) -> None:
    """Stop the chosen servers — the processes listening on their ports, one line each.

    A never-saved workspace that kept work on its way out gets one more line: where the work is, and the restart that
    continues it (:func:`never_saved_note`).
    """
    for server in chosen:
        print(stop_port(f"{spec.name} ({server['label']})", int(server["port"])))
        note = never_saved_note(spec, server)
        if note:
            print(note)


def refuse_a_foreign_holder(spec: AppSpec, port: int) -> None:
    """A listener on ``port`` that did not answer as the app is never SIGTERMed by a stop.

    The one wording for it: a stop's fallbacks and ``restart <port>`` both call it, so the two commands cannot drift
    into saying different things about the same port.
    """
    if pids_listening(port):
        raise ServeError(
            f"port {port} is held by something that is not {spec.name}{port_holder(port)} — not stopping it"
        )


def refuse_a_port_serving_something_else(spec: AppSpec, address: Address, expect: str) -> None:
    """Refuse the port fallback when what answers there is NOT the workspace that was named.

    The fallback stops a port because the registry knows no server for the workspace — a server started before
    registration, or on a port nothing probes. It used to stop whatever held that port. Measured: nothing was serving
    the type named, so its stop stopped a server serving another workspace file on the default port, reporting only
    ``stopped pid 28992 (port 8787)`` — a line that names no workspace, so it did not even say what had been ended.

    So the IDENTITY decides, not the port: a server answering as the workspace named is stopped (that is the
    fallback's whole purpose), one answering as something else is refused, and a port held by a process that is not
    the app is refused too rather than SIGTERMed. With no identity expected (``expect=""``: an empty workspace,
    nothing remembered, a restart) any server of the app answering there is stopped, and a listener that does not
    answer as one is still refused. The cost: a server too wedged to answer its status check is refused the same way,
    and is ended with ``kill``.
    """
    meta = app_at(spec, address.host, address.port)
    if meta is not None:
        info = meta.get("workspace") or {}
        running = workspace_identity(str(info.get("path") or ""), str(info.get("template") or ""))
        if expect and running and running != expect:
            named = running[5:] if running.startswith("type:") else running
            raise ServeError(
                f"port {address.port} serves {identity_label(running)}, not {identity_label(expect)} — "
                f"'{spec.name} stop {named}' stops that one, or '{spec.name} stop --all' stops every server"
            )
        return
    refuse_a_foreign_holder(spec, address.port)


def tell_server_it_restarts(host: str, port: int) -> None:
    """Tell the server on ``host:port`` that the stop about to come is a restart — before the signal.

    Every page open on it then shows "restarting…" and reloads once the new server answers, where a stop's pages show
    "has exited" and close (``portail.web.pages``). A server that does not take the word — one too stuck to answer —
    is stopped all the same; its pages then hear ``exit``.
    """
    request = urllib.request.Request(
        f"http://{host}:{port}/api/leaving",
        data=json.dumps({"why": "restart"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urlopen(request, timeout=1.0) as answer:
            answer.read()
    except OSError as exc:
        logger.debug(f"the server on port {port} was not told it restarts: {exc}")


def stop_both(spec: AppSpec, address: Address, expect: str = "", restarting: bool = False) -> None:
    """Stop whatever server of the app serves on ``address`` — the fallback when discovery found none.

    ``restarting``: the same workspace comes back on this very port, so its open pages wait for it and reload
    (:func:`tell_server_it_restarts`) instead of closing.
    """
    refuse_a_port_serving_something_else(spec, address, expect)
    if restarting:
        tell_server_it_restarts(address.host, address.port)
    print(stop_port(spec.name, address.port))


# -- talking to a running server --------------------------------------------------------------------------------------


def post_json(spec: AppSpec, host: str, port: int, route: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST ``body`` to a running server of the app and return its JSON answer (an error names the message)."""
    request = urllib.request.Request(
        f"http://{host}:{port}{route}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with _urlopen(request, timeout=600) as response:
            return dict(json.load(response))
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc).get("error") or exc.reason
        except Exception:  # noqa: BLE001 - a non-JSON error body still names the status
            message = exc.reason
        raise ServeError(f"{spec.name} on port {port} refused: {message}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise ServeError(f"{spec.name} on port {port} stopped answering: {exc}") from None


def get_json(spec: AppSpec, host: str, port: int, route: str) -> Dict[str, Any]:
    """GET ``route`` of a running server of the app and return its JSON answer (an error names the message)."""
    try:
        with _urlopen(f"http://{host}:{port}{route}", timeout=60) as response:
            return dict(json.load(response))
    except (urllib.error.URLError, OSError) as exc:
        raise ServeError(f"{spec.name} on port {port} stopped answering: {exc}") from None


# -- the port ---------------------------------------------------------------------------------------------------------


def browser_choice(browser: str) -> BrowserChoice:
    """The ``--browser`` value, checked — before anything is opened, stopped or served."""
    if browser not in BROWSER_CHOICES:
        allowed = f"{', '.join(BROWSER_CHOICES[:-1])} or {BROWSER_CHOICES[-1]}"
        raise ServeError(f"--browser must be {allowed} — got {browser!r}")
    return cast(BrowserChoice, browser)


def apply_port(target: Union[Served, Address], port: Any) -> None:
    """``--port N`` on a serving command: the workspace serves on N.

    The commands BUILD their workspace (from a file, from a type), so a flag naming no parameter used to fall through
    to the app's own configuration and reach nothing — the conflict check still probed the default port (measured:
    ``workspace <type> --port 8890`` refused naming the default). The CLI may hand the value over as a string;
    anything that is not a port is a located error.
    """
    if port in (0, "", None):
        return
    try:
        number = int(port)
    except (TypeError, ValueError):
        raise ServeError(f"--port takes a port number, got {port!r}") from None
    if not 1 <= number <= 65535:
        raise ServeError(f"--port takes a port number between 1 and 65535, got {number}")
    target.port = number


def next_free_port(host: str, start: int) -> int:
    """The first port after ``start`` nothing listens on (within 100)."""
    for candidate in range(start + 1, start + 101):
        if not port_in_use(host, candidate):
            return candidate
    raise ServeError(f"no free port in {start + 1}..{start + 100} on {host}")


def free_port_agreed(target: Served, auto_port: bool, busy: str, asked: str) -> Optional[int]:
    """The next free port once the move is agreed — by ``--auto-port`` or a yes ([Y/n], Enter = yes); None on a no.

    ``busy`` says what holds the port, as the refusal without a terminal to ask in names it; ``asked`` opens the
    question.
    """
    free = next_free_port(target.host, target.port)
    if auto_port:
        return free
    if not sys.stdin.isatty():
        raise ServeError(f"{busy} — rerun with --auto-port to launch this workspace on the next free port")
    answer = input(f"{asked} on port {free}? [Y/n] ").strip().lower()
    return None if answer in ("n", "no") else free


def resolve_port_conflict(spec: AppSpec, target: Served, auto_port: bool) -> bool:
    """When the port is taken, move to a free one — by ``--auto-port`` (no ask), or, for ANOTHER workspace, by a yes.

    ``auto_port`` is on by default for a type workspace (a NEW workspace each time: the command decides) and typed for
    anything else. It takes the next free port whoever holds this one — another workspace, the same type, a program
    that is not the app — and asks nothing.

    Without it a saved workspace keeps its port: ANOTHER workspace answering there asks ([Y/n], Enter = yes), and a
    program that is not the app is left to :meth:`Server.serve`'s located error. Returns False when the user declines
    — the running server's report is printed and nothing serves. Untouched, the port unchanged: a free port, the SAME
    saved workspace (serve prints its report), and a bare empty workspace against a running server of the app
    (nothing specific was asked for, and the app answers there).
    """
    if not port_in_use(target.host, target.port):
        return True
    wanted = workspace_identity(target.path, target.template)
    original = target.port
    running = app_at(spec, target.host, target.port)
    if running is None:
        if not auto_port:
            return True
        holder = port_holder(target.port)
        target.port = next_free_port(target.host, target.port)
        # `port_holder` spells it " (pid 2279 Code Helper (Plugin))", or "" when lsof names nobody.
        print(
            f"launching on port {target.port} "
            f"(port {original} stays with {holder[2:-1] if holder else 'another program'})"
        )
        return True
    if not wanted:
        return True
    info = running.get("workspace") or {}
    serving = workspace_identity(str(info.get("path") or ""), str(info.get("template") or ""))
    # A type workspace is a NEW one each time, so with the port found automatically a server of the same type is no
    # reason to stay: this one gets a port of its own. Without it the same type keeps the already-running report.
    new_workspace = bool(target.template) and not target.path
    if serving == wanted and not (auto_port and new_workspace):
        return True
    label = serving or "an empty workspace"
    free = free_port_agreed(
        target,
        auto_port,
        f"port {target.port} already serves {label}",
        f"{spec.name} already serves {label} on port {target.port} — launch {wanted}",
    )
    if free is None:
        print(Server(spec, target).already_running_report(running))
        return False
    target.port = free
    print(f"launching on port {free} (port {original} stays with {label})")
    return True


# -- hooks ------------------------------------------------------------------------------------------------------------


def run_hook(spec: AppSpec, name: str, hook: Callable[[Served], None], served: Served) -> bool:
    """Run one of the app's hooks on ``served`` — a hook that fails is logged and serving goes on. True when it ran."""
    try:
        hook(served)
    except Exception as exc:  # noqa: BLE001 - hooks never raise into serving
        logger.warning(f"{spec.name}: the {name} hook failed: {exc}")
        return False
    return True


def prepare(spec: AppSpec, target: Served) -> None:
    """``Hooks.prepare``, in the TERMINAL — unless the start that launched this one ran it already.

    A ``--background`` start runs it before it starts the child and hands the child ``spec.env("prepared")``: the
    child's output is discarded, so what the hook says must reach the person who typed the command — and it is not
    run a second time where nobody would see it.
    """
    if os.environ.pop(spec.env(PREPARED), None):
        return
    run_hook(spec, "prepare", spec.hooks.prepare, target)


# -- starting a server from the command line --------------------------------------------------------------------------


def as_options(value: Any) -> Dict[str, str]:  # what a workspace or a live answer holds under ``options``
    """``value`` as serving options — text keys and text values; anything that is no mapping is no options."""
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def options_of(target: Served) -> Dict[str, str]:
    """The app's own serving options a workspace carries (its ``options``) — none when it carries none."""
    return as_options(getattr(target, "options", None))


def option_words(options: Dict[str, str]) -> List[str]:
    """The options as command-line words: ``{"notes_dir": "/n"}`` → ``["--notes-dir", "/n"]``."""
    words: List[str] = []
    for key, value in options.items():
        words += ["--" + key.replace("_", "-"), value]
    return words


def background_command(spec: AppSpec, target: Served) -> List[str]:
    """The argv that serves THIS workspace again, in a child process.

    Rebuilt from the workspace rather than replayed from ``sys.argv``, for two reasons: the parent has already
    resolved the port (``--auto-port`` may have moved it, and the child must bind THAT port rather than re-pick), and
    a replayed ``--background`` would recurse. ``-m <spec.cli_module>`` rather than ``sys.argv[0]``, from a directory
    that is not this one (:func:`neutral_cwd`). ``--browser never``: the terminal opens the browser once the child
    answers, never the child itself.
    """
    argv = [sys.executable, "-m", spec.cli_module]
    if target.template:
        argv += ["workspace", target.template]
    elif target.path:
        argv += ["open", target.path]
    else:
        argv += ["open"]
    return argv + ["--port", str(target.port), "--browser", "never"] + option_words(options_of(target))


def _where(target: Served) -> str:
    return target.path or (f"the {target.template} type workspace" if target.template else "(unsaved workspace)")


def _tail(path: Path) -> str:
    """The last lines of a dead child's output, indented for the error. Never raises."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:  # pragma: no cover - the file we just wrote
        return ""
    return "\n".join(f"  {line}" for line in lines[-_BACKGROUND_TAIL_LINES:])


def serve_detached(spec: AppSpec, target: Served, continuing: Optional[Path] = None) -> bool:
    """Serve in a child process and return once it ANSWERS — ``--background`` / ``-b``. True when it answered, False
    when it was still coming up when the wait ran out (the browser is opened only on an answer).

    The port conflict was resolved by the caller, the process holding the terminal, so ``--auto-port`` and the
    ``[Y/n]`` ask keep working and the child inherits the answer. A restart CONTINUING a never-saved workspace hands
    its folder down (``spec.env("continue_folder")``): the child's command names only the type, and without the
    folder it would start a fresh one.

    The wait is what makes the flag honest: a detached process that dies on a bad workspace would otherwise leave the
    user with a prompt, no server, and nothing said. Its stdout is discarded (loggair writes the real log, named in
    the report); its stderr is kept only long enough to explain a death before loggair was configured.
    """
    name = spec.name
    errors = tempfile.NamedTemporaryFile(prefix=f"{name}-background-", suffix=".err", delete=False)
    child = subprocess.Popen(
        background_command(spec, target),
        stdout=subprocess.DEVNULL,
        stderr=errors,
        start_new_session=True,
        cwd=neutral_cwd(),
        env={
            **os.environ,
            spec.env(PREPARED): "1",
            **({spec.env(CONTINUE_FOLDER): str(continuing)} if continuing is not None else {}),
        },
    )
    errors.close()
    url = Server(spec, target).url()
    deadline = time.monotonic() + _BACKGROUND_WAIT_SECONDS
    while time.monotonic() < deadline:
        if app_at(spec, target.host, target.port) is not None:
            Path(errors.name).unlink(missing_ok=True)
            print(f"{name}: serving {target.title!r} on {url}  — {_where(target)}")
            print(f"  background pid {child.pid} · {log_path(server_log(spec))}")
            print(f"  '{name} stop' stops it")
            return True
        code = child.poll()  # the exit code from the poll that SAW it, never a second read
        if code is not None:
            tail = _tail(Path(errors.name))
            Path(errors.name).unlink(missing_ok=True)
            raise ServeError(
                f"{name} exited before it answered (pid {child.pid}, code {code})"
                + (f" — its last output:\n{tail}" if tail else "")
            )
        time.sleep(0.2)
    print(f"{name}: still starting on {url} (background pid {child.pid}) — {_where(target)}")
    print(f"  it has not answered in {_BACKGROUND_WAIT_SECONDS:.0f} s; watch {errors.name}")
    return False


def open_browser(spec: AppSpec, target: Served) -> None:
    """Open the default browser on ``target``'s address — one line naming the address when no browser could be."""
    url = Server(spec, target).url()
    logger.debug(f"opening {url} in the default browser")
    if not webbrowser.open(url):
        print(f"{spec.name}: no browser could be opened — open {url} yourself")


def open_once_answering(spec: AppSpec, target: Served, served: threading.Event) -> threading.Thread:
    """A foreground start's browser, opened from a thread once the server ANSWERS.

    :meth:`Server.serve` holds the main thread until the server stops, and werkzeug binds the port only inside it: a
    tab opened before the bind shows the browser's "cannot connect" page. So this thread asks the address
    (``/api/meta``, the probe every start uses) until it answers, and opens the tab then. ``served`` is set when
    serving returns — a server that ends without ever answering opens nothing, and the caller joins the thread.
    """

    def wait_then_open() -> None:
        while not served.is_set():
            if app_at(spec, target.host, target.port) is not None:
                open_browser(spec, target)
                return
            served.wait(_BROWSER_POLL_SECONDS)

    thread = threading.Thread(target=wait_then_open, name=browser_thread(spec), daemon=True)
    thread.start()
    return thread


def serve_workspace(
    spec: AppSpec,
    target: Served,
    *,
    create_app: Optional[Callable[["Server"], "Flask"]] = None,
    auto_port: bool = False,
    background: bool = False,
    continuing: Optional[Path] = None,
    browser: BrowserChoice = "never",
) -> None:
    """Serve ``target`` from the command line — here, or ``background`` in a child.

    Args:
        create_app: The app's Flask app for a :class:`Server` (the shell mounted in it); unset → the bare shell.
        auto_port: Move to the next free port without asking when the port is taken (:func:`resolve_port_conflict`).
        background: Serve in a DETACHED child and return once it answers (:func:`serve_detached`).
        continuing: The never-saved folder this start continues, handed to that child; a foreground start already
            works out of it.
        browser: ``new`` opens the address once a server started HERE answers, ``always`` also on a server of the app
            already answering there, ``never`` opens none.
    """
    if not resolve_port_conflict(spec, target, auto_port):
        # Declined — the running server's report was printed instead, naming the address it serves.
        if browser == "always":
            open_browser(spec, target)
        return
    if background:
        # The same workspace already answering is reported as a foreground start reports it, and no child is started:
        # measured before, the child found the port taken and exited, while the terminal saw the OLD server answer and
        # printed `serving … background pid N` for that dead child.
        running = app_at(spec, target.host, target.port) if port_in_use(target.host, target.port) else None
        if running is not None:
            print(Server(spec, target).already_running_report(running))
            if browser == "always":
                open_browser(spec, target)
            return
        # The app's preparation runs HERE, in the terminal the user typed into: the child's output is discarded.
        prepare(spec, target)
        if serve_detached(spec, target, continuing) and browser != "never":
            open_browser(spec, target)
        return
    # The preparation runs BEFORE the server is registered: the registry lists a server only while it answers (`list`,
    # `stop` and the MCP tools prune an entry that does not), and a server still preparing does not — measured before:
    # a `list` during an 8 s page build pruned the entry, and the server was invisible to list, stop and the MCP tools
    # from then on. A port already taken prepares nothing: serve() prints that server's report (or refuses).
    if not port_in_use(target.host, target.port):
        prepare(spec, target)
    # Register only a port this process will actually BIND — asked after the preparation, which may have taken a
    # while: a busy port means serve() prints the already-running report (or raises), and registering would clobber
    # the REAL server's entry on the way out.
    register = not port_in_use(target.host, target.port)
    if register:
        register_server(spec, target)
        if not target.path:
            # The never-saved workspace this process serves: what a restart continues and a stop names — recorded
            # HERE, by the process that serves it, so a --background start's child does it.
            record_unsaved(spec, target)
    done = threading.Event()
    waiting = open_once_answering(spec, target, done) if register and browser != "never" else None
    try:
        Server(spec, target, create_app).serve()
    except PortInUseError as exc:
        raise ServeError(str(exc)) from exc
    finally:
        done.set()
        if waiting is not None:
            waiting.join()
        if register:
            unregister_server(spec, target)
    if not register and browser == "always":
        open_browser(spec, target)  # serve() printed the already-running report: open the server it names


# -- the server -------------------------------------------------------------------------------------------------------


def _quiet_request_logs() -> None:
    """Silence werkzeug's per-request access lines (one INFO per request) — WARNING keeps what matters."""
    import logging

    logging.getLogger("werkzeug").setLevel(logging.WARNING)


#: The signals that end the server the way Ctrl+C (SIGINT) does, so the app's ``leaving`` hook runs on the way out:
#: SIGTERM (what ``stop`` / ``restart`` send) and SIGHUP (the terminal closed). Measured before they were handled:
#: either ended the process at once, and every unsaved edit went with it.
_WAYS_OUT: Tuple[signal.Signals, ...] = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)


def _interrupt(signum: int, frame: Any) -> None:  # frame: the interrupted stack frame, unused
    """SIGTERM or SIGHUP handled like Ctrl+C: the server stops serving and takes the same way out."""
    raise KeyboardInterrupt(f"signal {signum}")


class Server:
    """One app's workspace, served: the lifecycle :class:`~portail.spec.Served` needs and the page's shell asks.

    Args:
        spec: The app.
        served: The workspace it serves.
        create_app: Makes the app's Flask app for this server, with the shell mounted (``portail.web.mount_shell``);
            unset → the bare shell (``portail.web.create_shell``).
    """

    #: True while :meth:`serve` holds SIGTERM's handler (on the main thread): only then does a SIGTERM to this process
    #: take serve's way out — the pages told, the ``leaving`` hook run — rather than end it at once.
    _ends_on_sigterm: bool = False

    def __init__(
        self, spec: AppSpec, served: Served, create_app: Optional[Callable[["Server"], "Flask"]] = None
    ) -> None:
        self.spec = spec
        self.served = served
        self._create_app = create_app

    def url(self) -> str:
        """Where this workspace serves — the address to open in a browser."""
        return f"http://{self.served.host}:{self.served.port}"

    def meta(self) -> Dict[str, Any]:
        """What ``/api/meta`` answers: the app's name (its identity), the title, the workspace and this process."""
        return {"app": self.spec.name, "title": self.served.title, "workspace": self.served.info(), "pid": os.getpid()}

    def already_running_report(self, running: Dict[str, Any]) -> str:
        """What to print when a server of the app is already serving this address.

        The URL sits ALONE on its line: terminals linkify a bare ``http://…`` run, and a trailing comma or bracket is
        taken into the link and breaks it.
        """
        info = running.get("workspace") or {}
        serving = label_of(str(info.get("path") or ""), str(info.get("template") or ""))
        name = self.spec.name
        return (
            f"{name} is already running — serving {serving}\n"
            f"  {self.url()}\n"
            f"  '{name} stop' stops it; '{name} restart' reloads it."
        )

    def leaving_note(self) -> Dict[str, str]:
        """Where this workspace's work is kept once its server has gone — what :meth:`exiting` answers the ⏻, and what
        every open page is told by any stop. Never refused: it says, it stops nothing.

        ``kept_in`` is a never-saved workspace's own folder when the app's restarts continue one (``Hooks.continued``),
        ``kept_until`` says how long; both are ``""`` for a saved workspace (its file keeps it) and for an app whose
        restarts continue nothing.
        """
        if self.served.path or not continues(self.spec):
            return {"kept_in": "", "kept_until": ""}
        try:
            folder = str(self.served.state_dir())
        except Exception:  # noqa: BLE001 - state_dir never raises by contract; a broken one keeps nothing said
            folder = ""
        return {"kept_in": folder, "kept_until": kept_until(self.spec) if folder else ""}

    def exiting(self) -> Dict[str, str]:
        """What the page says once its ⏻ has stopped this server — asked BEFORE :meth:`stop_serving`.

        Refused like :meth:`stop_serving` when this server cannot end itself, so the page hears the refusal instead of
        an answer that promises an exit.
        """
        self._refuse_an_exit_it_does_not_own()
        return self.leaving_note()

    def stop_serving(self) -> None:
        """End this server the way ``<name> stop`` ends it: SIGTERM to this very process.

        Not a shutdown of its own — the signal lands in the handler :meth:`serve` set, which leaves ``app.run`` the one
        way every stop leaves it (the pages told, the ``leaving`` hook run, the registry entry removed). A server
        :meth:`serve` did not start on the main thread holds no such handler: the signal would end the program it runs
        in, so it is refused instead.
        """
        self._refuse_an_exit_it_does_not_own()
        os.kill(os.getpid(), signal.SIGTERM)

    def restarting(self) -> Dict[str, Union[int, str]]:
        """What the page needs once its ↻ asked for a restart — asked BEFORE :meth:`restart_serving`.

        ``pid`` is this process: the page reloads once ``/api/meta`` answers with ANOTHER one, the fresh server.
        ``log`` is where the restart writes what it prints (:func:`restart_log`) — what the page names when no server
        comes back. Refused like :meth:`stop_serving`.
        """
        self._refuse_an_exit_it_does_not_own()
        return {"pid": os.getpid(), "log": str(log_dir() / restart_log(self.spec))}

    def restart_serving(self) -> None:
        """Restart this server the way ``<name> restart <port> --background`` does — that very command, launched.

        A fresh Python process is what picks up changed code, and the command line already knows how to restart every
        kind of workspace (a saved file, a type, a never-saved one continued in its folder): it stops this server with
        SIGTERM — the way out :meth:`stop_serving` takes — and serves the same workspace again, detached, on this
        port. The command runs in a session of its OWN, so neither that SIGTERM nor a Ctrl+C in this server's terminal
        reaches it, from the neutral directory (:func:`neutral_cwd`). Refused like :meth:`stop_serving`.
        """
        self._refuse_an_exit_it_does_not_own()
        log = log_dir() / restart_log(self.spec)
        if not log.parent.is_dir():
            log.parent.mkdir(parents=True)
        with log.open("w", encoding="utf-8") as out:
            subprocess.Popen(
                [sys.executable, "-m", self.spec.cli_module, "restart", str(self.served.port), "--background"],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=neutral_cwd(),
            )

    def _refuse_an_exit_it_does_not_own(self) -> None:
        if not self._ends_on_sigterm:
            raise ServeError(f"this {self.spec.name} runs inside another program — it stops with that program")

    def app(self) -> "Flask":
        """The Flask app this server runs: the app's own (``create_app``), else the bare shell."""
        if self._create_app is not None:
            return self._create_app(self)
        from portail.web.shell import create_shell

        return create_shell(self)

    def serve(self) -> None:
        """Serve the workspace until interrupted (Flask's threaded dev server — a local tool, not a deployment).

        When a server of the app is ALREADY serving this address, that is not a failure: what the user asked for — the
        app reachable here — is already true. Hand them the URL and return. A port held by anything else is a
        :class:`~portail.errors.PortInUseError`.

        ``Hooks.starting`` runs before the app answers. Ctrl+C (SIGINT), SIGTERM — what ``stop`` and ``restart`` send —
        and SIGHUP (the terminal closed) all end ``app.run`` the same way, and the way out tells every open page FIRST
        (``portail.web.pages.tell_pages_leaving``), then runs ``Hooks.leaving``. A kill -9 or a crash cannot keep
        anything.
        """
        from portail.web import pages

        served, name = self.served, self.spec.name
        _quiet_request_logs()
        if port_in_use(served.host, served.port):
            running = app_at(self.spec, served.host, served.port)
            if running is None:
                raise PortInUseError(
                    f"port {served.port} on {served.host} is in use{port_holder(served.port)} — "
                    + ("name another with --port" if self.spec.workspace == "folder" else "set port: in the workspace")
                    + " (or stop that process)"
                )
            print(self.already_running_report(running))
            return
        run_hook(self.spec, "starting", self.spec.hooks.starting, served)
        app = self.app()
        logger.info(f"{name}: serving {served.title!r} on {self.url()}  — {_where(served)}")
        # Signal handlers can be set only on the main thread; served from another (a test, an embedding app) the stop
        # is the caller's business and Ctrl+C still lands here.
        main = threading.current_thread() is threading.main_thread()
        previous = {signum: signal.signal(signum, _interrupt) for signum in _WAYS_OUT} if main else {}
        self._ends_on_sigterm = main  # the page's ⏻ may end this process only through that handler (stop_serving)
        try:
            app.run(host=served.host, port=served.port, threaded=True, debug=False, use_reloader=False)
        except KeyboardInterrupt:
            pass  # a signal outside werkzeug's serve loop (which returns quietly on one): the same way out
        finally:
            self._ends_on_sigterm = False
            # FIRST, before anything slow: every open page shows that this server has gone (or is restarting) instead
            # of looking alive. Their streams outlive app.run — werkzeug never joins a request thread.
            try:
                pages.tell_pages_leaving(app, self)
            except Exception as exc:  # noqa: BLE001 - the stop goes on; a page then sees its stream drop
                logger.debug(f"the open pages could not be told this server is going: {exc}")
            try:
                run_hook(self.spec, "leaving", self.spec.hooks.leaving, served)
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            self._say_what_is_kept()

    def _say_what_is_kept(self) -> None:
        """A never-saved workspace that holds work on its way out: where, and what continues it — in the log."""
        note = self.leaving_note()
        if note["kept_in"] and holds_work(self.spec, Path(note["kept_in"])):
            what = f"the {self.served.template} type workspace" if self.served.template else "the workspace"
            logger.info(
                f"{what} was never saved — its work is kept in {note['kept_in']}{_kept(self.spec)};"
                f" '{self.spec.name} restart' continues it"
            )


__all__ = [
    "Address",
    "BROWSER_CHOICES",
    "BrowserChoice",
    "LAST_WORKSPACE",
    "Server",
    "UNSAVED_RECORD",
    "app_at",
    "continue_command",
    "get_json",
    "kept_until",
    "log_dir",
    "meta_at",
    "neutral_cwd",
    "pids_listening",
    "port_in_use",
    "post_json",
    "running_servers",
    "serve_workspace",
    "stop_port",
]
