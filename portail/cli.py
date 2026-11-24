"""The lifecycle commands, added to an app's own liquifai CLI: ``open`` (the default), ``workspace <type>``, ``stop``,
``restart`` and ``list``.

An app builds its CLI and hands portail what only it knows — how to open a workspace file, an empty workspace and a
type workspace::

    app = LiquifyApp(name="demoapp", description="…", strict_flags=True)
    lifecycle = add_lifecycle_commands(app, SPEC, open_file=open_file, open_empty=DemoWorkspace,
                                       open_type=open_type, type_names=lambda: ["classification"])

``strict_flags=True`` is the app's choice, and the recommended one: an option a command does not declare is then an
error, never a silent config override — measured, ``stop --port 8799`` (no such option then) stopped the only running
server, as a bare ``stop`` would.

Every message names the app by ``spec.name``; the commands' help is written with it too.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection, Dict, List, Optional

import yaml
from liquifai import LiquifyApp
from liquifai.exceptions import ConfigNotFoundError

from portail import serving
from portail.errors import ServeError
from portail.serving import Address, BrowserChoice
from portail.spec import AppSpec, Served

if TYPE_CHECKING:
    from flask import Flask


@dataclass(frozen=True)
class Continuation:
    """A never-saved workspace a restart continues: its folder, its type, where it comes back, and the server serving
    it now (``None`` when none does — its stop already left its work in the folder)."""

    folder: Path
    template: str
    host: str
    port: int
    server: Optional[Dict[str, Any]]


class Lifecycle:
    """What the lifecycle commands do, for one app — each command a method, so a Python caller (and a test) can call
    it directly.

    Args:
        spec: The app.
        open_file: Open a workspace file (its absolute path) into a live workspace; raises when it is not one.
        open_empty: An EMPTY workspace (what a bare ``<name>`` serves).
        open_type: Open a TYPE workspace by name (``<name> workspace <type>``) — ``None``: the app has no types.
        type_names: Every type ``open_type`` opens (also the TAB completions).
        create_app: The app's Flask app for a :class:`~portail.serving.Server`; unset → the bare shell.
    """

    def __init__(
        self,
        spec: AppSpec,
        *,
        open_file: Callable[..., Served],
        open_empty: Callable[[], Served],
        open_type: Optional[Callable[[str], Served]] = None,
        type_names: Optional[Callable[[], List[str]]] = None,
        create_app: Optional[Callable[[serving.Server], "Flask"]] = None,
    ) -> None:
        self.spec = spec
        self._open_file = open_file
        self._open_empty = open_empty
        self._open_type = open_type
        self._type_names = type_names or (lambda: [])
        self.create_app = create_app

    # -- what the app opens ---------------------------------------------------------------------------------------

    def type_names(self) -> List[str]:
        """Every type ``<name> workspace <type>`` opens — none when the app opens no types."""
        return sorted(self._type_names()) if self._open_type is not None else []

    def is_workspace(self, path: str) -> bool:
        """Whether ``path`` is there as what the app's workspace is: a file, or a folder (``AppSpec.workspace``)."""
        return Path(path).is_dir() if self.spec.workspace == "folder" else Path(path).is_file()

    def open_file(self, path: str, options: Optional[Dict[str, str]] = None) -> Served:
        """The workspace ``path`` — a file, or a folder for an app that serves one — opened by the app.

        Args:
            path: The workspace's file or folder.
            options: The app's own serving options, handed to its ``open_file`` as keyword arguments.

        Raises:
            ConfigNotFoundError: it does not exist (one clean CLI line) — a bare type name points at
                ``<name> workspace <type>`` instead.
        """
        target = Path(path)
        if not self.is_workspace(path):
            hint = (
                f" — for the type workspace use: {self.spec.name} workspace {path}" if path in self.type_names() else ""
            )
            raise ConfigNotFoundError(f"workspace {self.spec.workspace} {target} does not exist{hint}")
        return self._open_file(str(target.resolve()), **(options or {}))

    def open_type(self, name: str) -> Served:
        """The type workspace ``name``, opened by the app.

        Raises:
            ConfigNotFoundError: no type of that name (the message lists them).
        """
        names = self.type_names()
        if self._open_type is None or name not in names:
            known = f" (known: {', '.join(names)})" if names else ""
            raise ConfigNotFoundError(f"{name!r} is not a type workspace{known}")
        return self._open_type(name)

    def open_empty(self) -> Served:
        """An empty workspace, opened by the app."""
        return self._open_empty()

    def continued(self, folder: Path, template: str) -> Served:
        """The never-saved workspace kept in ``folder``, opened again by the app (``Hooks.continued``)."""
        continued = self.spec.hooks.continued
        if continued is None:  # pragma: no cover - a continuation is only ever found when the app gives the hook
            raise ServeError(f"{self.spec.name} continues no never-saved workspace")
        return continued(folder, template)

    def serve(self, target: Served, **kwargs: Any) -> None:
        """:func:`~portail.serving.serve_workspace` with the app's Flask app."""
        serving.serve_workspace(self.spec, target, create_app=self.create_app, **kwargs)

    # -- the commands ---------------------------------------------------------------------------------------------

    def open(
        self,
        workspace: str = "",
        auto_port: bool = False,
        port: Any = 0,
        background: bool = False,
        browser: str = "new",
        options: Optional[Dict[str, str]] = None,
    ) -> None:
        """``<name> [workspace.yaml]`` — serve an empty workspace or a workspace file (``options``: the app's own
        serving options, for the workspace named)."""
        choice = serving.browser_choice(browser)
        handed = serving.handed_down_folder(self.spec, self.spec.name)
        if workspace:
            target = self.open_file(workspace, options)
        elif handed is not None:
            target = self.continued(handed, "")
        else:
            serving.warn_about_work_left_behind(self.spec)
            target = self.open_empty()
        serving.apply_port(target, port)
        # What the app opens with nothing named may be a workspace on disk too (the folder the command is typed in).
        serving.remember(self.spec, f"file:{target.path}" if target.path else "empty")
        self.serve(target, auto_port=auto_port, background=background, browser=choice)

    def workspace(
        self,
        type: str = "",
        auto_port: Optional[bool] = None,
        port: Any = 0,
        background: bool = False,
        browser: str = "new",
    ) -> None:
        """``<name> workspace <type>`` — serve a NEW type workspace, on its own port unless ``--port`` names one."""
        choice = serving.browser_choice(browser)
        if not type:
            raise ConfigNotFoundError(f"a type is required (known: {', '.join(self.type_names())})")
        handed = serving.handed_down_folder(self.spec, f"{self.spec.name} workspace {type}")
        if handed is not None:
            target = self.continued(handed, type)
        else:
            serving.warn_about_work_left_behind(self.spec)
            target = self.open_type(type)
        serving.apply_port(target, port)
        serving.remember(self.spec, f"type:{type}")
        # Unset: a NEW workspace finds its own port, unless the person named one (`--port 8193` is what another
        # command of the app talks to — moving it silently would send its work to another workspace).
        automatic = port in (0, "", None) if auto_port is None else auto_port
        self.serve(target, auto_port=automatic, background=background, browser=choice)

    def stop(self, workspace: str = "", all: bool = False) -> None:
        """``<name> stop [target] [--all]`` — stop the server(s) named, or the running one(s)."""
        spec = self.spec
        servers = serving.running_servers(spec)
        port = self._port_named(workspace) if workspace else None
        if port is not None:
            self._stop_by_port(port, servers)
            return
        if workspace:
            if self.is_workspace(workspace):
                identity, address = str(Path(workspace).resolve()), self._ports_only(workspace)
            elif workspace in self.type_names():
                identity, address = f"type:{workspace}", self._default_address()
            else:
                identity, address = "", self._ports_only(workspace)
            match = [server for server in servers if identity and server["identity"] == identity]
            if match:
                serving.stop_servers(spec, match)
            else:
                # Not discovered — the workspace's own port (refused when what answers there serves something else,
                # see serving.refuse_a_port_serving_something_else).
                serving.stop_both(spec, address, expect=identity)
            return
        if all:
            # Every server of the app answering — discovery's, else one on the last served workspace's port (a server
            # the registry lost). Never a listener that is not the app.
            chosen = servers or serving.answering_at(spec, self._last_served()[0])
            if chosen:
                serving.stop_servers(spec, chosen)
            else:
                print(f"no {spec.name} is running")
            return
        if len(servers) > 1:
            if not sys.stdin.isatty():
                raise ServeError(
                    f"{len(servers)} {spec.name} servers are running — pass --all, or name the workspace to stop"
                )
            print(f"running {spec.name} servers:")
            for index, server in enumerate(servers, start=1):
                print(f"  {index}. {server['label']}")
                print(f"     http://{server['host']}:{server['port']}")
            answer = input(f"stop which? [a=all / 1-{len(servers)} / q=quit] ").strip().lower()
            if answer in ("a", "all"):
                serving.stop_servers(spec, servers)
            elif answer.isdigit() and 1 <= int(answer) <= len(servers):
                serving.stop_servers(spec, [servers[int(answer) - 1]])
            else:
                print("stopped nothing")
            return
        if servers:
            serving.stop_servers(spec, servers)
            return
        # Nothing discovered: the LAST served workspace's port, refused when what holds it is not that workspace (or,
        # with no identity, not the app at all).
        address, identity = self._last_served()
        serving.stop_both(spec, address, expect=identity)

    def restart(
        self,
        target: str = "",
        port: Any = 0,
        background: bool = False,
        browser: str = "never",
        options: Optional[Dict[str, str]] = None,
    ) -> None:
        """``<name> restart [target]`` — stop the server and serve the workspace again (``options``: the app's own
        serving options; none named → the ones the running server has)."""
        choice = serving.browser_choice(browser)
        continuing = self._continuation(target)
        if continuing is not None:
            # How the person spelled this restart — a refusal names the same command with the flag that gets past it.
            again = f"{self.spec.name} restart" + (f" {target}" if target and self._port_named(target) is None else "")
            self._restart_continuing(continuing, port, background, again, choice)
            return
        resolved = self._resolve_target(target, options)
        # A port target names the server ON it: that one is stopped even when --port moves it. Any other target is
        # stopped where it will serve.
        named_by_port = bool(target) and self._port_named(target) is not None
        ran_on = Address(resolved.host, resolved.port)
        serving.apply_port(resolved, port)
        stopping = ran_on if named_by_port else Address(resolved.host, resolved.port)
        serving.remember_workspace(self.spec, resolved)
        same = (stopping.host, stopping.port) == (resolved.host, resolved.port)
        serving.stop_both(self.spec, stopping, restarting=same)
        self.serve(resolved, background=background, browser=choice)

    def list(self) -> None:
        """``<name> list`` — one line per running server, lowest port first, its URL last."""
        servers = serving.running_servers(self.spec)
        name = self.spec.name
        if not servers:
            types = self.type_names()
            first = f"`{name} workspace {types[0]}` or " if types else ""
            print(f"no {name} is running — start one with {first}`{name} open <workspace.yaml>`")
            return
        for server in servers:
            print(f"{server['port']}  {server['label']}  http://{server['host']}:{server['port']}")

    # -- naming a server ------------------------------------------------------------------------------------------

    def _default_address(self) -> Address:
        return Address(self.spec.default_host, self.spec.default_port)

    def _port_named(self, target: str) -> Optional[int]:
        """The port a ``stop`` / ``restart`` positional names — None when it names a workspace.

        A server is named by the port it runs on exactly as by its workspace: ``stop 8790`` stops whatever serves
        8790. Before, a number was read as a workspace FILE, found none, and the stop fell back to the DEFAULT port. A
        file or type of that spelling is checked FIRST, so a workspace file literally called ``8790`` stays reachable.
        """
        text = str(target)
        if not (text.isascii() and text.isdigit()) or self.is_workspace(text) or text in self.type_names():
            return None
        number = int(text)
        if not 1 <= number <= 65535:
            raise ServeError(f"{text} is not a port — a port number is between 1 and 65535")
        return number

    def _ports_only(self, path: str) -> Address:
        """The workspace file's HOST/PORT, read WITHOUT building anything.

        Stopping a server must never depend on the workspace still loading: a file whose classes are gone (a moved
        module, an uninstalled package) is exactly the one you most want to stop — measured as a stop dying with
        "Cannot resolve class" instead of stopping anything. Tags are ignored wholesale; any parse trouble falls back
        to the default address.
        """

        class _TagBlind(yaml.SafeLoader):
            pass

        def _any_tag(loader: Any, tag_suffix: Any, node: Any) -> Any:
            if isinstance(node, yaml.MappingNode):
                return loader.construct_mapping(node, deep=False)
            if isinstance(node, yaml.SequenceNode):
                return loader.construct_sequence(node)
            return loader.construct_scalar(node)

        _TagBlind.add_multi_constructor("", _any_tag)
        address = self._default_address()
        if self.spec.workspace == "folder":  # a folder holds no address: nothing is read from it
            return address
        try:
            document = yaml.load(Path(path).read_text(), Loader=_TagBlind) or {}
            fields = document.get(self.spec.workspace_key) or {}
            address.port = int(fields.get("port", address.port))
            address.host = str(fields.get("host", address.host))
        except Exception:  # noqa: BLE001 - an unreadable file still stops the default address
            pass
        return address

    def _last_served(self) -> "tuple[Address, str]":
        """Where the LAST served workspace serves, and its identity — what a bare stop falls back to.

        A file or type that is no longer there (nothing served yet, an empty workspace served last, the file deleted
        since) leaves the default address and no identity (``""``).
        """
        what = serving.last_spec(self.spec)
        if what.startswith("file:") and self.is_workspace(what[5:]):
            return self._ports_only(what[5:]), str(Path(what[5:]).resolve())
        if what.startswith("type:") and what[5:] in self.type_names():
            opened = self.open_type(what[5:])
            return Address(opened.host, opened.port), f"type:{what[5:]}"
        return self._default_address(), ""

    def _stop_by_port(self, port: int, servers: List[Dict[str, Any]]) -> None:
        """``stop <port>`` — the same stop as naming the workspace that serves there.

        Nothing answering as the app on that port is the not-serving path a name takes (the plain report); a listener
        that is not the app is refused.
        """
        server = serving.server_on_port(self.spec, port, servers)
        if server is not None:
            serving.stop_servers(self.spec, [server])
            return
        serving.stop_both(self.spec, Address(self.spec.default_host, port))

    def _running_options(self, path: str) -> Dict[str, str]:
        """The serving options the server of the workspace ``path`` runs with — none when none runs."""
        identity = str(Path(path).resolve())
        for server in serving.running_servers(self.spec):
            if server["identity"] == identity:
                return dict(server.get("options") or {})
        return {}

    def _from_spec(self, what: str, options: Optional[Dict[str, str]] = None) -> Served:
        """The remembered workspace, announced — a bare restart means "what I worked on"."""
        if what.startswith("file:"):
            print(f"restarting {what[5:]}")
            return self.open_file(what[5:], options or self._running_options(what[5:]))
        if what.startswith("type:"):
            print(f"restarting the {what[5:]} type workspace")
            return self.open_type(what[5:])
        print("restarting an empty workspace (nothing was served before)")
        return self.open_empty()

    def _from_port(self, port: int, options: Optional[Dict[str, str]] = None) -> Served:
        """``restart <port>``: the workspace serving on that port, to be served there again.

        Opened from what the LIVE server says it serves — its file, its type, or nothing (an empty workspace) — and it
        keeps the port it ran on, not its file's own: an ``--auto-port`` server would otherwise come back on the port
        it was moved away from. Opening happens before anything is stopped, so a served file deleted since is refused
        while its server is still up.

        A port where nothing answers as the app is refused in one of two ways, told apart by whether anything LISTENS
        there: a program that is not the app gets the refusal ``stop <port>`` gives
        (:func:`~portail.serving.refuse_a_foreign_holder`), a port nobody listens on gets "nothing is serving".
        """
        servers = serving.running_servers(self.spec)
        server = serving.server_on_port(self.spec, port, servers)
        if server is None:
            serving.refuse_a_foreign_holder(self.spec, port)
            running = ", ".join(f"{s['port']} {s['label']}" for s in servers)
            raise ServeError(
                f"nothing is serving on port {port} — "
                + (f"running: {running}" if running else f"no {self.spec.name} is running")
            )
        if server["path"]:
            resolved = self.open_file(server["path"], options or dict(server.get("options") or {}))
        elif server["template"]:
            resolved = self.open_type(server["template"])
        else:
            resolved = self.open_empty()
        resolved.port = port
        print(f"restarting {server['label']} (port {port})")
        return resolved

    def _resolve_target(self, target: str, options: Optional[Dict[str, str]] = None) -> Served:
        """``restart [target]``: bare → the LAST served workspace; a file → that workspace; a type name → that type
        workspace; a port → the workspace serving on it (:meth:`_from_port`)."""
        if not target:
            return self._from_spec(serving.last_spec(self.spec), options)
        port = self._port_named(target)
        if port is not None:
            return self._from_port(port, options)
        if self.is_workspace(target):
            return self.open_file(target, options or self._running_options(target))
        if target in self.type_names():
            return self.open_type(target)
        names = self.type_names()
        if not names:
            raise ConfigNotFoundError(f"{target!r} is not a workspace {self.spec.workspace}")
        raise ConfigNotFoundError(
            f"{target!r} is neither a workspace {self.spec.workspace} nor a type workspace (known: {', '.join(names)})"
        )

    # -- continuing a never-saved workspace -----------------------------------------------------------------------

    def _continuation(self, target: str) -> Optional[Continuation]:
        """The never-saved workspace ``restart [target]`` continues — ``None`` for every other restart, and always for
        an app whose restarts continue nothing (no ``Hooks.continued``).

        * a PORT: the never-saved workspace serving there (found by its live answer);
        * bare, when the last served workspace was a never-saved one: the never-saved workspace served last that runs
          or holds work (:func:`~portail.serving.last_unsaved`), else the one never-saved server of that kind running;
        * a TYPE name: the never-saved workspace of that type running, else the one served last when of that type.

        A workspace FILE, a name that is neither, a saved workspace and ``restart <another type>`` are none of these.
        Nothing is opened or stopped here: a running server keeps its work on its way out, so the restart stops it
        first (:meth:`_restart_continuing`).

        Raises:
            ServeError: several never-saved servers of the kind asked for run, and none was served last.
        """
        spec = self.spec
        if not serving.continues(spec):
            return None
        port = self._port_named(target) if target else None
        what = serving.last_spec(spec)
        if target and port is None and (self.is_workspace(target) or target not in self.type_names()):
            return None
        if not target and not serving.never_saved_spec(what):
            return None
        servers = serving.running_servers(spec)
        if port is not None:
            on_port = serving.server_on_port(spec, port, servers)
            return None if on_port is None or not serving.never_saved(on_port) else self._from_server(on_port)
        kind = target or (what[len("type:") :] if what.startswith("type:") else "")
        running = [server for server in servers if serving.never_saved(server) and server["template"] == kind]
        last = serving.last_unsaved(spec, servers)
        if not target and last is not None:
            return self._from_entry(last)
        if target:
            if last is not None and last["server"] is not None and last["server"] in running:
                return self._from_entry(last)
            if not running and last is not None and str(last.get("template") or "") == kind:
                return self._from_entry(last)
        if len(running) == 1:
            return self._from_server(running[0])
        if len(running) > 1:
            ports = ", ".join(str(server["port"]) for server in running)
            raise ServeError(
                f"{len(running)} never-saved {serving.kind(kind)}s are running (ports {ports}) — "
                f"name the one to restart by its port: {spec.name} restart {running[0]['port']}"
            )
        return None

    @staticmethod
    def _from_server(server: Dict[str, Any]) -> Continuation:
        return Continuation(
            Path(str(server["state_dir"])), str(server["template"]), str(server["host"]), int(server["port"]), server
        )

    def _from_entry(self, entry: Dict[str, Any]) -> Continuation:
        server = entry.get("server")
        if server is not None:
            return self._from_server(server)
        return Continuation(
            Path(str(entry["folder"])),
            str(entry.get("template") or ""),
            str(entry.get("host") or self.spec.default_host),
            int(entry.get("port") or self.spec.default_port),
            None,
        )

    def _restart_continuing(
        self, continuing: Continuation, port: Any, background: bool, again: str, browser: BrowserChoice
    ) -> None:
        """Restart a never-saved workspace by CONTINUING it in its own folder.

        A server still serving it is stopped FIRST: it keeps its work in the folder on its way out (``Hooks.leaving``),
        and that is what is read. With none running the folder is read first, so one the app refuses is refused
        before anything is stopped. It comes back where it was served unless ``--port`` moves it; ``--background``
        hands the folder to the server it starts (``spec.env("continue_folder")``).

        The port it comes back on is stopped as every restart stops it — a port ``--port`` moves it to included, also
        while it still runs (measured: only the old port was stopped, then the start refused the busy new one with
        ``--auto-port`` advice restart does not take, and nothing served). Two things are refused BEFORE anything is
        stopped: a port held by a program that is not the app, and — for one that is not running, coming back on the
        port it was served on — a SAVED workspace serving that port now (:meth:`_refuse_taking_a_saved_port`).

        Args:
            again: How the restart was spelled — what a refusal names, with the flag that gets past it.
            browser: The restart's ``--browser``.
        """
        spec = self.spec
        destination = Address(continuing.host, continuing.port)
        serving.apply_port(destination, port)
        named = port not in (0, "", None)
        moved = (destination.host, destination.port) != (continuing.host, continuing.port)
        takes_over = continuing.server is None or moved  # the port it comes back on is not the running server's own
        if continuing.server is None and not named:
            self._refuse_taking_a_saved_port(continuing, destination, again)
        if takes_over:
            serving.refuse_a_port_serving_something_else(spec, destination, expect="")
        print(f"continuing the never-saved {serving.kind(continuing.template)} from {continuing.folder}")
        if continuing.server is not None:
            serving.stop_both(spec, Address(continuing.host, continuing.port), restarting=not moved)
        resolved = self.continued(continuing.folder, continuing.template)
        resolved.host, resolved.port = destination.host, destination.port
        if takes_over:
            serving.stop_both(spec, Address(resolved.host, resolved.port))
        serving.remember_workspace(spec, resolved)
        self.serve(resolved, background=background, continuing=continuing.folder, browser=browser)

    def _refuse_taking_a_saved_port(self, continuing: Continuation, destination: Address, again: str) -> None:
        """Refuse to bring a STOPPED never-saved workspace back on its old port while a SAVED workspace serves it.

        The record remembers where it was served; since then that port may serve a saved workspace — the page's Save
        of a newer never-saved one, or a workspace file opened there. Coming back would stop that server to put an
        older workspace in its place (measured). A port named with ``--port`` is the person's choice and is taken over
        as every restart takes it; this guards only the remembered one, and names both ways on.

        Raises:
            ServeError: a saved workspace answers on the port — nothing has been stopped.
        """
        meta = serving.app_at(self.spec, destination.host, destination.port)
        serving_path = str(((meta or {}).get("workspace") or {}).get("path") or "")
        if not serving_path:
            return
        free = serving.next_free_port(destination.host, destination.port)
        name = self.spec.name
        raise ServeError(
            f"port {destination.port} serves {serving_path}, a saved workspace — not stopping it to bring back the"
            f" never-saved {serving.kind(continuing.template)} from {continuing.folder}; '{name} restart"
            f" {destination.port}' restarts that one, '{again} --port {free}' continues the never-saved one on a free"
            " port"
        )


