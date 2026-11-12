"""A never-saved workspace is CONTINUED by a restart — in its own folder, with everything it held — when the app says
how (``Hooks.continued``); the demo app does.

A never-saved workspace (``demoapp workspace <type>``, bare ``demoapp``) works out of a temp folder. Measured with an
app on the same seam, before continuation: a restart resolved the target to a FRESH type workspace in a NEW temp
folder and came back EMPTY, and the next start's sweep removed the old, unlocked folder with the person's work in it.

The behaviour these pins hold:

1. a restart that names a never-saved workspace — bare (the last served), by its port, or by the type of the one
   running or served last — CONTINUES it in the same folder; the server keeps its work there on its way out
   (``Hooks.leaving``) and the continuing process opens it (``Hooks.continued``); ``--background`` hands the folder to
   the child;
2. ``stop`` of that workspace says how to continue it;
3. a fresh never-saved start while one with work is left behind warns, then starts.

The con cases — a saved workspace's restart and stop, ``restart <another type>``, an untouched never-saved workspace,
a fresh start with nothing behind it, an app whose restarts continue nothing — are pinned beside each.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from liquifai import LiquifyApp

from portaide import AppSpec, serving
from portaide.cli import add_lifecycle_commands
from portaide.errors import ServeError
from tests.demo_cli import DEMO_PORT, DEMO_SPEC, HELD, KEPT, WORK_FILE, DemoWorkspace, continued, lifecycle, open_type

D = DEMO_PORT
CONTINUE = DEMO_SPEC.env("continue_folder")


def _folder() -> Path:
    """A never-saved workspace's temp folder, where the suite's temp workspaces live — held by nobody."""
    return Path(tempfile.mkdtemp(prefix="demoapp-"))


def _temp_folders() -> List[str]:
    return sorted(p.name for p in Path(tempfile.gettempdir()).glob("demoapp-*"))


def _continued(folder: Path, template: str = "detection", port: int = 8787) -> DemoWorkspace:
    """A workspace working out of ``folder`` — what a server serving that folder holds."""
    workspace = continued(folder, template)
    workspace.port = port
    return workspace


def _left_behind(folder: Path, template: str = "detection", text_size: int = 20) -> None:
    """What a stopped server leaves in its folder: its document, written by the demo's own ``leaving``."""
    left = _continued(folder, template)
    left.text_size = text_size
    assert left.keep() is not None


def _served_last(folder: Path, template: str = "detection", port: int = 8787) -> None:
    """The CLI's memory of a never-saved workspace served: its spec and its folder, as a serving process writes them."""
    serving.remember(DEMO_SPEC, f"type:{template}" if template else "empty")
    serving.record_unsaved(DEMO_SPEC, _continued(folder, template, port))


def _register(state: Path, port: int) -> None:
    directory = state / "servers"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{port}.json").write_text(json.dumps({"host": "127.0.0.1", "port": port, "pid": 1}))


class Servers:
    """Fake running servers: the meta probe answers from ``alive`` (carrying each one's folder), a stop pops the port
    and runs what the real server does on its way out (``on_stop``); ``foreign`` ports are held by a program that is
    not the app."""

    def __init__(
        self,
        monkeypatch: Any,
        alive: Optional[Dict[int, Dict[str, str]]] = None,
        on_stop: Optional[Dict[int, Callable[[], None]]] = None,
        foreign: Optional[Dict[int, int]] = None,
    ) -> None:
        self.alive = dict(alive or {})
        self.on_stop = dict(on_stop or {})
        self.foreign = dict(foreign or {})
        self.stopped: List[int] = []
        monkeypatch.setattr(serving, "meta_at", self._meta)
        monkeypatch.setattr(serving, "port_in_use", lambda host, port: port in self.alive or port in self.foreign)
        monkeypatch.setattr(serving, "port_holder", lambda port: f" (pid {self.foreign[port]} postgres)")
        monkeypatch.setattr(serving, "stop_port", self._stop)
        monkeypatch.setattr(serving, "pids_listening", self._pids)

    def _pids(self, port: int) -> List[int]:
        if port in self.alive:
            return [port * 10]
        return [self.foreign[port]] if port in self.foreign else []

    def _meta(self, host: str, port: int) -> Optional[Dict[str, Any]]:
        info = self.alive.get(port)
        if info is None:
            return None
        return {"app": "demoapp", "title": "x", "workspace": {"path": "", "template": "", "state_dir": "", **info}}

    def _stop(self, name: str, port: int) -> str:
        if port not in self.alive:
            return f"{name}: nothing listening on port {port}"
        self.alive.pop(port)
        self.stopped.append(port)
        if port in self.on_stop:
            self.on_stop[port]()
        return f"{name}: stopped pid {port * 10} (port {port})"


