"""Every open page is told when its server goes.

Before: ``<name> stop`` ended the server and every tab on it stayed on screen looking alive — the next click failed.

Each page holds ``GET /api/events`` open (a server-sent event stream; every tab of one server shares ONE connection
through a shared worker, because a browser allows six HTTP/1.1 connections per server across ALL its tabs and a
stream per tab would starve the page). On its way out — ``stop``, Ctrl+C, the terminal closed, ⏻ in any tab — the
server sends ``leaving`` down every stream before it goes:

* ``{"why": "exit"}``: each page shows "<name> has exited" (with where a never-saved workspace's work is kept) and
  tries to close its tab — what ⏻ does;
* ``{"why": "restart"}``, set by ``<name> restart`` with ``POST /api/leaving`` before it stops the server: each page
  shows "<name> is restarting…" and reloads once a server with another process id answers — what ↻ does. A restart
  that brings the workspace back on ANOTHER port tells the old pages ``exit``: nothing comes back there.

A server that dies without a word (``kill -9``, a crash) cannot send anything: the page sees its stream drop and, when
it does not come back within ``Portaide.NOT_ANSWERING_MS``, says the server is not answering, with a Retry. A stream
that comes back hides it.
"""

import http.client
import json
import os
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from portaide import AppSpec, Hooks, serving
from portaide.serving import Server
from portaide.web import create_shell
from portaide.web.pages import PageChannel, channel_of, tell_pages_leaving
from tests import test_exit
from tests.browsers import chromium_page, serving_app
from tests.demo_cli import DEMO_SPEC, KEPT, DemoWorkspace, lifecycle
from tests.fleet import meta

#: The real command line in a child process, on a free port with its own HOME.
real_server = test_exit.real_server


def _event(chunk: str) -> Tuple[str, Dict[str, Any]]:
    """One server-sent event as (name, data) — the ``retry:`` line and comments skipped."""
    name, data = "", ""
    for line in chunk.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data = line[len("data: ") :]
    return name, json.loads(data) if data else {}


def _server(workspace: Optional[DemoWorkspace] = None) -> Server:
    return Server(DEMO_SPEC, workspace or DemoWorkspace())


# -- the channel ------------------------------------------------------------------------------------------------------


class TestTheChannel:
    def test_a_page_is_greeted_with_the_servers_process_id(self) -> None:
        stream = PageChannel().stream()
        first = next(stream)
        assert first.startswith("retry: 1000\n"), "a dropped stream is retried within a second"
        assert _event(first) == ("hello", {"pid": os.getpid()})
        stream.close()

    def test_leaving_reaches_every_page_and_ends_its_stream(self) -> None:
        channel = PageChannel()
        streams = [channel.stream() for _ in range(2)]
        for stream in streams:
            next(stream)
        said: List[str] = []

        def read(stream: Iterator[str]) -> None:
            said.extend(stream)  # the leaving event, then the stream's end

        readers = [threading.Thread(target=read, args=(stream,)) for stream in streams]
        for reader in readers:
            reader.start()
        assert channel.announce({"why": "exit", "pid": 1}) == 2
        for reader in readers:
            reader.join(timeout=5)
        assert [_event(chunk) for chunk in said] == [("leaving", {"why": "exit", "pid": 1})] * 2
        assert channel.pages == 0, "an ended stream leaves the channel"

    def test_a_page_that_does_not_read_holds_the_stop_no_longer_than_the_wait(self) -> None:
        channel = PageChannel()
        stream = channel.stream()
        next(stream)  # subscribed, and nobody reads what comes next
        started = time.monotonic()
        assert channel.announce({"why": "exit"}, wait_s=0.2) == 0
        assert time.monotonic() - started < 2.0
        stream.close()

    def test_with_no_page_open_there_is_nobody_to_tell(self) -> None:
        assert PageChannel().announce({"why": "exit"}) == 0

    def test_a_quiet_stream_says_it_is_still_there(self) -> None:
        """A comment line every ``heartbeat_s``: what finds a tab the browser closed (its write fails)."""
        stream = PageChannel(heartbeat_s=0.05).stream()
        next(stream)
        assert next(stream) == ": still here\n\n"
        stream.close()

    def test_a_closed_page_leaves_the_channel(self) -> None:
        channel = PageChannel()
        stream = channel.stream()
        next(stream)
        assert channel.pages == 1
        stream.close()
        assert channel.pages == 0

    def test_ending_silently_ends_every_stream_without_a_word(self) -> None:
        channel = PageChannel()
        stream = channel.stream()
        next(stream)
        channel.end_silently()
        assert list(stream) == []
        assert channel.pages == 0

    def test_why_is_exit_until_a_restart_says_otherwise(self) -> None:
        channel = PageChannel()
        assert channel.why == "exit"
        channel.why = "restart"
        assert channel.why == "restart"


