"""The page's ⏻: stop this server and close the tab.

The page asks ``POST /api/exit``; the server answers FIRST and, once that answer has been sent, sends ITSELF SIGTERM —
the signal ``<name> stop`` sends — so the one way out ``Server.serve`` already takes runs unchanged: the pages told,
the app's ``leaving`` hook run, the registry entry removed. A server that does not own that signal (served on another
thread, inside another program — the browser harness's own ``served``) refuses, and nothing is signalled.

The page then replaces itself with "<name> has exited" and calls ``window.close()``. Measured in Chromium: a tab opened
straight at the URL (``history.length`` 1) closes; a tab that visited another page first is refused with "Scripts may
close only the windows that were opened by them." — and keeps the exited text.

Unsaved changes: a SAVED workspace with changes (``info()["dirty"]``) loses them on any stop, so the button asks first;
a never-saved workspace is kept in its folder by the stop when the app continues one, so it asks nothing.
"""

import http.client
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from portaide import serving
from portaide.errors import ServeError
from portaide.serving import Server
from portaide.web import create_shell
from tests.browsers import answer_confirm, chromium_page, ready, served
from tests.demo_cli import DEMO_SPEC, KEPT, DemoWorkspace

REFUSAL = "this demoapp runs inside another program — it stops with that program"
PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def signals(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Every ``os.kill`` the serving module sends, recorded instead of sent — the suite's own process is never
    signalled."""
    sent: List[Any] = []
    monkeypatch.setattr(serving.os, "kill", lambda pid, signum: sent.append((pid, signum)))
    return sent


def _server(workspace: Any = None) -> Server:
    return Server(DEMO_SPEC, workspace if workspace is not None else DemoWorkspace())


class TestTheExitRoute:
    def test_the_answer_goes_out_first_and_the_stop_follows_once_it_is_sent(
        self, monkeypatch: pytest.MonkeyPatch, signals: List[Any]
    ) -> None:
        server = _server()
        monkeypatch.setattr(server, "_ends_on_sigterm", True)
        response = create_shell(server).test_client().post("/api/exit")
        assert response.status_code == 200, response.get_json()
        assert signals == [], "the stop must wait until the answer has been sent"
        response.close()
        assert signals == [(os.getpid(), signal.SIGTERM)]

    def test_a_server_that_does_not_own_its_stop_refuses_and_signals_nothing(self, signals: List[Any]) -> None:
        response = create_shell(_server()).test_client().post("/api/exit")
        response.close()
        assert response.status_code == 400
        assert response.get_json() == {"error": REFUSAL}
        assert signals == []


class TestWhatThePageSaysOnceItHasExited:
    """Where a never-saved workspace's work is kept — the page names the folder; a saved one keeps nothing there."""

    @pytest.fixture(autouse=True)
    def _owns_its_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Server, "_ends_on_sigterm", True)

    def test_a_saved_workspace_names_no_folder(self, tmp_path: Path) -> None:
        workspace = DemoWorkspace()
        workspace.save(str(tmp_path / "mine.workspace.yaml"))
        assert _server(workspace).exiting() == {"kept_in": "", "kept_until": ""}

    def test_a_never_saved_workspace_names_the_folder_its_work_is_kept_in(self) -> None:
        workspace = DemoWorkspace(template="classification")
        assert _server(workspace).exiting() == {"kept_in": str(workspace.state_dir()), "kept_until": KEPT}


class TestTheStopItself:
    def test_it_sends_this_process_the_signal_the_stop_command_sends(
        self, monkeypatch: pytest.MonkeyPatch, signals: List[Any]
    ) -> None:
        server = _server()
        monkeypatch.setattr(server, "_ends_on_sigterm", True)
        server.stop_serving()
        assert signals == [(os.getpid(), signal.SIGTERM)]

    def test_without_the_signal_handler_it_refuses_rather_than_end_the_program_it_runs_in(
        self, signals: List[Any]
    ) -> None:
        with pytest.raises(ServeError, match=REFUSAL):
            _server().stop_serving()
        with pytest.raises(ServeError, match=REFUSAL):
            _server().exiting()
        assert signals == []