def _capture_serve(monkeypatch: Any) -> List[Tuple[Any, Dict[str, Any]]]:
    served: List[Tuple[Any, Dict[str, Any]]] = []
    monkeypatch.setattr(lifecycle, "serve", lambda target, **kwargs: served.append((target, kwargs)))
    monkeypatch.setattr(serving, "tell_server_it_restarts", lambda host, port: None)
    return served


class TestARestartContinuesTheNeverSavedWorkspace:
    def test_a_bare_restart_continues_the_last_one_in_its_own_folder(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        before = _temp_folders()
        lifecycle.restart()
        ((workspace, kwargs),) = served
        assert capsys.readouterr().out == (
            f"continuing the never-saved detection type workspace from {folder}\n"
            "demoapp: nothing listening on port 8193\n"
        )
        assert workspace.state_dir() == folder and workspace.path == "" and workspace.template == "detection"
        assert workspace.text_size == 20, "the whole document comes back"
        assert workspace.port == 8193, "where it was served"
        assert kwargs.get("continuing") == folder
        assert _temp_folders() == before, "no second temp folder is minted"
        assert (state / "last-workspace").read_text().strip() == "type:detection"

    def test_a_running_server_is_stopped_FIRST_and_its_work_read_after(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """The work is kept by the server on its way out — so the stop comes before the read."""
        folder = _folder()
        _served_last(folder, port=8193)
        _register(state, 8193)
        Servers(
            monkeypatch,
            alive={8193: {"template": "detection", "state_dir": str(folder)}},
            on_stop={8193: lambda: _left_behind(folder)},
        )
        served = _capture_serve(monkeypatch)
        lifecycle.restart()
        ((workspace, _kwargs),) = served
        assert capsys.readouterr().out == (
            f"continuing the never-saved detection type workspace from {folder}\n"
            "demoapp: stopped pid 81930 (port 8193)\n"
        )
        assert workspace.state_dir() == folder and workspace.text_size == 20 and workspace.port == 8193

    def test_restart_by_port_continues_the_never_saved_workspace_serving_there(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """A server the record does not know is found by its live answer."""
        folder = _folder()
        servers = Servers(
            monkeypatch,
            alive={8193: {"template": "detection", "state_dir": str(folder)}},
            on_stop={8193: lambda: _left_behind(folder)},
        )
        served = _capture_serve(monkeypatch)
        lifecycle.restart("8193")
        ((workspace, _kwargs),) = served
        assert servers.stopped == [8193]
        assert workspace.state_dir() == folder and workspace.text_size == 20 and workspace.port == 8193
        assert capsys.readouterr().out.startswith(
            f"continuing the never-saved detection type workspace from {folder}\n"
        )

    def test_restart_by_port_beside_a_program_that_is_not_the_app_still_continues(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """The refusal of a foreign holder is for the port NAMED: the never-saved one on 8193 is continued while a
        program that is not the app holds 8197; naming 8197 itself is refused before anything is stopped."""
        folder = _folder()
        servers = Servers(
            monkeypatch,
            alive={8193: {"template": "detection", "state_dir": str(folder)}},
            on_stop={8193: lambda: _left_behind(folder)},
            foreign={8197: 29923},
        )
        served = _capture_serve(monkeypatch)
        with pytest.raises(ServeError, match=r"^port 8197 is held by something that is not demoapp \(pid 29923"):
            lifecycle.restart("8197")
        assert servers.stopped == [] and served == []
        capsys.readouterr()
        lifecycle.restart("8193")
        ((workspace, _kwargs),) = served
        assert servers.stopped == [8193]
        assert workspace.state_dir() == folder and workspace.text_size == 20 and workspace.port == 8193

    def test_restart_by_the_type_of_the_one_served_last_continues_it(self, state: Path, monkeypatch: Any) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart("detection")
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == folder and workspace.text_size == 20

    def test_restart_by_another_type_opens_it_fresh(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart("classification")
        ((workspace, _kwargs),) = served
        assert workspace.template == "classification" and workspace.state_dir() != folder
        assert capsys.readouterr().out == f"demoapp: nothing listening on port {D}\n"

    def test_restarting_a_saved_workspace_prints_exactly_what_it_did(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        target = tmp_path / "w.workspace.yaml"
        DemoWorkspace().save(str(target))
        serving.remember(DEMO_SPEC, f"file:{target.resolve()}")
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart()
        ((workspace, _kwargs),) = served
        assert workspace.path == str(target.resolve())
        assert capsys.readouterr().out == f"restarting {target.resolve()}\ndemoapp: nothing listening on port {D}\n"

    def test_restarting_a_workspace_file_by_name_opens_it(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        target = tmp_path / "w.workspace.yaml"
        DemoWorkspace().save(str(target))
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart(str(target))
        ((workspace, _kwargs),) = served
        assert workspace.path == str(target.resolve())
        assert capsys.readouterr().out == f"demoapp: nothing listening on port {D}\n"

    def test_an_untouched_stopped_workspace_is_not_continued(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        """No work kept, not running — nothing to continue; the restart opens the type."""
        folder = _folder()
        _served_last(folder, port=8193)
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart()
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() != folder
        assert capsys.readouterr().out == (
            f"restarting the detection type workspace\ndemoapp: nothing listening on port {D}\n"
        )

    def test_any_work_the_app_counts_is_continued(self, state: Path, monkeypatch: Any) -> None:
        """``Hooks.holds_work`` decides what work is — the demo's ``work.txt`` without any document."""
        folder = _folder()
        (folder / WORK_FILE).write_text("x")
        _served_last(folder, port=8193)
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart()
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == folder and workspace.text_size == 12, "the type's own settings"

    def test_port_moves_it_and_it_is_still_the_same_folder(self, state: Path, monkeypatch: Any) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart(port=8194)
        ((workspace, _kwargs),) = served
        assert workspace.port == 8194 and workspace.state_dir() == folder


class TestWhichNeverSavedWorkspaceARestartNames:
    def test_a_bare_restart_finds_the_one_never_saved_server_the_record_does_not_know(
        self, state: Path, monkeypatch: Any
    ) -> None:
        folder = _folder()
        serving.remember(DEMO_SPEC, "type:detection")
        _register(state, 8193)
        Servers(monkeypatch, alive={8193: {"template": "detection", "state_dir": str(folder)}})
        served = _capture_serve(monkeypatch)
        lifecycle.restart()
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == folder and workspace.port == 8193

    def test_restart_by_type_prefers_the_running_one_served_last(self, state: Path, monkeypatch: Any) -> None:
        older, newer = _folder(), _folder()
        _served_last(older, port=8193)
        _served_last(newer, port=8194)
        for port in (8193, 8194):
            _register(state, port)
        Servers(
            monkeypatch,
            alive={
                8193: {"template": "detection", "state_dir": str(older)},
                8194: {"template": "detection", "state_dir": str(newer)},
            },
        )
        served = _capture_serve(monkeypatch)
        lifecycle.restart("detection")
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == newer and workspace.port == 8194

    def test_several_running_and_none_recorded_is_refused_naming_the_ports(self, state: Path, monkeypatch: Any) -> None:
        for port in (8193, 8194):
            _register(state, port)
        servers = Servers(
            monkeypatch,
            alive={
                8193: {"template": "detection", "state_dir": str(_folder())},
                8194: {"template": "detection", "state_dir": str(_folder())},
            },
        )
        served = _capture_serve(monkeypatch)
        with pytest.raises(ServeError, match=r"^2 never-saved detection type workspaces are running \(ports 8193, "):
            lifecycle.restart("detection")
        assert served == [] and servers.stopped == [], "nothing is stopped on a guess"

    def test_an_empty_workspace_is_continued_as_one(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        folder = _folder()
        _left_behind(folder, template="")
        _served_last(folder, template="", port=8193)
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart()
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == folder and workspace.template == "" and workspace.text_size == 20
        assert capsys.readouterr().out.startswith(f"continuing the never-saved empty workspace from {folder}\n")
        assert (state / "last-workspace").read_text().strip() == "empty"

    def test_a_record_that_cannot_be_read_names_nothing(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        state.mkdir(parents=True)
        (state / serving.UNSAVED_RECORD).write_text("{not json")
        serving.remember(DEMO_SPEC, "type:detection")
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart()
        ((workspace, _kwargs),) = served
        assert workspace.template == "detection"
        assert capsys.readouterr().out.startswith("restarting the detection type workspace\n")

    def test_an_empty_workspace_without_work_is_continued_from_its_port(self, state: Path, monkeypatch: Any) -> None:
        folder = _folder()
        Servers(monkeypatch, alive={8193: {"template": "", "state_dir": str(folder)}})
        served = _capture_serve(monkeypatch)
        lifecycle.restart("8193")
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == folder and workspace.template == "" and workspace.port == 8193


class TestTheRecord:
    def test_the_record_makes_its_own_folder(self, state: Path) -> None:
        serving.record_unsaved(DEMO_SPEC, _continued(_folder()))
        assert (state / serving.UNSAVED_RECORD).is_file()

    def test_a_record_that_cannot_be_written_never_stops_a_start(self, state: Path) -> None:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("a file where the folder should be")
        serving.record_unsaved(DEMO_SPEC, _continued(_folder()))  # said in the log at debug, never raised
        assert state.is_file()

    def test_the_same_folder_is_listed_once_and_a_gone_one_is_dropped(self, state: Path) -> None:
        kept, gone = _folder(), _folder()
        (kept / WORK_FILE).write_text("x")
        serving.record_unsaved(DEMO_SPEC, _continued(gone))
        serving.record_unsaved(DEMO_SPEC, _continued(kept))
        shutil.rmtree(gone)
        serving.record_unsaved(DEMO_SPEC, _continued(kept))
        recorded = [Path(entry["folder"]) for entry in json.loads((state / serving.UNSAVED_RECORD).read_text())]
        assert recorded == [kept]

    def test_stopping_an_OLDER_one_while_a_newer_one_holds_work_promises_nothing(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """A bare restart would continue the newer one — so the older one's stop names no command."""
        older, newer = _folder(), _folder()
        _served_last(older, port=8193)
        _left_behind(newer)
        _served_last(newer, port=8194)
        Servers(
            monkeypatch,
            alive={8193: {"template": "detection", "state_dir": str(older)}},
            on_stop={8193: lambda: _left_behind(older)},
        )
        lifecycle.stop("8193")
        assert capsys.readouterr().out == "demoapp (the detection type workspace): stopped pid 81930 (port 8193)\n"

    def test_an_empty_workspace_with_a_saved_file_served_since_gets_no_note(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """No command reaches it — so no line promises one."""
        folder = _folder()
        _served_last(folder, template="", port=8193)
        serving.remember(DEMO_SPEC, f"file:{tmp_path / 'other.workspace.yaml'}")
        Servers(
            monkeypatch,
            alive={8193: {"template": "", "state_dir": str(folder)}},
            on_stop={8193: lambda: _left_behind(folder, template="")},
        )
        lifecycle.stop("8193")
        assert capsys.readouterr().out == "demoapp (an empty workspace): stopped pid 81930 (port 8193)\n"


class _Popen:
    def __init__(self, monkeypatch: Any, servers: Servers, port: int) -> None:
        self.argv: List[str] = []
        self.env: Dict[str, str] = {}
        self.pid = 4242
        self._servers, self._port = servers, port
        monkeypatch.setattr(serving.subprocess, "Popen", self._popen)

    def _popen(self, argv: List[str], **kwargs: Any) -> "_Popen":
        self.argv, self.env = list(argv), dict(kwargs.get("env") or {})
        self._servers.alive[self._port] = {"template": "detection"}  # the child answers
        return self

    def poll(self) -> Optional[int]:
        return None


class TestTheBackgroundChildContinuesTheSameFolder:
    def test_the_parent_hands_the_folder_down(self, state: Path, monkeypatch: Any) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        servers = Servers(monkeypatch)
        spawn = _Popen(monkeypatch, servers, 8193)
        before = _temp_folders()
        lifecycle.restart(background=True)
        assert spawn.env.get(CONTINUE) == str(folder)
        assert spawn.argv[3:] == ["workspace", "detection", "--port", "8193", "--browser", "never"]
        assert _temp_folders() == before, "and the parent minted no folder"

    def test_the_child_takes_the_folder_handed_down_and_removes_the_variable(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        folder = _folder()
        _left_behind(folder)
        monkeypatch.setenv(CONTINUE, str(folder))
        served = _capture_serve(monkeypatch)
        before = _temp_folders()
        lifecycle.workspace("detection", port=8193)
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == folder and workspace.text_size == 20 and workspace.port == 8193
        assert CONTINUE not in os.environ, "it reaches no further than this start"
        assert _temp_folders() == before
        assert capsys.readouterr().out == "", "a continuation is no fresh start: no warning"

    def test_a_bare_start_takes_a_folder_handed_down(self, state: Path, monkeypatch: Any) -> None:
        folder = _folder()
        _left_behind(folder, template="")
        monkeypatch.setenv(CONTINUE, str(folder))
        served = _capture_serve(monkeypatch)
        lifecycle.open()
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == folder and workspace.text_size == 20

    def test_opening_a_file_drops_a_folder_handed_down(self, state: Path, tmp_path: Path, monkeypatch: Any) -> None:
        target = tmp_path / "w.workspace.yaml"
        DemoWorkspace().save(str(target))
        monkeypatch.setenv(CONTINUE, str(_folder()))
        served = _capture_serve(monkeypatch)
        lifecycle.open(str(target))
        ((workspace, _kwargs),) = served
        assert workspace.path == str(target.resolve())
        assert CONTINUE not in os.environ

    def test_a_folder_handed_down_that_is_gone_is_refused_naming_it(self, state: Path, monkeypatch: Any) -> None:
        gone = Path(tempfile.gettempdir()) / "demoapp-gone"
        monkeypatch.setenv(CONTINUE, str(gone))
        _capture_serve(monkeypatch)
        with pytest.raises(ServeError) as refused:
            lifecycle.workspace("detection")
        assert str(refused.value) == (
            f"the never-saved workspace's folder {gone} handed to this start is gone — nothing to continue;"
            " 'demoapp workspace detection' starts a new one"
        )


class TestTheServingProcessRemembersTheFolder:
    def test_a_never_saved_workspace_that_serves_is_recorded(self, state: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(serving.Server, "serve", lambda self: None)
        workspace = open_type("detection")
        workspace.port = 8193
        lifecycle.serve(workspace)
        (entry,) = json.loads((state / serving.UNSAVED_RECORD).read_text())
        assert Path(entry["folder"]) == workspace.state_dir()
        assert entry["template"] == "detection" and entry["port"] == 8193

    def test_a_saved_workspace_is_not_recorded(self, state: Path, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(serving.Server, "serve", lambda self: None)
        workspace = DemoWorkspace()
        workspace.save(str(tmp_path / "w.workspace.yaml"))
        lifecycle.serve(workspace)
        assert not (state / serving.UNSAVED_RECORD).exists()

    def test_a_start_that_serves_nothing_records_nothing(self, state: Path, monkeypatch: Any) -> None:
        """The port already serves it — the already-running report, no server, no record."""
        monkeypatch.setattr(serving, "port_in_use", lambda host, port: True)
        monkeypatch.setattr(
            serving, "meta_at", lambda host, port: {"app": "demoapp", "workspace": {"template": "detection"}}
        )
        monkeypatch.setattr(serving.Server, "serve", lambda self: None)
        lifecycle.serve(open_type("detection"))
        assert not (state / serving.UNSAVED_RECORD).exists()


class TestStopSaysHowToContinueIt:
    def test_stopping_the_one_a_bare_restart_continues_says_where_its_work_is(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        folder = _folder()
        _served_last(folder, port=8193)
        _register(state, 8193)
        Servers(
            monkeypatch,
            alive={8193: {"template": "detection", "state_dir": str(folder)}},
            on_stop={8193: lambda: _left_behind(folder)},
        )
        lifecycle.stop()
        assert capsys.readouterr().out == (
            "demoapp (the detection type workspace): stopped pid 81930 (port 8193)\n"
            f"demoapp: the detection type workspace was never saved — its work is kept in {folder} {KEPT};"
            " 'demoapp restart' continues it\n"
        )

    def test_with_a_saved_workspace_served_since_it_names_the_type(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """A bare restart would restart the saved file now — ``restart <type>`` is what continues it."""
        folder = _folder()
        _served_last(folder, port=8193)
        serving.remember(DEMO_SPEC, f"file:{tmp_path / 'other.workspace.yaml'}")
        Servers(
            monkeypatch,
            alive={8193: {"template": "detection", "state_dir": str(folder)}},
            on_stop={8193: lambda: _left_behind(folder)},
        )
        lifecycle.stop("8193")
        assert capsys.readouterr().out.endswith("; 'demoapp restart detection' continues it\n")

    def test_an_untouched_never_saved_workspace_says_nothing_more(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        folder = _folder()
        _served_last(folder, port=8193)
        _register(state, 8193)
        Servers(monkeypatch, alive={8193: {"template": "detection", "state_dir": str(folder)}})
        lifecycle.stop()
        assert capsys.readouterr().out == "demoapp (the detection type workspace): stopped pid 81930 (port 8193)\n"

    def test_a_saved_workspaces_stop_is_unchanged(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        target = tmp_path / "w.workspace.yaml"
        workspace = DemoWorkspace()
        workspace.save(str(target))
        _register(state, 8193)
        Servers(monkeypatch, alive={8193: {"path": workspace.path, "state_dir": workspace.path + ".d"}})
        lifecycle.stop()
        assert capsys.readouterr().out == f"demoapp ({workspace.path}): stopped pid 81930 (port 8193)\n"


class TestAFreshStartWarnsAboutWorkLeftBehind:
    def _warning(self, folder: Path) -> str:
        return (
            f"demoapp: the never-saved detection type workspace served last holds work in {folder}, kept {KEPT}"
            " — starting a new one; to continue that one instead, stop this one ('demoapp stop') and run"
            " 'demoapp restart'\n"
        )

    def test_a_new_type_workspace_warns_then_starts(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        Servers(monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.workspace("detection", port=8193)
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() != folder, "no question asked: the new one starts"
        assert capsys.readouterr().out == self._warning(folder)

    def test_a_bare_start_warns_too(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        Servers(monkeypatch)
        _capture_serve(monkeypatch)
        lifecycle.open()
        assert capsys.readouterr().out == self._warning(folder)

    def test_nothing_behind_prints_nothing_new(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        Servers(monkeypatch)
        _capture_serve(monkeypatch)
        lifecycle.workspace("detection")
        assert capsys.readouterr().out == ""

    def test_an_untouched_one_behind_prints_nothing(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        _served_last(_folder(), port=8193)
        Servers(monkeypatch)
        _capture_serve(monkeypatch)
        lifecycle.workspace("detection")
        assert capsys.readouterr().out == ""

    def test_one_still_running_is_no_warning(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        _register(state, 8193)
        Servers(monkeypatch, alive={8193: {"template": "detection", "state_dir": str(folder)}})
        _capture_serve(monkeypatch)
        lifecycle.workspace("detection", port=8194)
        assert capsys.readouterr().out == ""

    def test_opening_a_workspace_file_never_warns(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        target = tmp_path / "w.workspace.yaml"
        DemoWorkspace().save(str(target))
        Servers(monkeypatch)
        _capture_serve(monkeypatch)
        lifecycle.open(str(target))
        assert capsys.readouterr().out == ""

    def test_the_advice_holds_stop_the_new_untouched_one_and_restart_continues_the_old(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        old = _folder()
        _left_behind(old)
        _served_last(old, port=8193)
        Servers(monkeypatch)
        monkeypatch.setattr(serving.Server, "serve", lambda self: None)
        lifecycle.workspace("detection", port=8193)  # the new one serves (and is recorded), then stops untouched
        capsys.readouterr()
        served = _capture_serve(monkeypatch)
        lifecycle.restart()
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == old and workspace.text_size == 20


class TestASaveEndsTheContinuation:
    """After the first Save of the one served last, a bare restart stopped the server of the just-saved file and
    served an OLDER never-saved workspace in its place (measured with an app on the same seam)."""

    @staticmethod
    def _saved_since(state: Path, tmp_path: Path) -> Tuple[Path, Path]:
        """The older one stopped with work; the newer one served after it, then SAVED — its folder EMPTIED, as a first
        Save that moves every file to the saved file's folder leaves it — and still serving on 8193."""
        older = _folder()
        _left_behind(older)
        _served_last(older, port=8193)
        newer = _folder()
        _served_last(newer, port=8193)
        saving = _continued(newer)
        saving.save(str(tmp_path / "b.workspace.yaml"))
        (newer / HELD).unlink()  # the Save moved it with the rest
        assert newer.is_dir() and not any(newer.iterdir()), "what a first Save leaves where it was"
        _register(state, 8193)
        return older, Path(saving.path + ".d")

    def test_a_bare_restart_continues_no_older_workspace_and_stops_no_saved_one(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        older, moved = self._saved_since(state, tmp_path)
        servers = Servers(
            monkeypatch, alive={8193: {"path": str(tmp_path / "b.workspace.yaml"), "state_dir": str(moved)}}
        )
        served = _capture_serve(monkeypatch)
        lifecycle.restart()
        ((workspace, _kwargs),) = served
        assert servers.stopped == [], "the saved workspace's server is left alone"
        assert workspace.state_dir() != older and workspace.template == "detection"
        assert capsys.readouterr().out == (
            f"restarting the detection type workspace\ndemoapp: nothing listening on port {D}\n"
        )

    def test_the_next_record_drops_the_emptied_folder(self, state: Path, tmp_path: Path) -> None:
        older, _moved = self._saved_since(state, tmp_path)
        newest = _folder()
        serving.record_unsaved(DEMO_SPEC, _continued(newest))
        recorded = [Path(entry["folder"]) for entry in json.loads((state / serving.UNSAVED_RECORD).read_text())]
        assert recorded == [newest, older]

    def test_a_fresh_start_after_the_save_names_no_older_workspace(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        self._saved_since(state, tmp_path)
        Servers(monkeypatch)
        _capture_serve(monkeypatch)
        lifecycle.workspace("detection", port=8195)
        assert capsys.readouterr().out == ""


class TestAStoppedOneNeverTakesASavedWorkspacesPort:
    """The never-saved workspace a restart continues was served on a port a SAVED workspace serves now. Coming back
    there would stop it — so the restart refuses, naming both."""

    @staticmethod
    def _setup(state: Path, tmp_path: Path, monkeypatch: Any) -> Tuple[Path, Servers]:
        older = _folder()
        _left_behind(older)
        _served_last(older, port=8193)
        _served_last(_folder(), port=8195)  # the one served last: untouched and stopped — walked past
        _register(state, 8193)
        servers = Servers(
            monkeypatch,
            alive={8193: {"path": str(tmp_path / "b.workspace.yaml"), "state_dir": str(tmp_path / "b.d")}},
        )
        return older, servers

    @pytest.mark.parametrize("target", ["", "detection"])
    def test_it_refuses_naming_both_and_stops_nothing(
        self, target: str, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        older, servers = self._setup(state, tmp_path, monkeypatch)
        served = _capture_serve(monkeypatch)
        with pytest.raises(ServeError) as refused:
            lifecycle.restart(target)
        again = f"demoapp restart {target}".strip()
        assert str(refused.value) == (
            f"port 8193 serves {tmp_path / 'b.workspace.yaml'}, a saved workspace — not stopping it to bring back the"
            f" never-saved detection type workspace from {older}; 'demoapp restart 8193' restarts that one,"
            f" '{again} --port 8194' continues the never-saved one on a free port"
        )
        assert servers.stopped == [] and served == []
        assert capsys.readouterr().out == "", "nothing announced, nothing stopped"

    def test_the_port_the_refusal_names_continues_it_there(self, state: Path, tmp_path: Path, monkeypatch: Any) -> None:
        older, servers = self._setup(state, tmp_path, monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart(port=8194)
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == older and workspace.port == 8194 and servers.stopped == []

    def test_a_port_named_on_the_command_line_is_taken_over_as_every_restart_does(
        self, state: Path, tmp_path: Path, monkeypatch: Any
    ) -> None:
        older, servers = self._setup(state, tmp_path, monkeypatch)
        served = _capture_serve(monkeypatch)
        lifecycle.restart(port=8193)
        ((workspace, _kwargs),) = served
        assert workspace.state_dir() == older and servers.stopped == [8193]


class TestPortMovesARunningOneOntoABusyPort:
    """``--port N`` while the never-saved server runs and N is busy stopped the running server, then refused with advice
    (``--auto-port``) that restart does not take — nothing served afterwards (measured)."""

    @staticmethod
    def _running(state: Path, monkeypatch: Any, **extra: Any) -> Tuple[Path, Servers]:
        folder = _folder()
        _served_last(folder, port=8193)
        _register(state, 8193)
        servers = Servers(
            monkeypatch,
            alive={8193: {"template": "detection", "state_dir": str(folder)}, **extra.get("alive", {})},
            on_stop={8193: lambda: _left_behind(folder)},
            foreign=extra.get("foreign"),
        )
        return folder, servers

    @pytest.mark.parametrize("target", ["", "detection", "8193"])
    def test_it_stops_what_serves_the_port_it_moves_to_as_every_restart_does(
        self, target: str, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        saved = {"path": str(tmp_path / "s.workspace.yaml"), "state_dir": str(tmp_path / "s.d")}
        folder, servers = self._running(state, monkeypatch, alive={8194: saved})
        served = _capture_serve(monkeypatch)
        lifecycle.restart(target, port=8194)
        ((workspace, _kwargs),) = served
        assert servers.stopped == [8193, 8194]
        assert workspace.state_dir() == folder and workspace.port == 8194 and workspace.text_size == 20
        assert capsys.readouterr().out == (
            f"continuing the never-saved detection type workspace from {folder}\n"
            "demoapp: stopped pid 81930 (port 8193)\n"
            "demoapp: stopped pid 81940 (port 8194)\n"
        )

    def test_a_port_held_by_something_that_is_not_the_app_is_refused_before_anything_stops(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        _folder_, servers = self._running(state, monkeypatch, foreign={8194: 5555})
        served = _capture_serve(monkeypatch)
        with pytest.raises(ServeError) as refused:
            lifecycle.restart(port=8194)
        assert (
            str(refused.value)
            == "port 8194 is held by something that is not demoapp (pid 5555 postgres) — not stopping it"
        )
        assert servers.stopped == [] and served == [], "the never-saved server still runs"
        assert capsys.readouterr().out == ""


class TestTheAppsRefusalOfItsWork:
    """What a never-saved workspace kept is the app's to read: a document it refuses is refused (never half-loaded),
    with the app's own words — which may name the restart that reaches it (``serving.continue_command``)."""

    def test_a_stopped_ones_refusal_stops_nothing(self, state: Path, monkeypatch: Any) -> None:
        folder = _folder()
        _left_behind(folder)
        _served_last(folder, port=8193)
        servers = Servers(monkeypatch)
        served = _capture_serve(monkeypatch)

        def refuse(where: Path, template: str) -> DemoWorkspace:
            raise ValueError(
                f"{where}: not a document — then run '{serving.continue_command(DEMO_SPEC, where, template)}'"
            )

        monkeypatch.setattr(lifecycle, "continued", refuse)
        with pytest.raises(ValueError, match="then run 'demoapp restart'$"):
            lifecycle.restart()
        assert servers.stopped == [] and served == []

    def test_no_restart_reaches_a_folder_nobody_recorded(self, state: Path) -> None:
        assert serving.continue_command(DEMO_SPEC, _folder(), "detection") == ""


class TestAnAppThatContinuesNothing:
    """With the default hooks (no ``continued``) a restart never continues a never-saved workspace: it opens what it
    names, records nothing and promises nothing."""

    @pytest.fixture()
    def plain(self, monkeypatch: Any) -> Any:
        spec = AppSpec(name="demoapp", cli_module="tests.demo_cli", default_port=D)
        return add_lifecycle_commands(
            LiquifyApp(name="demoapp", strict_flags=True),
            spec,
            open_file=lambda path: DemoWorkspace(path=path),
            open_empty=DemoWorkspace,
            open_type=open_type,
            type_names=lambda: ["detection"],
        )

    def test_a_restart_by_port_opens_the_type_afresh(self, state: Path, plain: Any, monkeypatch: Any) -> None:
        folder = _folder()
        (folder / WORK_FILE).write_text("x")
        Servers(monkeypatch, alive={8193: {"template": "detection", "state_dir": str(folder)}})
        served: List[Any] = []
        monkeypatch.setattr(plain, "serve", lambda target, **kwargs: served.append(target))
        plain.restart("8193")
        (workspace,) = served
        assert workspace.template == "detection" and workspace.state_dir() != folder and workspace.port == 8193

    def test_nothing_is_recorded_kept_or_handed_down(self, state: Path, plain: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(serving.Server, "serve", lambda self: None)
        workspace = open_type("detection")
        plain.serve(workspace)
        assert not (state / serving.UNSAVED_RECORD).exists()
        assert serving.Server(plain.spec, workspace).leaving_note() == {"kept_in": "", "kept_until": ""}
        monkeypatch.setenv(plain.spec.env("continue_folder"), str(_folder()))
        assert serving.handed_down_folder(plain.spec, "demoapp") is None
        assert plain.spec.env("continue_folder") not in os.environ
        assert serving.last_unsaved(plain.spec) is None
        assert serving.never_saved_note(plain.spec, {"path": "", "state_dir": str(_folder())}) == ""


class TestHooksThatFail:
    """A hook never raises into the lifecycle: one that fails is logged and the stop or start goes on."""

    def test_holds_work_and_kept_until_that_fail_read_as_nothing(self, monkeypatch: Any) -> None:
        from portaide import Hooks

        def broken(*_: Any) -> Any:
            raise RuntimeError("boom")

        spec = AppSpec(
            name="demoapp", cli_module="x", hooks=Hooks(holds_work=broken, kept_until=broken, continued=continued)
        )
        assert serving.holds_work(spec, _folder()) is False
        assert serving.kept_until(spec) == ""
