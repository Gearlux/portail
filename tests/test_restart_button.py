"""The page's ↻: restart this server with the code on disk now, and reload the tab.

The page asks ``POST /api/restart``; the server answers FIRST — its own process id and the restart's log — and, once
that answer has been sent, launches ``<name> restart <its port> --background`` in a process of its own session. That
command is the one a person types: it stops this server the way ``<name> stop`` does (a never-saved workspace kept and
continued in its folder) and serves the same workspace again, detached, on the same port — a fresh Python process, so
every code change is picked up. A server that does not own its stop refuses, and nothing is launched.

The page replaces itself with "<name> is restarting…", asks ``api/meta`` until a server with ANOTHER process id
answers there, and reloads. One that never comes back is said, with the restart's log.
"""

import http.client
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest

from portail import serving
from portail.errors import ServeError
from portail.serving import Server
from portail.web import create_shell
from tests.browsers import answer_confirm, chromium_page, ready, served
from tests.demo_cli import DEMO_SPEC, DemoWorkspace
from tests.test_exit import CHILD, child_environment, free_port

REFUSAL = "this demoapp runs inside another program — it stops with that program"


@pytest.fixture()
def launches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> List[Dict[str, Any]]:
    """Every process the serving module would launch, recorded instead of started — and the log under ``tmp_path``."""
    monkeypatch.setenv("LOGGAIR_DIR", str(tmp_path / "logs"))
    started: List[Dict[str, Any]] = []

    def popen(argv: List[str], **kwargs: Any) -> None:
        started.append({"argv": argv, **kwargs})

    monkeypatch.setattr(serving.subprocess, "Popen", popen)
    return started


def _server(port: int = 8710) -> Server:
    return Server(DEMO_SPEC, DemoWorkspace(port=port))


class TestTheRestartRoute:
    def test_the_answer_goes_out_first_and_the_restart_is_launched_once_it_is_sent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launches: List[Dict[str, Any]]
    ) -> None:
        server = _server()
        monkeypatch.setattr(server, "_ends_on_sigterm", True)
        response = create_shell(server).test_client().post("/api/restart")
        assert response.status_code == 200, response.get_json()
        assert response.get_json() == {
            "pid": os.getpid(),
            "log": str((tmp_path / "logs" / "demoapp-restart.log").resolve()),
        }
        assert launches == [], "the restart must wait until the answer has been sent"
        response.close()
        assert len(launches) == 1

    def test_a_server_that_does_not_own_its_stop_refuses_and_launches_nothing(
        self, launches: List[Dict[str, Any]]
    ) -> None:
        response = create_shell(_server()).test_client().post("/api/restart")
        response.close()
        assert response.status_code == 400
        assert response.get_json() == {"error": REFUSAL}
        assert launches == []

    def test_the_page_can_tell_one_server_process_from_the_next(self) -> None:
        meta = create_shell(_server()).test_client().get("/api/meta").get_json()
        assert meta["pid"] == os.getpid() and meta["app"] == "demoapp"


class TestTheRestartItself:
    def test_it_launches_the_restart_command_on_its_own_port_in_a_session_of_its_own(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launches: List[Dict[str, Any]]
    ) -> None:
        server = _server(port=8799)
        monkeypatch.setattr(server, "_ends_on_sigterm", True)
        server.restart_serving()
        [launched] = launches
        assert launched["argv"] == [sys.executable, "-m", "tests.demo_cli", "restart", "8799", "--background"]
        # Its own session: the stop it sends this server must not reach it, nor Ctrl+C in this server's terminal.
        assert launched["start_new_session"] is True
        assert launched["cwd"] == serving.neutral_cwd()
        assert launched["stdin"] == subprocess.DEVNULL
        assert launched["stderr"] == subprocess.STDOUT
        log = tmp_path / "logs" / "demoapp-restart.log"
        assert launched["stdout"].name == str(log.resolve()), "what the restart prints lands in the log the page names"
        assert log.is_file()

    def test_without_the_signal_handler_it_refuses_rather_than_stop_the_program_it_runs_in(
        self, launches: List[Dict[str, Any]]
    ) -> None:
        with pytest.raises(ServeError, match=REFUSAL):
            _server().restart_serving()
        with pytest.raises(ServeError, match=REFUSAL):
            _server().restarting()
        assert launches == []