# -- the routes -------------------------------------------------------------------------------------------------------


class TestTheRoutes:
    def test_the_event_stream_is_a_server_sent_event_stream(self) -> None:
        response = create_shell(_server()).test_client().get("/api/events", buffered=False)
        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"
        assert response.headers["Cache-Control"] == "no-cache"
        first = next(iter(response.response))
        assert isinstance(first, bytes)
        assert _event(first.decode())[0] == "hello"
        response.close()

    def test_a_restart_tells_the_server_why_it_is_stopped(self) -> None:
        app = create_shell(_server())
        answer = app.test_client().post("/api/leaving", json={"why": "restart"})
        assert answer.status_code == 200
        assert answer.get_json() == {"why": "restart"}
        assert channel_of(app).why == "restart"

    def test_a_reason_that_is_neither_is_refused(self) -> None:
        app = create_shell(_server())
        answer = app.test_client().post("/api/leaving", json={"why": "nap"})
        assert answer.status_code == 400
        assert answer.get_json() == {"error": "why must be one of 'exit', 'restart' — got 'nap'"}
        assert channel_of(app).why == "exit"


# -- what the server says on its way out ------------------------------------------------------------------------------


def _told(app: Any, server: Server) -> Dict[str, Any]:
    """What one page reads when ``tell_pages_leaving`` runs."""
    stream = channel_of(app).stream()
    next(stream)
    said: List[str] = []
    reader = threading.Thread(target=lambda: said.extend(stream))
    reader.start()
    assert tell_pages_leaving(app, server) == 1
    reader.join(timeout=5)
    name, data = _event(said[0])
    assert name == "leaving"
    return data


class TestWhatTheServerSays:
    def test_a_saved_workspace_says_exit_and_names_no_folder(self, tmp_path: Path) -> None:
        workspace = DemoWorkspace()
        workspace.save(str(tmp_path / "mine.workspace.yaml"))
        server = _server(workspace)
        assert _told(create_shell(server), server) == {
            "why": "exit",
            "pid": os.getpid(),
            "kept_in": "",
            "kept_until": "",
        }

    def test_a_never_saved_workspace_names_the_folder_its_work_is_kept_in(self) -> None:
        workspace = DemoWorkspace(template="classification")
        server = _server(workspace)
        said = _told(create_shell(server), server)
        assert said["kept_in"] == str(workspace.state_dir())
        assert said["kept_until"] == KEPT

    def test_a_restart_says_restart(self) -> None:
        server = _server()
        app = create_shell(server)
        app.test_client().post("/api/leaving", json={"why": "restart"})
        assert _told(app, server)["why"] == "restart"

    def test_an_app_without_pages_tells_nobody(self) -> None:
        assert tell_pages_leaving(object(), _server()) == 0


