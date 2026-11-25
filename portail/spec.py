"""What an app tells portail: who it is (:class:`AppSpec`), what it serves (:class:`Served`), and its hooks.

An app built on portail serves ONE object — its workspace — behind a Flask page. portail knows nothing of what the
workspace holds: it serves it, finds it again by its identity, stops and restarts it, and tells its pages when it
goes. Everything it needs to do so is here.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional, Protocol, runtime_checkable


@runtime_checkable
class Served(Protocol):
    """The workspace an app serves, as portail sees it.

    Attributes:
        host: The address the page is served on (``127.0.0.1``).
        port: The port the page is served on; the lifecycle commands may change it before serving.
        path: The workspace's file, absolute — ``""`` for a workspace never saved.
        template: The TYPE the workspace was opened from (``<app> workspace <type>``) — ``""`` for none.
        title: What the page and every listing call it.

    A workspace MAY also carry ``options``, a ``Dict[str, str]`` of the app's own serving options (``{"notes_dir":
    "/n"}``): each is handed to the command that serves it again (``--notes-dir /n``, a ``--background`` child), and
    answered under ``options`` by :meth:`info` so a restart serves with what the running server has.

    It MAY carry ``page`` too, a ``str``: the part of the address its browser tab opens on (``?item=7`` →
    ``http://127.0.0.1:8790/?item=7``), for a start that named one thing of the workspace. It is read when the browser
    is opened and nowhere else: never registered, never handed to a ``--background`` child or a restart.
    """

    host: str
    port: int
    path: str
    template: str
    title: str

    def state_dir(self) -> Path:
        """The folder the workspace keeps its state in (a never-saved one: its temp folder). Cheap; never raises."""
        ...

    def info(self) -> Dict[str, Any]:
        """The JSON-safe description ``/api/meta`` answers under ``workspace``.

        It carries at least ``path``, ``template`` and ``state_dir`` (as text): discovery reads the LIVE answer, never
        a registry copy, to say what a server serves.
        """
        ...


def _nothing(*_: Any) -> None:
    return None


def _no_work(_: Path) -> bool:
    return False


def _no_date() -> str:
    return ""


@dataclass(frozen=True)
class Hooks:
    """What the app does at the moments serving passes through. Every hook is optional and never raises into serving.

    Attributes:
        prepare: Run in the TERMINAL before anything is registered or forked (a ``--background`` child is started only
            after it): checks whose answer the person must read, a page to build.
        starting: Run in the SERVING process before the page answers: startup jobs, a cleanup.
        leaving: Run in the serving process's way out, after the pages were told: keep unsaved work.
        holds_work: Whether a never-saved workspace's folder holds work a restart should continue. With the default
            (nothing is ever work) a never-saved workspace is never continued.
        kept_until: How long a never-saved workspace's work is kept, in words (``until 2026-12-01``) — for messages.
        continued: Open the never-saved workspace kept in a folder, of a type (``""``: an empty one) — a restart's
            continuation. ``None``: never-saved workspaces are never continued.
    """

    prepare: Callable[[Served], None] = _nothing
    starting: Callable[[Served], None] = _nothing
    leaving: Callable[[Served], None] = _nothing
    holds_work: Callable[[Path], bool] = _no_work
    kept_until: Callable[[], str] = _no_date
    continued: Optional[Callable[[Path, str], Served]] = None


@dataclass(frozen=True)
class AppSpec:
    """Who the app is.

    Attributes:
        name: The app's command name, in every message and file name (``<name> stop``, ``<name>.log``).
        cli_module: The module ``python -m`` runs to start the app again (a ``--background`` child, a ↻ restart).
        default_port: The port a workspace serves on unless its file or ``--port`` names another.
        default_host: The address a workspace serves on unless its file names another.
        workspace: What a workspace is on disk — a ``"file"`` (a workspace file, the default) or a ``"folder"``
            (the app serves a folder; there is no workspace file, and nothing is read from it before serving).
        workspace_key: The top-level key of a workspace file that holds the workspace (read tag-blind for its port).
        state_folder: The per-user state folder; unset → ``~/.<name>``, read when asked (a test's ``HOME`` holds).
        hooks: The app's :class:`Hooks`.
    """

    name: str
    cli_module: str
    default_port: int = 8787
    default_host: str = "127.0.0.1"
    workspace: Literal["file", "folder"] = "file"
    workspace_key: str = "workspace"
    state_folder: Optional[Path] = None
    hooks: Hooks = field(default_factory=Hooks)

    def state_dir(self) -> Path:
        """The per-user state folder (``~/.<name>`` unless :attr:`state_folder` names one). Not created here."""
        return self.state_folder if self.state_folder is not None else Path.home() / f".{self.name}"

    def env(self, word: str) -> str:
        """The name of one of the app's environment variables.

        ``env("continue_folder")`` → ``<NAME>_CONTINUE_FOLDER``.
        """
        stem = "".join(ch if ch.isalnum() else "_" for ch in self.name).upper()
        return f"{stem}_{word.upper()}"