class TestServeOwnsTheStopOnlyWhileItHoldsTheSignal:
    """``serve`` sets SIGTERM's handler on the main thread only — and only then may the page end the process."""

    @staticmethod
    def _server(monkeypatch: pytest.MonkeyPatch, seen: List[bool]) -> Server:
        holder: List[Server] = []

        class FakeApp:
            def run(self, **kwargs: Any) -> None:
                seen.append(holder[0]._ends_on_sigterm)

        monkeypatch.setattr(serving, "port_in_use", lambda host, port: False)
        make: Any = lambda s: FakeApp()  # noqa: E731 - a stand-in for the app's Flask app
        server = Server(DEMO_SPEC, DemoWorkspace(), create_app=make)
        holder.append(server)
        return server

    def test_served_on_the_main_thread_it_owns_the_stop_until_serving_ends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: List[bool] = []
        server = self._server(monkeypatch, seen)
        before = signal.getsignal(signal.SIGTERM)
        server.serve()
        assert seen == [True]
        assert server._ends_on_sigterm is False
        assert signal.getsignal(signal.SIGTERM) == before, "the handler it set is put back"

    def test_served_on_another_thread_it_never_owns_the_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: List[bool] = []
        server = self._server(monkeypatch, seen)
        thread = threading.Thread(target=server.serve)
        thread.start()
        thread.join(timeout=30)
        assert seen == [False]


# -- a real server process --------------------------------------------------------------------------------------------

#: The real command line in a child process. Ctrl+C put back where the fixture started the child with it ignored (the
#: comment inside says why), and the child says LISTENING once its socket listens, so the test waits on a line rather
#: than a sleep.
CHILD = r"""
import signal
import sys

import werkzeug.serving as serving

# Started as a background job of a non-interactive sh (a CI `sh` step), this process inherits SIGINT IGNORED and
# Python installs no Ctrl+C handler: `os.kill(pid, SIGINT)` would do nothing. A terminal's own handler, so the Ctrl+C
# test means the same thing wherever the suite runs.
signal.signal(signal.SIGINT, signal.default_int_handler)
listening = serving.BaseWSGIServer.serve_forever


def serve_forever(self, *args, **kwargs):
    print("LISTENING", flush=True)
    return listening(self, *args, **kwargs)


serving.BaseWSGIServer.serve_forever = serve_forever
from tests import demo_cli

# --browser never: this real start would otherwise open a tab in the operator's browser.
sys.argv = ["demoapp", "--port", sys.argv[1], "--browser", "never"]
demo_cli.main()
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def start_with_ctrl_c_ignored(argv: List[str], **kwargs: Any) -> "subprocess.Popen[str]":
    """``Popen`` with SIGINT IGNORED, inherited through exec — what a CI ``sh`` step (a background job of a
    non-interactive sh) hands pytest and every process it starts. Measured: a child started there survives
    ``os.kill(pid, SIGINT)``, and the Ctrl+C test then waited as long as the server lived and held a nightly for hours.
    Started this way everywhere, a bootstrap that stops putting the handler back fails at the developer's desk."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return subprocess.Popen(argv, **kwargs)
    finally:
        signal.signal(signal.SIGINT, previous)


def child_environment(home: Path, temp: Path, logs: Path) -> dict:
    """The child's own HOME (the registry), temp folder (its workspace folder) and logs — and the demo app importable
    from wherever it runs."""
    path = os.pathsep.join(filter(None, [str(PROJECT), os.environ.get("PYTHONPATH", "")]))
    return {**os.environ, "HOME": str(home), "TMPDIR": str(temp), "LOGGAIR_DIR": str(logs), "PYTHONPATH": path}