class TestServeTellsThePagesOnEveryWayOut:
    """Ctrl+C, SIGTERM (``stop``) and SIGHUP all leave ``app.run`` the same way — and the pages are told BEFORE the
    app's ``leaving`` hook, which can take seconds on a big never-saved workspace."""

    def _serve(
        self, monkeypatch: pytest.MonkeyPatch, raises: Optional[BaseException] = None
    ) -> Tuple[Server, List[str]]:
        order: List[str] = []

        class FakeApp:
            def run(self, **kwargs: Any) -> None:
                if raises is not None:
                    raise raises

        monkeypatch.setattr(serving, "port_in_use", lambda host, port: False)

        def told(app: Any, server: Server) -> int:
            order.append("pages told")
            return 0

        monkeypatch.setattr("portaide.web.pages.tell_pages_leaving", told)
        spec = AppSpec(
            name="demoapp",
            cli_module="tests.demo_cli",
            hooks=Hooks(starting=lambda s: order.append("starting"), leaving=lambda s: order.append("leaving")),
        )
        make: Any = lambda s: FakeApp()  # noqa: E731 - a stand-in for the app's Flask app
        return Server(spec, DemoWorkspace(), create_app=make), order

    def test_a_server_that_stops_serving_tells_its_pages_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, order = self._serve(monkeypatch)
        server.serve()
        assert order == ["starting", "pages told", "leaving"]

    def test_ctrl_c_outside_the_serve_loop_tells_them_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, order = self._serve(monkeypatch, raises=KeyboardInterrupt())
        server.serve()
        assert order == ["starting", "pages told", "leaving"]

    def test_a_failure_telling_them_never_stops_the_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server, order = self._serve(monkeypatch)

        def broken(app: Any, server: Server) -> int:
            raise RuntimeError("socket gone")

        monkeypatch.setattr("portaide.web.pages.tell_pages_leaving", broken)
        server.serve()
        assert order == ["starting", "leaving"]

    def test_a_sigterm_or_sighup_is_the_way_ctrl_c_takes(self) -> None:
        assert set(serving._WAYS_OUT) == {signal.SIGTERM, signal.SIGHUP}
        with pytest.raises(KeyboardInterrupt, match=f"signal {int(signal.SIGTERM)}"):
            serving._interrupt(int(signal.SIGTERM), None)

    def test_a_hook_that_fails_never_stops_the_serving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeApp:
            def run(self, **kwargs: Any) -> None:
                ran.append("ran")

        def broken(served: Any) -> None:
            raise RuntimeError("boom")

        ran: List[str] = []
        monkeypatch.setattr(serving, "port_in_use", lambda host, port: False)
        spec = AppSpec(name="demoapp", cli_module="tests.demo_cli", hooks=Hooks(starting=broken, leaving=broken))
        Server(spec, DemoWorkspace(), create_app=lambda s: FakeApp()).serve()  # type: ignore[arg-type,return-value]
        assert ran == ["ran"]


# -- the command line -------------------------------------------------------------------------------------------------


@pytest.fixture()
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, int]]:
    """A saved workspace served on 8787; what the command line tells that server, stops and serves, in order."""
    workspace = tmp_path / "mine.workspace.yaml"
    DemoWorkspace(port=8787).save(str(workspace))
    entry = {
        "host": "127.0.0.1",
        "port": 8787,
        "path": str(workspace),
        "template": "",
        "identity": str(workspace.resolve()),
        "label": str(workspace),
        "state_dir": "",
    }
    happened: List[Tuple[str, int]] = []
    monkeypatch.setattr(serving, "meta_at", lambda host, port: meta(path=str(workspace)))
    monkeypatch.setattr(serving, "running_servers", lambda spec: [entry])

    def stopped(name: str, port: int) -> str:
        happened.append(("stopped", port))
        return ""

    monkeypatch.setattr(serving, "stop_port", stopped)
    monkeypatch.setattr(serving, "tell_server_it_restarts", lambda host, port: happened.append(("told restart", port)))
    monkeypatch.setattr(lifecycle, "serve", lambda target, **kwargs: happened.append(("served", target.port)))
    return happened


