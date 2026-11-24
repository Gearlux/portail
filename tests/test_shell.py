"""The page's shell mounted on an app's own Flask app: its routes, its files, and addresses that are all RELATIVE.

An app keeps its own page and API; the shell adds ``/api/meta`` (the identity every discovery reads),
``/api/workspace``,
⏻ / ↻, the event stream, and its files under ``/portail/`` — never the app's ``/static``. A path proxy that serves the
app under a prefix (``/proxy/8787/``) keeps working because the page asks nothing by an absolute path.
"""

import contextlib
import re
import threading
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import pytest
from flask import Flask

from portail import serving
from portail.serving import Server
from portail.web import STATIC, create_shell, mount_shell, server_of
from tests.browsers import chromium_page, serving_app
from tests.demo_cli import DEMO_SPEC, DemoWorkspace


def _server() -> Server:
    return Server(DEMO_SPEC, DemoWorkspace(title="mine"))


class TestTheRoutes:
    def test_meta_is_the_apps_identity(self) -> None:
        server = _server()
        answer = create_shell(server).test_client().get("/api/meta").get_json()
        assert answer["app"] == "demoapp" and answer["title"] == "mine"
        assert answer["workspace"] == server.served.info()

    def test_the_workspace_route_answers_its_info(self) -> None:
        server = _server()
        assert create_shell(server).test_client().get("/api/workspace").get_json() == server.served.info()

    def test_the_shells_files_are_served_under_their_own_prefix(self) -> None:
        client = create_shell(_server()).test_client()
        for name in ("shell.js", "shell.css", "server-events.js", "index.html"):
            response = client.get(f"/portail/{name}")
            assert response.status_code == 200, name
            assert response.headers["Cache-Control"] == "no-cache"
            response.close()
        missing = client.get("/portail/nope.js")
        assert missing.status_code == 404

    def test_a_bare_shell_serves_its_minimal_page(self) -> None:
        response = create_shell(_server()).test_client().get("/")
        assert response.status_code == 200 and b"portail/shell.js" in response.data
        response.close()

    def test_the_server_is_found_on_the_app(self) -> None:
        server = _server()
        assert server_of(create_shell(server)) is server


class TestOnTheAppsOwnApp:
    def test_the_apps_own_page_and_static_stay_its_own(self) -> None:
        app = Flask("host")

        @app.get("/")
        def index() -> str:
            return "the app's own page"

        mount_shell(app, _server())
        client = app.test_client()
        assert client.get("/").get_data(as_text=True) == "the app's own page"
        assert client.get("/api/meta").get_json()["app"] == "demoapp"

    def test_the_minimal_page_can_be_asked_for_or_refused(self) -> None:
        asked = mount_shell(Flask("asked"), _server(), index=True)
        refused = mount_shell(Flask("refused"), _server(), index=False)
        assert asked.test_client().get("/").status_code == 200
        assert refused.test_client().get("/").status_code == 404

    def test_a_server_serves_the_apps_app(self, monkeypatch: Any) -> None:
        made: list = []

        def create(server: Server) -> Flask:
            app = mount_shell(Flask("host"), server)
            made.append(app)
            return app

        server = Server(DEMO_SPEC, DemoWorkspace(), create_app=create)
        assert server.app() is made[0]


class TestEveryAddressIsRelative:
    """A path proxy serves the app under a prefix: an address starting with ``/`` would leave it."""

    @pytest.mark.parametrize("name", ["shell.js", "server-events.js", "index.html", "shell.css"])
    def test_no_shell_file_asks_by_an_absolute_path(self, name: str) -> None:
        text = (STATIC / name).read_text()
        absolute = re.findall(r"""(?:fetch\(|src=|href=|EventSource\(|Worker\(|url\()\s*["'`]/""", text)
        assert absolute == [], f"{name} asks by an absolute path"

    def test_the_page_works_under_a_path_prefix(self) -> None:
        from werkzeug.exceptions import NotFound
        from werkzeug.middleware.dispatcher import DispatcherMiddleware
        from werkzeug.serving import make_server

        server = _server()
        app = create_shell(server)
        proxied = DispatcherMiddleware(NotFound(), {"/proxy/8710": app})
        socket_server = make_server("127.0.0.1", 0, proxied, threaded=True)
        thread = threading.Thread(target=socket_server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{socket_server.server_port}/proxy/8710/"
            with chromium_page(url) as page:
                page.wait_for_function("() => Portail.serverListening")
                assert page.evaluate("() => Portail.meta.app") == "demoapp"
                assert page.inner_text("#title") == "demoapp — mine"
        finally:
            socket_server.shutdown()
            thread.join()


class TestDiscoveryOfARealServer:
    """The live probe against a real socket: the shell's ``/api/meta`` is what makes a server ours."""

    @contextlib.contextmanager
    def _real_http(self, monkeypatch: Any) -> Iterator[None]:
        monkeypatch.setattr(serving, "_urlopen", urllib.request.urlopen)
        yield

    def test_a_served_shell_answers_as_the_app(self, monkeypatch: Any) -> None:
        with self._real_http(monkeypatch), serving_app(DemoWorkspace(title="mine")) as (_app, socket_server, _s):
            answer = serving.app_at(DEMO_SPEC, "127.0.0.1", socket_server.server_port)
            assert answer is not None and answer["title"] == "mine"
            from portail import AppSpec

            other = AppSpec(name="otherapp", cli_module="x")
            assert serving.app_at(other, "127.0.0.1", socket_server.server_port) is None

    def test_a_registered_real_server_is_listed(self, state: Path, monkeypatch: Any) -> None:
        with self._real_http(monkeypatch), serving_app(DemoWorkspace(template="detection")) as (_a, socket_server, s):
            s.served.port = socket_server.server_port
            serving.register_server(DEMO_SPEC, s.served)
            (entry,) = [e for e in serving.running_servers(DEMO_SPEC) if e["port"] == socket_server.server_port]
            assert entry["label"] == "the detection type workspace" and entry["pid"] > 0
            serving.unregister_server(DEMO_SPEC, s.served)
            assert not (state / "servers" / f"{socket_server.server_port}.json").exists()


class TestWhereTheLogsGo:
    def test_the_log_folder_follows_loggair(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setenv("LOGGAIR_DIR", str(tmp_path / "logs"))
        assert serving.log_dir() == (tmp_path / "logs").resolve()
        assert (
            serving.log_path("demoapp.log")
            == f"$LOGGAIR_DIR/demoapp.log: {(tmp_path / 'logs').resolve() / 'demoapp.log'}"
        )

    def test_without_it_the_neutral_folder_holds_them(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("LOGGAIR_DIR", raising=False)
        assert serving.log_dir() == (Path(serving.neutral_cwd()) / "logs").resolve()

    def test_the_names_follow_the_app(self) -> None:
        assert serving.server_log(DEMO_SPEC) == "demoapp.log"
        assert serving.restart_log(DEMO_SPEC) == "demoapp-restart.log"
        assert serving.browser_thread(DEMO_SPEC) == "demoapp-browser"
