"""``--browser always|new|never`` — a start opens the default browser on the address it serves.

``new`` (the default of ``<name>`` and ``<name> workspace``) opens a tab only when THIS command started a new server
process: never on the already-running report. ``always`` opens one on that report too, ``never`` opens none.
``restart`` defaults to ``never`` (the edit-and-restart loop would leave one more tab per restart beside the one still
open). The tab opens once the server ANSWERS — opened before werkzeug binds, it shows the browser's "cannot connect"
page — and a background start's terminal opens it, never the background process.

Pinned with it: ``-b`` while the same workspace already answers starts no child and prints the already-running
report. Measured before: it printed ``serving … background pid 24754`` for a child that had already exited after
finding the port taken. The suite's autouse ``browser_opens`` fixture records every address instead of opening a tab.
"""

import inspect
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, get_args

import pytest

from portail import serving
from portail.cli import Continuation
from portail.errors import ServeError
from tests.cli_output import said
from tests.conftest import run
from tests.demo_cli import DEMO_PORT, DemoWorkspace, app, lifecycle
from tests.fleet import meta
from tests.test_background import Child, Spawn

D = DEMO_PORT
URL = f"http://127.0.0.1:{D}"


def _already_serving(monkeypatch: Any, answer: Optional[Dict[str, Any]]) -> None:
    """A server of the app answering ``answer`` on the default port already (``None``: another program holds it)."""
    monkeypatch.setattr(serving, "port_in_use", lambda host, port: port == D)
    monkeypatch.setattr(serving, "meta_at", lambda host, port: answer if port == D else None)


class Foreground:
    """A foreground server: ``serve()`` binds (its ``/api/meta`` answers from then on) and stays up until the tab
    opened — what a real server does, except it ends on its own instead of on Ctrl+C."""

    def __init__(self, monkeypatch: Any, browser_opens: List[str], answers: bool = True) -> None:
        self.events: List[str] = []
        self.answers = answers
        self.bound = False
        self.opened = threading.Event()
        monkeypatch.setattr(serving, "port_in_use", lambda host, port: False)
        monkeypatch.setattr(serving, "meta_at", self._probe)
        monkeypatch.setattr(serving.Server, "serve", lambda server: self._serve())
        monkeypatch.setattr(serving.webbrowser, "open", self._open)
        self._recorded = browser_opens

    def _probe(self, host: str, port: int) -> Optional[Dict[str, Any]]:
        return meta() if self.bound else None

    def _serve(self) -> None:
        self.events.append("bound")
        self.bound = self.answers
        if self.answers:
            assert self.opened.wait(10), "the tab never opened on a server that answers"

    def _open(self, url: str, *args: Any, **kwargs: Any) -> bool:
        self.events.append("opened")
        self._recorded.append(url)
        self.opened.set()
        return True


def _browser_threads() -> List[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == serving.browser_thread(lifecycle.spec)]


class TestTheOption:
    def test_the_values_are_the_literals(self) -> None:
        assert serving.BROWSER_CHOICES == get_args(serving.BrowserChoice) == ("always", "new", "never")

    @pytest.mark.parametrize("command, default", [("open", "new"), ("workspace", "new"), ("restart", "never")])
    def test_each_serving_command_declares_it_with_its_default(self, command: str, default: str) -> None:
        parameter = inspect.signature(app._commands[command]).parameters["browser"]
        assert parameter.default == default and parameter.annotation == serving.BrowserChoice

    @pytest.mark.parametrize(
        "argv",
        [
            ["--browser", "sometimes"],
            ["workspace", "classification", "--browser", "sometimes"],
            ["restart", "--browser", "sometimes"],
        ],
        ids=["open", "workspace", "restart"],
    )
    def test_a_value_outside_the_three_is_one_error_line_and_nothing_starts_or_stops(
        self, argv: List[str], monkeypatch: Any, capsys: Any, browser_opens: List[str]
    ) -> None:
        monkeypatch.setattr(lifecycle, "serve", lambda *a, **k: pytest.fail("a refused --browser serves nothing"))
        monkeypatch.setattr(serving, "stop_both", lambda *a, **k: pytest.fail("a refused --browser stops nothing"))
        with pytest.raises(SystemExit) as exit_info:
            run(argv, monkeypatch)
        assert exit_info.value.code == 1
        out = said(capsys)
        # liquifai refuses a value outside the three at the command line when it validates choices; an older one
        # hands it on, and portail's own check refuses it — one line naming the option and the value either way.
        assert "--browser must be" in out and "got 'sometimes'" in out
        assert "Traceback" not in out and browser_opens == []

    def test_a_python_caller_is_refused_too(self) -> None:
        with pytest.raises(ServeError, match="--browser must be always, new or never — got 'sometimes'"):
            lifecycle.open(browser="sometimes")