class TestTheCommandLineSaysWhy:
    def test_a_restart_tells_the_server_before_it_stops_it(self, tmp_path: Path, machine: List[Any]) -> None:
        lifecycle.restart(str(tmp_path / "mine.workspace.yaml"))
        assert machine == [("told restart", 8787), ("stopped", 8787), ("served", 8787)]

    def test_a_restart_by_port_tells_it_too(self, machine: List[Any]) -> None:
        lifecycle.restart("8787")
        assert machine == [("told restart", 8787), ("stopped", 8787), ("served", 8787)]

    def test_a_restart_onto_another_port_leaves_the_old_pages_told_exit(self, machine: List[Any]) -> None:
        """Nothing comes back on 8787 — its pages must not wait for a server that never answers there."""
        lifecycle.restart("8787", port=8799)
        assert machine == [("stopped", 8787), ("served", 8799)]

    def test_a_stop_tells_nothing_so_the_pages_hear_exit(self, machine: List[Any]) -> None:
        """The server's own default is ``exit`` — a stop sends no word before its signal."""
        lifecycle.stop("8787")
        assert machine == [("stopped", 8787)]


class TestTellingTheServer:
    def test_it_posts_restart_to_the_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: List[Any] = []

        class Answer:
            def __enter__(self) -> "Answer":
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def read(self) -> bytes:
                return b'{"why": "restart"}'

        def urlopen(request: Any, timeout: float) -> Answer:
            sent.append((request.full_url, request.data, request.get_method(), timeout))
            return Answer()

        monkeypatch.setattr(serving, "_urlopen", urlopen)
        serving.tell_server_it_restarts("127.0.0.1", 8787)
        assert sent == [("http://127.0.0.1:8787/api/leaving", b'{"why": "restart"}', "POST", 1.0)]

    def test_a_server_that_does_not_answer_is_stopped_all_the_same(self) -> None:
        """A server too stuck to answer: the restart goes on, and its pages hear ``exit``. The suite's
        ``_no_live_http`` refuses the request as such a server would."""
        serving.tell_server_it_restarts("127.0.0.1", 8787)  # no exception


# -- a real server process --------------------------------------------------------------------------------------------

#: How long a real server may take to say ``leaving`` once it is signalled. A TOTAL deadline, not a per-read timeout:
#: the stream's heartbeat kept a 30 s socket timeout from ever firing, so a Ctrl+C the child ignored held a nightly for
#: hours (measured).
LEAVING_WAIT_S = 60.0


def _read_leaving(port: int, before: Any) -> Dict[str, Any]:
    """Open the event stream of the server on ``port``, run ``before()``, and answer the ``leaving`` it sends — or
    fail, with every line read, once ``LEAVING_WAIT_S`` have passed."""
    deadline = time.monotonic() + LEAVING_WAIT_S
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=LEAVING_WAIT_S)
    lines: List[str] = []
    chunk: List[str] = []
    try:
        connection.request("GET", "/api/events")
        sock = connection.sock  # the answer reads from it after the connection has let go of it (will_close)
        answer = connection.getresponse()
        assert answer.status == 200

        def readline() -> str:
            left = deadline - time.monotonic()
            if left > 0:
                sock.settimeout(left)  # what is left of the deadline — never a fresh timeout per line
                try:
                    line: Optional[str] = answer.fp.readline().decode()
                except TimeoutError:
                    line = None
                if line is not None:
                    lines.append(line.rstrip("\n"))
                    return line
            pytest.fail(f"the server on port {port} said no leaving within {LEAVING_WAIT_S:g} s — read so far: {lines}")

        while not lines or lines[-1] != "":  # the greeting, up to its blank line
            readline()
        before()
        while True:
            line = readline()
            if not line:
                break
            chunk.append(line)
            if line == "\n" and any(entry.startswith("event: leaving") for entry in chunk):
                break
    finally:
        connection.close()
    name, data = _event("".join(chunk))
    assert name == "leaving", chunk
    return data