@pytest.fixture()
def real_server(tmp_path: Path) -> Iterator[Any]:
    """``demoapp --port <free>`` in a child with its own HOME, temp and log folders — never on a port of the
    operator's, and never a stop or restart of anything this test did not start."""
    home, temp, logs = tmp_path / "child-home", tmp_path / "child-tmp", tmp_path / "child-logs"
    for folder in (home, temp, logs):
        folder.mkdir()
    port = free_port()
    errors = tmp_path / "stderr.txt"  # a file, not a pipe: nothing reads the child's log while it serves
    with errors.open("w") as log:
        child = start_with_ctrl_c_ignored(
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
        yield child, port, home / ".demoapp" / "servers" / f"{port}.json", temp, errors
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


class TestARealServerExits:
    def test_the_exit_request_ends_the_process_the_way_the_stop_command_does(self, real_server: Any) -> None:
        child, port, registered, temp, errors = real_server
        assert registered.is_file(), "the running server is in the registry `list` reads"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        connection.request("POST", "/api/exit", body="{}", headers={"Content-Type": "application/json"})
        answer = connection.getresponse()
        assert answer.status == 200
        assert json.loads(answer.read())["kept_in"].startswith(str(temp)), "a never-saved workspace's folder"
        connection.close()
        assert child.wait(timeout=60) == 0
        logged = errors.read_text()
        assert "Traceback" not in logged, logged
        assert not registered.exists(), "the stopped server must leave the registry"


class TestTheChildIsStartedAsCIStartsIt:
    def test_a_child_started_through_the_helper_has_ctrl_c_ignored_and_this_process_keeps_its_own(self) -> None:
        probe = "import signal; print(signal.getsignal(signal.SIGINT) == signal.SIG_IGN)"
        before = signal.getsignal(signal.SIGINT)
        child = start_with_ctrl_c_ignored([sys.executable, "-c", probe], stdout=subprocess.PIPE, text=True)
        said, _ = child.communicate(timeout=60)
        assert said.strip() == "True"
        assert signal.getsignal(signal.SIGINT) == before, "the suite's own Ctrl+C is back once the child is started"


# -- the page ---------------------------------------------------------------------------------------------------------


@pytest.fixture()
def stopped(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """The browser tests' server owns its stop, and the stop is recorded instead of signalling pytest."""
    happened = threading.Event()
    monkeypatch.setattr(Server, "_ends_on_sigterm", True)
    monkeypatch.setattr(Server, "stop_serving", lambda self: happened.set())
    return happened


class TestTheExitButton:
    def test_a_tab_the_browser_lets_close_closes_and_the_server_stops(self, stopped: threading.Event) -> None:
        with served(DemoWorkspace()) as url, chromium_page(url) as page:
            # A tab the browser lets a script close: one a script opened (as one opened straight at the URL is).
            with page.context.expect_page() as opened:
                page.evaluate("() => window.open(location.href)")
            tab = opened.value
            ready(tab)
            with tab.expect_event("close"):
                tab.click("#ws-exit")
            assert stopped.wait(timeout=10)

    def test_a_tab_the_browser_will_not_close_says_the_app_has_exited(self, stopped: threading.Event) -> None:
        with served(DemoWorkspace()) as url, chromium_page(url) as page:
            page.click("#ws-exit")
            page.wait_for_selector("#exited")
            text = page.inner_text("#exited")
            assert "demoapp has exited." in text
            assert "You can close this tab — demoapp restart continues this workspace" in text
            assert KEPT in text
            assert page.locator("#top").count() == 0, "nothing of the stopped workspace stays on screen"
            assert stopped.wait(timeout=10)

    def test_a_saved_workspace_with_unsaved_changes_is_asked_about_first(
        self, tmp_path: Path, stopped: threading.Event
    ) -> None:
        workspace = DemoWorkspace()
        workspace.save(str(tmp_path / "mine.workspace.yaml"))
        workspace.dirty = True
        with served(workspace) as url, chromium_page(url) as page:
            exits: List[str] = []
            page.on("request", lambda request: exits.append(request.url) if "/api/exit" in request.url else None)
            answer_confirm(page, False)
            page.click("#ws-exit")
            page.wait_for_function("() => window.asked.length === 1")
            assert page.evaluate("() => window.asked") == [
                "mine.workspace.yaml has unsaved changes — exit and lose them?"
            ]
            assert page.evaluate("async () => (await fetch('api/meta')).ok"), "Cancel keeps the server"
            assert exits == [] and not stopped.is_set()
            assert page.locator("#exited").count() == 0

            answer_confirm(page, True)
            page.click("#ws-exit")
            page.wait_for_selector("#exited")
            assert "You can close this tab — demoapp restart starts it again." in page.inner_text("#exited")
            assert stopped.wait(timeout=10)

    def test_a_never_saved_workspace_is_kept_so_nothing_is_asked(self, stopped: threading.Event) -> None:
        workspace = DemoWorkspace()
        workspace.dirty = True
        with served(workspace) as url, chromium_page(url) as page:
            answer_confirm(page, False)
            page.click("#ws-exit")
            page.wait_for_selector("#exited")
            assert page.evaluate("() => window.asked") == []
            assert str(workspace.state_dir()) in page.inner_text("#exited")
            assert stopped.wait(timeout=10)

    def test_the_host_pages_own_question_can_keep_the_server(self, stopped: threading.Event) -> None:
        with served(DemoWorkspace()) as url, chromium_page(url) as page:
            page.evaluate(
                "() => { Portaide.beforeLeaving = async (what) => { window.askedFor = what; return false; }; }"
            )
            page.click("#ws-exit")
            page.wait_for_function("() => window.askedFor === 'exit'")
            assert page.locator("#exited").count() == 0 and not stopped.is_set()

    def test_a_server_inside_another_program_says_so_and_the_page_stays(self) -> None:
        with served(DemoWorkspace()) as url, chromium_page(url) as page:
            page.click("#ws-exit")
            page.wait_for_selector(".portaide-toast.error")
            assert REFUSAL in page.inner_text(".portaide-toast.error")
            assert page.locator("#exited").count() == 0
            assert page.locator("#top").is_visible()