_OPEN_DOC = """Serve a workspace: empty, or the workspace file given on the command line (``{name} <file>``).

    A saved workspace keeps its own port and never moves by itself. When {name} is already serving ANOTHER workspace
    there, the command asks to launch this one on the next free port ([Y/n], Enter = yes); the same workspace keeps the
    plain already-running report; a port held by a program that is not {name} is an error.

    Args:
        workspace: A saved (or hand-written) workspace file to open; empty → an empty workspace.
        auto_port: Take the next free port WITHOUT asking when the port is taken (by another workspace or by a
            program that is not {name}). Off unless typed.
        port: Serve on THIS port instead of the workspace's own (its file's ``port``, else {default_port}).
        background: Serve in a DETACHED process and hand the terminal back (``-b``). The report names the pid and the
            log file; ``{name} stop`` stops it.
        browser: Open the default browser on the address — ``new`` (default): once a server this command started
            answers; ``always``: also on a {name} already running there; ``never``.
    """

_WORKSPACE_DOC = """Serve a NEW workspace of one type (``{name} workspace <type>``).

    A type workspace is a NEW workspace each time, so it takes the next free port on its own when the default one is
    taken — whoever holds it, another workspace, the same type or another program — and never asks.
    ``--auto-port false`` gives that up (a port held by another workspace asks, as ``{name} <file>`` does), and
    ``--port N`` names the port: nothing is moved then unless ``--auto-port`` is typed too.

    Args:
        type: The type workspace to open; it opens as a template, Save asks where to write the copy.
        auto_port: Take the next free port WITHOUT asking when the port is taken. Unset, it is on unless ``--port``
            names the port; ``--auto-port false`` turns it off.
        port: Serve on THIS port instead of the type's default ({default_port}).
        background: Serve in a DETACHED process and hand the terminal back (``-b``).
        browser: Open the default browser on the address — ``new`` (default): once a server this command started
            answers; ``always``: also on a {name} already running there; ``never``.
    """