class TestARealServerTellsItsPages:
    def test_the_stop_command_tells_every_open_page_exit(self, real_server: Any) -> None:
        child, port, registered, temp, errors = real_server
        said = _read_leaving(port, lambda: os.kill(child.pid, signal.SIGTERM))
        assert said["why"] == "exit"
        assert said["pid"] == child.pid
        assert said["kept_in"].startswith(str(temp)), "a never-saved workspace's folder"
        assert child.wait(timeout=60) == 0
        assert "Traceback" not in errors.read_text()

    def test_ctrl_c_tells_them_too(self, real_server: Any) -> None:
        child, port, registered, temp, errors = real_server
        assert _read_leaving(port, lambda: os.kill(child.pid, signal.SIGINT))["why"] == "exit"
        assert child.wait(timeout=60) == 0

    def test_a_restart_tells_them_restart(self, real_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        child, port, registered, temp, errors = real_server
        monkeypatch.setattr(serving, "_urlopen", urllib.request.urlopen)

        def restart() -> None:
            serving.tell_server_it_restarts("127.0.0.1", port)
            os.kill(child.pid, signal.SIGTERM)

        said = _read_leaving(port, restart)
        assert (said["why"], said["pid"]) == ("restart", child.pid)
        assert said["kept_in"].startswith(str(temp))
        assert child.wait(timeout=60) == 0


class TestReadingLeaving:
    def test_a_server_that_never_leaves_fails_the_read_at_the_deadline_with_what_was_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The heartbeats are read and do NOT push the deadline back — before, each one reset the socket's timeout and
        the read waited as long as the server lived (7 min 51 s measured, then killed)."""
        monkeypatch.setattr(sys.modules[__name__], "LEAVING_WAIT_S", 0.5)
        with serving_app(DemoWorkspace()) as (app, socket_server, _server_):
            channel_of(app).heartbeat_s = 0.05
            started = time.monotonic()
            with pytest.raises(pytest.fail.Exception, match="said no leaving within 0.5 s") as failed:
                _read_leaving(socket_server.server_port, lambda: None)
            assert time.monotonic() - started < 2.0
            assert ": still here" in str(failed.value), "what was read while waiting is in the report"
            assert "event: hello" in str(failed.value)


# -- the pages --------------------------------------------------------------------------------------------------------


def _url(socket_server: Any) -> str:
    return f"http://127.0.0.1:{socket_server.server_port}/"


def _listening(page: Any) -> None:
    page.wait_for_function("() => window.Portaide && Portaide.serverListening")


def _wait_for_pages(app: Any, count: int) -> None:
    deadline = time.monotonic() + 30
    while channel_of(app).pages < count and time.monotonic() < deadline:
        time.sleep(0.05)
    assert channel_of(app).pages == count


class TestThePages:
    def test_every_tab_of_the_server_shares_one_stream(self) -> None:
        with serving_app(DemoWorkspace()) as (app, socket_server, _s), chromium_page(_url(socket_server)) as page:
            with page.context.expect_page() as opened:
                page.evaluate("() => window.open(location.href)")
            second = opened.value
            _listening(page)
            _listening(second)
            assert channel_of(app).pages == 1, "one connection for every tab — not one of six per tab"

    def test_a_shared_worker_kept_off_the_network_falls_back_to_one_of_the_tabs_own(self) -> None:
        """An embedded browser pane may let no SharedWorker onto the network: every request fails. A tab whose shared
        worker never reaches the server while the tab itself does listens through a dedicated worker instead."""
        with serving_app(DemoWorkspace()) as (app, socket_server, server), chromium_page(_url(socket_server)) as page:
            _listening(page)
            _wait_for_pages(app, 1)  # the first tab, through a real shared worker
            page.context.add_init_script(
                """
                window.SharedWorker = class {
                  constructor() {
                    const port = { start() {}, postMessage(m) { (window.toTheSharedWorker ||= []).push(m); } };
                    this.port = port;
                    setTimeout(() => port.onmessage && port.onmessage({ data: { type: "lost" } }), 50);
                  }
                };
                """
            )
            with page.context.expect_page() as opened:
                page.evaluate("() => window.open(location.href)")
            tab = opened.value
            _listening(tab)
            _wait_for_pages(app, 2)  # the second tab, through a worker of its own
            assert tab.evaluate("() => window.toTheSharedWorker") == ["bye"], "the shared worker is let go"
            with tab.expect_event("close"):
                tell_pages_leaving(app, server)

    def test_exit_closes_a_tab_the_browser_lets_close_and_says_so_in_one_it_does_not(self) -> None:
        workspace = DemoWorkspace()
        with serving_app(workspace) as (app, socket_server, server), chromium_page(_url(socket_server)) as page:
            with page.context.expect_page() as opened:
                page.evaluate("() => window.open(location.href)")
            tab = opened.value
            _listening(page)
            _listening(tab)
            _wait_for_pages(app, 1)
            with tab.expect_event("close"):
                tell_pages_leaving(app, server)
            page.wait_for_selector("#exited")
            text = page.inner_text("#exited")
            assert "demoapp has exited." in text
            assert "demoapp restart continues this workspace" in text
            assert str(workspace.state_dir()) in text
            assert page.locator("#top").count() == 0

    def test_restart_waits_for_the_new_server_and_reloads(self) -> None:
        with serving_app(DemoWorkspace()) as (app, socket_server, _s), chromium_page(_url(socket_server)) as page:
            _listening(page)
            _wait_for_pages(app, 1)
            page.evaluate("() => { window.beforeReload = true; }")
            with page.expect_navigation():
                # Another process id than the one serving: the page reloads as soon as api/meta answers.
                channel_of(app).announce({"why": "restart", "pid": 1, "kept_in": "", "kept_until": ""})
            _listening(page)
            assert page.evaluate("() => window.beforeReload") is None, "the page was loaded afresh"

    def test_restart_says_it_is_restarting_while_no_new_server_answers(self) -> None:
        with serving_app(DemoWorkspace()) as (app, socket_server, _s), chromium_page(_url(socket_server)) as page:
            _listening(page)
            _wait_for_pages(app, 1)
            # The same process id: the server that answers is still the old one, so the page keeps waiting.
            channel_of(app).announce({"why": "restart", "pid": os.getpid(), "kept_in": "", "kept_until": ""})
            page.wait_for_selector("#restarting")
            assert "demoapp is restarting…" in page.inner_text("#restarting")

    def test_a_server_that_vanished_is_not_answering_and_one_that_returns_hides_it(self) -> None:
        workspace = DemoWorkspace()
        with serving_app(workspace) as (app, socket_server, _s), chromium_page(_url(socket_server)) as page:
            port = socket_server.server_port
            page.evaluate("() => { Portaide.NOT_ANSWERING_MS = 300; }")
            _listening(page)
            _wait_for_pages(app, 1)
            socket_server.shutdown()
            socket_server.server_close()
            channel_of(app).end_silently()  # what a kill -9 looks like from the page: the stream drops
            page.wait_for_selector("#not-answering:not(.hidden)")
            assert "demoapp is not answering" in page.inner_text("#not-answering")
            page.click("#not-answering button")
            page.wait_for_function(
                "() => /still not answering/.test(document.getElementById('not-answering').textContent)"
            )
            with serving_app(workspace, port=port):
                page.wait_for_selector("#not-answering.hidden", state="attached")
            assert page.locator("#top").is_visible(), "a server that came back keeps the page"

    def test_a_page_that_exited_by_its_own_button_ignores_the_word(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """⏻ already replaced the page — the leaving that follows must not replace it again."""
        monkeypatch.setattr(Server, "_ends_on_sigterm", True)
        monkeypatch.setattr(Server, "stop_serving", lambda self: None)
        with serving_app(DemoWorkspace()) as (app, socket_server, _s), chromium_page(_url(socket_server)) as page:
            _listening(page)
            _wait_for_pages(app, 1)
            page.click("#ws-exit")
            page.wait_for_selector("#exited")
            page.evaluate("() => { document.getElementById('exited').dataset.mark = 'first'; }")
            channel_of(app).announce({"why": "restart", "pid": 1, "kept_in": "", "kept_until": ""})
            page.wait_for_timeout(500)
            assert page.locator("#exited[data-mark=first]").count() == 1
            assert page.locator("#restarting").count() == 0