# -- a real server process --------------------------------------------------------------------------------------------


def _meta(port: int) -> Optional[Dict[str, Any]]:
    """What the server on ``port`` says about itself — None while nothing answers there."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/api/meta")
        answer = connection.getresponse()
        return dict(json.loads(answer.read())) if answer.status == 200 else None
    except OSError:
        return None
    finally:
        connection.close()


def _until(predicate: Callable[[], bool], seconds: float) -> bool:
    """Ask ``predicate`` until it holds or ``seconds`` pass — a DETACHED process has no handle to wait on."""
    tick = threading.Event()
    for _ in range(int(seconds / 0.2)):
        if predicate():
            return True
        tick.wait(0.2)
    return predicate()


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.fixture()
def real_server(tmp_path: Path) -> Iterator[Any]:
    """``demoapp --port <free>`` in a child with its own HOME (the registry the restart reads), temp and log folders.
    Whatever answers as the demo app on the port at the end — the restarted server, detached — is stopped."""
    home, temp, logs = tmp_path / "child-home", tmp_path / "child-tmp", tmp_path / "child-logs"
    for folder in (home, temp, logs):
        folder.mkdir()
    port = free_port()
    errors = tmp_path / "stderr.txt"
    with errors.open("w") as log:
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(port)],
            env=child_environment(home, temp, logs),
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
    lines: "queue.Queue[str]" = queue.Queue()

    def read() -> None:
        for line in child.stdout or []:
            lines.put(line)

    threading.Thread(target=read, daemon=True).start()
    try:
        try:
            while lines.get(timeout=60).strip() != "LISTENING":
                pass
        except queue.Empty:
            pytest.fail(f"the server never listened: {errors.read_text()}")
        yield child, port, logs, errors
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        meta = _meta(port)
        if meta is not None and meta.get("app") == "demoapp":  # the restarted server, detached: no handle but its pid
            os.kill(int(meta["pid"]), signal.SIGTERM)
            _until(lambda: _gone(int(meta["pid"])), 30)


class TestARealServerRestarts:
    def test_a_fresh_process_serves_the_same_workspace_on_the_same_port(self, real_server: Any) -> None:
        child, port, logs, errors = real_server
        before = _meta(port)
        assert before is not None and before["pid"] == child.pid
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        connection.request("POST", "/api/restart", body="{}", headers={"Content-Type": "application/json"})
        answer = connection.getresponse()
        assert answer.status == 200
        said = json.loads(answer.read())
        connection.close()
        assert said == {"pid": child.pid, "log": str((logs / "demoapp-restart.log").resolve())}

        assert child.wait(timeout=60) == 0, "the old server stops the way the stop command stops it"
        assert "Traceback" not in errors.read_text(), errors.read_text()
        assert _until(lambda: (_meta(port) or {}).get("pid") not in (None, child.pid), 60), Path(
            said["log"]
        ).read_text()
        after = _meta(port)
        assert after is not None
        assert after["workspace"]["state_dir"] == before["workspace"]["state_dir"], "the never-saved one is continued"
        # The restart prints its report once the fresh server answered — a moment after this test may see it answer.
        assert _until(lambda: "demoapp: serving" in Path(said["log"]).read_text(), 30), Path(said["log"]).read_text()


# -- the page ---------------------------------------------------------------------------------------------------------


def _another_server_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """From now on ``/api/meta`` answers with another process id — as the fresh server on the same port would."""
    monkeypatch.setattr(os, "getpid", lambda: 4242)


@pytest.fixture()
def restarted(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """The browser tests' server owns its stop; the restart is recorded and another server answers at once."""
    happened = threading.Event()
    monkeypatch.setattr(Server, "_ends_on_sigterm", True)

    def restart_serving(self: Server) -> None:
        _another_server_answers(monkeypatch)
        happened.set()

    monkeypatch.setattr(Server, "restart_serving", restart_serving)
    return happened