class TestAForegroundStart:
    def test_new_opens_the_tab_once_the_new_server_answers(self, monkeypatch: Any, browser_opens: List[str]) -> None:
        server = Foreground(monkeypatch, browser_opens)
        lifecycle.serve(DemoWorkspace(), browser="new")
        assert browser_opens == [URL]
        assert server.events == ["bound", "opened"], "never before the server answers"
        assert _browser_threads() == [], "the thread that waited is gone once serving ends"

    def test_always_opens_it_on_a_new_server_too(self, monkeypatch: Any, browser_opens: List[str]) -> None:
        Foreground(monkeypatch, browser_opens)
        lifecycle.serve(DemoWorkspace(), browser="always")
        assert browser_opens == [URL]

    def test_a_server_that_ends_without_ever_answering_opens_nothing(
        self, monkeypatch: Any, browser_opens: List[str]
    ) -> None:
        server = Foreground(monkeypatch, browser_opens, answers=False)
        lifecycle.serve(DemoWorkspace(), browser="new")
        assert server.events == ["bound"] and browser_opens == []
        assert _browser_threads() == []

    def test_never_waits_for_nothing(self, monkeypatch: Any, browser_opens: List[str]) -> None:
        Foreground(monkeypatch, browser_opens, answers=False)
        monkeypatch.setattr(serving, "open_once_answering", lambda *a: pytest.fail("never starts no waiting thread"))
        lifecycle.serve(DemoWorkspace(), browser="never")
        assert browser_opens == []

    def test_the_command_opens_the_port_it_serves(self, monkeypatch: Any, browser_opens: List[str]) -> None:
        Foreground(monkeypatch, browser_opens)
        run(["workspace", "classification", "--port", "8795"], monkeypatch)
        assert browser_opens == ["http://127.0.0.1:8795"]

    def test_the_bare_command_defaults_to_new(self, monkeypatch: Any, browser_opens: List[str]) -> None:
        Foreground(monkeypatch, browser_opens)
        run([], monkeypatch)
        assert browser_opens == [URL]

    def test_a_machine_without_a_browser_gets_one_line_naming_the_address(
        self, monkeypatch: Any, capsys: Any, browser_opens: List[str]
    ) -> None:
        server = Foreground(monkeypatch, browser_opens)

        def no_browser(url: str, *args: Any, **kwargs: Any) -> bool:
            server.opened.set()
            return False

        monkeypatch.setattr(serving.webbrowser, "open", no_browser)
        lifecycle.serve(DemoWorkspace(), browser="new")
        assert f"demoapp: no browser could be opened — open {URL} yourself" in capsys.readouterr().out


class TestTheSameWorkspaceAlreadyRuns:
    def test_new_prints_the_report_and_opens_nothing(
        self, monkeypatch: Any, capsys: Any, browser_opens: List[str]
    ) -> None:
        _already_serving(monkeypatch, meta())
        monkeypatch.setattr(serving, "open_once_answering", lambda *a: pytest.fail("no new server to wait for"))
        run([], monkeypatch)
        out = capsys.readouterr().out  # pinned line for line: the report's LAYOUT is the point
        assert f"demoapp is already running — serving an empty workspace\n  {URL}\n" in out
        assert browser_opens == []

    def test_always_prints_the_report_and_opens_that_server(
        self, monkeypatch: Any, capsys: Any, browser_opens: List[str]
    ) -> None:
        _already_serving(monkeypatch, meta())
        run(["--browser", "always"], monkeypatch)
        assert "demoapp is already running" in said(capsys)
        assert browser_opens == [URL]

    def test_a_port_held_by_another_program_opens_nothing_even_with_always(
        self, monkeypatch: Any, browser_opens: List[str]
    ) -> None:
        _already_serving(monkeypatch, None)
        monkeypatch.setattr(serving, "port_holder", lambda port: " (pid 5555 postgres)")
        with pytest.raises(ServeError, match="in use \\(pid 5555 postgres\\)"):
            lifecycle.serve(DemoWorkspace(), browser="always")
        assert browser_opens == []