_STOP_DOC = """Stop running {name} server(s).

    Bare ``{name} stop`` stops the one running server — or, with several serving side by side, lists them and asks
    which to stop (all, one, or none). A workspace file or type name stops the server actually SERVING it, wherever it
    runs — an ``--auto-port`` launch included. Only the processes listening on the stopped servers' ports are
    signalled; a listener that does not answer as {name} is never stopped.

    Args:
        workspace: A workspace file or type name — stop the server serving it; or the PORT a server runs on
            (``{name} stop 8790``), which names it just the same; empty → the running server(s), asked about when
            there are several.
        all: Stop EVERY running {name} server without asking.
    """

_RESTART_DOC = """Stop the server and serve the workspace again — the dev loop in one command.

    A fresh process picks up changed code. A never-saved workspace is CONTINUED in its own folder when {name} keeps
    one: bare (when the last served workspace was a never-saved one), by its port, or by the type of the one running
    or served last.

    Args:
        target: What to serve after the restart — empty = the LAST served workspace (file or type; an empty workspace
            when nothing was served yet), a workspace file, a type name, or the PORT a server runs on
            (``{name} restart 8790``: whatever serves 8790, again on 8790; a port held by a program that is not {name}
            is refused, as ``stop`` refuses it, before anything is stopped).
        port: Serve on THIS port after the restart instead of the workspace's own (with a port target, the server
            named is stopped and comes back here).
        background: Serve in a DETACHED process and hand the terminal back (``-b``).
        browser: Open the default browser on the address — ``never`` (default: the tab of the server stopped is
            usually still open); ``new``: once the restarted server answers; ``always``: also when a {name} already
            answers there.
    """