class TestTheRestartButton:
    def test_it_sits_beside_the_exit_button(self) -> None:
        with served(DemoWorkspace()) as url, chromium_page(url) as page:
            assert page.evaluate("() => document.getElementById('ws-restart').nextElementSibling.id") == "ws-exit"

    def test_the_page_says_it_is_restarting_and_reloads_once_another_server_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Server, "_ends_on_sigterm", True)
        monkeypatch.setattr(Server, "restart_serving", lambda self: None)  # the old server answers until told
        with served(DemoWorkspace()) as url, chromium_page(url) as page:
            page.evaluate("() => { window.beforeRestart = true; }")
            page.click("#ws-restart")
            page.wait_for_selector("#restarting")
            assert page.inner_text("#restarting h1") == "demoapp is restarting…"
            assert page.inner_text("#restarting p") == "This tab reloads once the new server answers."
            assert page.locator("#top").count() == 0, "nothing of the stopped workspace stays on screen"
            with page.expect_navigation():
                _another_server_answers(monkeypatch)
            ready(page)
            assert page.evaluate("() => window.beforeRestart") is None, "the tab was reloaded"
            assert page.evaluate("() => Portail.meta.pid") == 4242

    def test_a_server_that_never_comes_back_is_said_with_the_restarts_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("LOGGAIR_DIR", str(tmp_path / "logs"))
        monkeypatch.setattr(Server, "_ends_on_sigterm", True)
        monkeypatch.setattr(Server, "restart_serving", lambda self: None)  # the same process keeps answering
        with served(DemoWorkspace()) as url, chromium_page(url) as page:
            page.evaluate("() => { Portail.RESTART_WAIT_MS = 1000; }")
            page.click("#ws-restart")
            page.wait_for_selector("#restarting.failed")
            text = page.inner_text("#restarting")
            assert "demoapp did not come back in 1 s" in text
            assert str((tmp_path / "logs" / "demoapp-restart.log").resolve()) in text

    def test_a_saved_workspace_with_unsaved_changes_is_asked_about_first(
        self, tmp_path: Path, restarted: threading.Event
    ) -> None:
        workspace = DemoWorkspace()
        workspace.save(str(tmp_path / "mine.workspace.yaml"))
        workspace.dirty = True
        with served(workspace) as url, chromium_page(url) as page:
            asks: List[str] = []
            page.on("request", lambda request: asks.append(request.url) if "/api/restart" in request.url else None)
            answer_confirm(page, False)
            page.click("#ws-restart")
            page.wait_for_function("() => window.asked.length === 1")
            assert page.evaluate("() => window.asked") == [
                "mine.workspace.yaml has unsaved changes — restart and lose them?"
            ]
            assert asks == [] and not restarted.is_set()
            assert page.locator("#restarting").count() == 0

            answer_confirm(page, True)
            with page.expect_navigation():
                page.click("#ws-restart")
            assert restarted.is_set()

    def test_a_never_saved_workspace_is_continued_so_nothing_is_asked(self, restarted: threading.Event) -> None:
        workspace = DemoWorkspace()
        workspace.dirty = True
        with served(workspace) as url, chromium_page(url) as page:
            answer_confirm(page, False)
            with page.expect_navigation():
                page.click("#ws-restart")
            assert restarted.is_set()

    def test_a_server_inside_another_program_says_so_and_the_page_stays(self) -> None:
        with served(DemoWorkspace()) as url, chromium_page(url) as page:
            page.click("#ws-restart")
            page.wait_for_selector(".portail-toast.error")
            assert REFUSAL in page.inner_text(".portail-toast.error")
            assert page.locator("#restarting").count() == 0
            assert page.locator("#top").is_visible()