class TestDecliningTheAskForAnotherPort:
    """Another workspace holds the port and the person answers ``n``: its report is printed, nothing is served."""

    @pytest.fixture()
    def declined(self, tmp_path: Path, monkeypatch: Any) -> DemoWorkspace:
        mine = DemoWorkspace()
        mine.save(str(tmp_path / "mine.workspace.yaml"))
        _already_serving(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        monkeypatch.setattr(serving.Server, "serve", lambda server: pytest.fail("declined — nothing is served"))
        return mine

    def test_always_opens_the_server_the_report_names(self, declined: DemoWorkspace, browser_opens: List[str]) -> None:
        lifecycle.serve(declined, browser="always")
        assert browser_opens == [URL]

    def test_new_opens_nothing(self, declined: DemoWorkspace, browser_opens: List[str]) -> None:
        lifecycle.serve(declined, browser="new")
        assert browser_opens == []


class TestABackgroundStart:
    def test_the_terminal_opens_the_tab_once_the_child_answers(
        self, monkeypatch: Any, capsys: Any, browser_opens: List[str]
    ) -> None:
        spawn = Spawn(monkeypatch)
        lifecycle.serve(DemoWorkspace(), background=True, browser="new")
        assert "background pid 4242" in said(capsys)
        assert browser_opens == [URL]
        assert spawn.argv[spawn.argv.index("--browser") + 1] == "never", "the child never opens one of its own"

    def test_never_opens_nothing(self, monkeypatch: Any, browser_opens: List[str]) -> None:
        Spawn(monkeypatch)
        lifecycle.serve(DemoWorkspace(), background=True, browser="never")
        assert browser_opens == []

    def test_a_child_still_coming_up_opens_nothing(self, monkeypatch: Any, browser_opens: List[str]) -> None:
        Spawn(monkeypatch, Child(answers_after=99))
        monkeypatch.setattr(serving, "_BACKGROUND_WAIT_SECONDS", 0.0)
        lifecycle.serve(DemoWorkspace(), background=True, browser="always")
        assert browser_opens == [], "the 'still starting' line names the address; a tab would show an error"

    def test_a_child_that_dies_opens_nothing(self, monkeypatch: Any, browser_opens: List[str]) -> None:
        Spawn(monkeypatch, Child(answers_after=99, exit_code=1, output="boom\n"))
        with pytest.raises(ServeError, match="boom"):
            lifecycle.serve(DemoWorkspace(), background=True, browser="always")
        assert browser_opens == []


class TestABackgroundStartWhileTheSameWorkspaceRuns:
    """No child, the already-running report."""

    @pytest.fixture()
    def running(self, monkeypatch: Any, prepared: List[str]) -> List[str]:
        _already_serving(monkeypatch, meta())
        monkeypatch.setattr(
            serving.subprocess, "Popen", lambda *a, **k: pytest.fail("no child for a running workspace")
        )
        return prepared

    def test_it_prints_the_already_running_report(self, running: List[str], capsys: Any) -> None:
        lifecycle.serve(DemoWorkspace(), background=True)
        out = capsys.readouterr().out  # pinned line for line: the report's LAYOUT is the point
        assert out == (
            "demoapp is already running — serving an empty workspace\n"
            f"  {URL}\n"
            "  'demoapp stop' stops it; 'demoapp restart' reloads it.\n"
        )
        assert running == [], "nothing is served, so nothing is prepared either"

    def test_new_opens_nothing(self, running: List[str], browser_opens: List[str]) -> None:
        lifecycle.serve(DemoWorkspace(), background=True, browser="new")
        assert browser_opens == []

    def test_always_opens_the_running_server(self, running: List[str], browser_opens: List[str]) -> None:
        lifecycle.serve(DemoWorkspace(), background=True, browser="always")
        assert browser_opens == [URL]

    def test_the_command_says_the_same_as_a_foreground_start(self, running: List[str], monkeypatch: Any) -> None:
        run(["-b"], monkeypatch)  # Popen would fail the test


class TestARestart:
    @pytest.fixture()
    def served(self, monkeypatch: Any) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        monkeypatch.setattr(serving, "stop_both", lambda *a, **k: None)
        monkeypatch.setattr(lifecycle, "serve", lambda target, **kwargs: calls.append(kwargs))
        return calls

    def test_a_bare_restart_opens_nothing_by_default(self, served: List[Dict[str, Any]], monkeypatch: Any) -> None:
        run(["restart"], monkeypatch)
        assert [call["browser"] for call in served] == ["never"]

    def test_a_restart_asked_for_a_tab_hands_new_on(self, served: List[Dict[str, Any]], monkeypatch: Any) -> None:
        run(["restart", "--browser", "new"], monkeypatch)
        assert [call["browser"] for call in served] == ["new"]

    def test_a_restart_continuing_a_never_saved_workspace_hands_it_on_too(
        self, served: List[Dict[str, Any]], monkeypatch: Any, tmp_path: Path
    ) -> None:
        folder = tmp_path / "never-saved"
        folder.mkdir()
        continuing = Continuation(folder, "classification", "127.0.0.1", D, None)
        monkeypatch.setattr(lifecycle, "_continuation", lambda target: continuing)
        monkeypatch.setattr(lifecycle, "_refuse_taking_a_saved_port", lambda *a: None)
        monkeypatch.setattr(serving, "refuse_a_port_serving_something_else", lambda *a, **k: None)
        monkeypatch.setattr(sys, "argv", ["demoapp", "restart", "--browser", "always"])
        run(["restart", "--browser", "always"], monkeypatch)
        assert [(call["browser"], call["continuing"]) for call in served] == [("always", folder)]