_LIST_DOC = """List every running {name} server: its port, the workspace it serves and its URL.

    One line per server, lowest port first, the URL last so a terminal makes it clickable. Discovery is the live probe
    ``stop`` uses (the registry plus the default port, each verified by {name}'s own meta answer), so a stale registry
    entry never lists.
    """


def add_lifecycle_commands(
    app: LiquifyApp,
    spec: AppSpec,
    *,
    open_file: Callable[..., Served],
    open_empty: Callable[[], Served],
    open_type: Optional[Callable[[str], Served]] = None,
    type_names: Optional[Callable[[], List[str]]] = None,
    create_app: Optional[Callable[[serving.Server], "Flask"]] = None,
    commands: Optional[Collection[str]] = None,
) -> Lifecycle:
    """Register the lifecycle commands on the app's own liquifai CLI, and answer the :class:`Lifecycle` behind them.

    ``open`` (the DEFAULT command: ``<name> [workspace.yaml]``), ``workspace <type>`` (only with ``open_type``),
    ``stop [target] [--all]``, ``restart [target]`` and ``list``. The serving commands take ``--auto-port``,
    ``--port``, ``--background`` / ``-b`` and ``--browser always|new|never``.

    Build the app with ``LiquifyApp(strict_flags=True)`` — the app's choice: an option a command does not declare is
    then refused, never dropped (a misspelled ``--al`` names ``--all`` instead of stopping every server).

    Args:
        app: The app's CLI.
        spec: The app.
        open_file: Open a workspace file (its absolute path) into a live workspace.
        open_empty: An empty workspace.
        open_type: Open a type workspace by name; ``None``: no ``workspace`` command.
        type_names: Every type ``open_type`` opens (TAB completions too).
        create_app: The app's Flask app for a :class:`~portail.serving.Server` (the shell mounted in it); unset → the
            bare shell.
        commands: The commands to register, by name — unset: all of them. An app whose serving commands take options
            of its own registers those itself (calling :meth:`Lifecycle.open` / :meth:`Lifecycle.restart` with
            ``options=``) and asks for the rest: ``commands=("stop", "list")``.
    """
    wanted = set(commands) if commands is not None else {"open", "workspace", "stop", "restart", "list"}
    life = Lifecycle(
        spec,
        open_file=open_file,
        open_empty=open_empty,
        open_type=open_type,
        type_names=type_names,
        create_app=create_app,
    )
    words = {"name": spec.name, "default_port": spec.default_port}

    def open_(
        workspace: str = "",
        auto_port: bool = False,
        port: int = 0,
        background: bool = False,
        browser: BrowserChoice = "new",
    ) -> None:
        life.open(workspace, auto_port=auto_port, port=port, background=background, browser=browser)

    open_.__doc__ = _OPEN_DOC.format(**words)
    if "open" in wanted:
        app.command(name="open", default=True, positionals=["workspace"], short={"b": "background"})(open_)

    if open_type is not None and "workspace" in wanted:

        def workspace(
            type: str = "",
            auto_port: Optional[bool] = None,
            port: int = 0,
            background: bool = False,
            browser: BrowserChoice = "new",
        ) -> None:
            life.workspace(type, auto_port=auto_port, port=port, background=background, browser=browser)

        workspace.__doc__ = _WORKSPACE_DOC.format(**words)
        app.command(
            name="workspace", positionals=["type"], completions={"type": life.type_names}, short={"b": "background"}
        )(workspace)

    def stop(workspace: str = "", all: bool = False) -> None:
        life.stop(workspace, all=all)

    stop.__doc__ = _STOP_DOC.format(**words)
    if "stop" in wanted:
        app.command(name="stop", positionals=["workspace"])(stop)

    def restart(target: str = "", port: int = 0, background: bool = False, browser: BrowserChoice = "never") -> None:
        life.restart(target, port=port, background=background, browser=browser)

    restart.__doc__ = _RESTART_DOC.format(**words)
    if "restart" in wanted:
        app.command(
            name="restart", positionals=["target"], completions={"target": life.type_names}, short={"b": "background"}
        )(restart)

    def list_() -> None:
        life.list()

    list_.__doc__ = _LIST_DOC.format(**words)
    if "list" in wanted:
        app.command(name="list")(list_)
    return life


__all__ = ["Continuation", "Lifecycle", "add_lifecycle_commands"]
