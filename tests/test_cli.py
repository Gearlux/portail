"""The lifecycle commands on an app's own CLI: ``open`` the DEFAULT command, ``workspace``, ``stop``, ``restart``,
``list``.

``demoapp`` serves an empty workspace; ``demoapp <workspace.yaml>`` opens a file — the path is the default command's
positional (bound by liquifai without a command name). A missing file fails loudly, in one line.
"""

import socket
from pathlib import Path
from typing import Any, Dict, List

import pytest
from liquifai import LiquifyApp

from portaide import AppSpec, serving
from portaide.cli import add_lifecycle_commands
from portaide.errors import PortInUseError, ServeError
from tests.cli_output import flat, said, said_all
from tests.conftest import run
from tests.demo_cli import DEMO_PORT, DemoWorkspace, app, lifecycle
from tests.fleet import meta, noting, serves

D = DEMO_PORT


def test_the_commands_are_registered_and_open_is_the_default() -> None:
    assert app._default_cmd is app._commands["open"]
    assert getattr(app._default_cmd, "__liquifai_positionals__", []) == ["workspace"]
    assert getattr(app._commands["workspace"], "__liquifai_positionals__", []) == ["type"]
    assert set(app._commands) == {"open", "workspace", "stop", "restart", "list"}


def test_the_help_names_the_app() -> None:
    assert "``demoapp stop`` stops it" in str(app._commands["open"].__doc__)
    assert "Stop running demoapp server(s)." in str(app._commands["stop"].__doc__)


def test_an_app_without_types_has_no_workspace_command() -> None:
    bare = LiquifyApp(name="bareapp", strict_flags=True)
    life = add_lifecycle_commands(
        bare, AppSpec(name="bareapp", cli_module="x"), open_file=lambda p: DemoWorkspace(), open_empty=DemoWorkspace
    )
    assert set(bare._commands) == {"open", "stop", "restart", "list"}
    assert life.type_names() == []
    with pytest.raises(FileNotFoundError, match="'classification' is not a type workspace$"):
        life.open_type("classification")


def test_bare_invocation_serves_an_empty_workspace(monkeypatch: Any) -> None:
    served = serves(monkeypatch)
    run([], monkeypatch)
    (workspace,) = served
    assert isinstance(workspace, DemoWorkspace)
    assert workspace.path == "" and workspace.template == ""


def test_a_workspace_file_is_opened_by_its_absolute_path(tmp_path: Path, monkeypatch: Any) -> None:
    target = tmp_path / "w.yaml"
    target.write_text("workspace:\n  port: 9999\n  title: mine\n")
    monkeypatch.chdir(tmp_path)
    served = serves(monkeypatch)
    run(["w.yaml"], monkeypatch)
    (opened,) = served
    assert opened.port == 9999 and opened.title == "mine"
    assert opened.path == str(target.resolve())


def test_a_missing_workspace_file_on_the_command_line_is_one_clean_error_line(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    serves(monkeypatch)
    with pytest.raises(SystemExit) as exit_info:
        run([str(tmp_path / "nope.yaml")], monkeypatch)
    assert exit_info.value.code == 1
    out = said(capsys)
    assert "Error:" in out and "nope.yaml" in out and "does not exist" in out
    assert "Traceback" not in out


def test_a_type_name_given_as_a_file_points_at_the_workspace_command() -> None:
    with pytest.raises(FileNotFoundError, match="demoapp workspace classification"):
        lifecycle.open_file("classification")


def test_the_apps_own_refusal_of_a_file_is_one_clean_error_line(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("something: 1\n")
    serves(monkeypatch)
    with pytest.raises(SystemExit) as exit_info:
        run([str(bad)], monkeypatch)
    assert exit_info.value.code == 1
    out = said(capsys)
    assert "Error:" in out and "bad.yaml" in out and "workspace" in out


def test_under_debug_the_traceback_propagates(tmp_path: Path, monkeypatch: Any) -> None:
    # `--debug` AFTER the positional: a leading flag would stop positional binding (liquifai rule).
    serves(monkeypatch)
    with pytest.raises(FileNotFoundError, match="nope.yaml"):
        run([str(tmp_path / "nope.yaml"), "--debug"], monkeypatch)


def test_workspace_type_opens_the_type(monkeypatch: Any) -> None:
    served = serves(monkeypatch)
    run(["workspace", "classification"], monkeypatch)
    (opened,) = served
    assert opened.template == "classification" and opened.path == ""


def test_an_unknown_type_names_the_known_ones(monkeypatch: Any, capsys: Any) -> None:
    serves(monkeypatch)
    with pytest.raises(SystemExit):
        run(["workspace", "nope"], monkeypatch)
    assert "'nope' is not a type workspace (known: classification, detection)" in said_all(capsys)


def test_a_type_is_required(monkeypatch: Any) -> None:
    with pytest.raises(FileNotFoundError, match=r"a type is required \(known: classification, detection\)"):
        lifecycle.workspace("")


def test_a_busy_port_is_one_clean_error_line(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    target = tmp_path / f"busy-{port}.yaml"
    target.write_text(f"workspace:\n  port: {port}\n")
    try:
        with pytest.raises(SystemExit) as exit_info:
            run([str(target)], monkeypatch)
        assert exit_info.value.code == 1
        out = said(capsys)
        assert "Error:" in out and f"port {port}" in out and "in use" in out and "Traceback" not in out
    finally:
        holder.close()


def test_stop_stops_the_server_and_nothing_else(monkeypatch: Any, capsys: Any) -> None:
    """A stop ends the one process listening on the server's port. A listener on another port is somebody else's —
    never probed, never stopped."""
    killed: List[Any] = []
    ports = {D: [111], 8188: [222]}
    probed: List[int] = []

    def answer(host: str, port: int) -> Any:
        probed.append(port)
        return meta() if port == D else None

    monkeypatch.setattr(serving, "meta_at", answer)
    monkeypatch.setattr(serving, "pids_listening", lambda port: ports.get(port, []))
    monkeypatch.setattr(serving, "_alive", lambda pid: False)  # they die on the first signal
    monkeypatch.setattr(serving.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    run(["stop"], monkeypatch)
    assert f"demoapp (an empty workspace): stopped pid 111 (port {D})" in said(capsys)
    assert 8188 not in probed
    assert [pid for pid, _ in killed] == [111]


def test_stop_reads_the_port_from_a_workspace_file(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    target = tmp_path / "w.yaml"
    target.write_text("workspace: {port: 9999, other_url: 'http://127.0.0.1:8188'}\n")
    asked: List[int] = []
    monkeypatch.setattr(serving, "pids_listening", noting(asked, []))
    run(["stop", str(target)], monkeypatch)
    assert set(asked) == {9999}, "only the workspace's own port"
    assert "nothing listening on port 9999" in said(capsys)


def test_restart_stops_and_serves_again(monkeypatch: Any) -> None:
    monkeypatch.setattr(serving, "pids_listening", lambda port: [])
    served = serves(monkeypatch)
    run(["restart", "classification"], monkeypatch)
    (workspace,) = served
    assert workspace.template == "classification"


def test_restart_refuses_an_unknown_target_with_the_known_names(capsys: Any, monkeypatch: Any) -> None:
    with pytest.raises(SystemExit):
        run(["restart", "nope"], monkeypatch)
    assert "'nope' is neither a workspace file nor a type workspace (known: classification, detection)" in said_all(
        capsys
    )


def test_serving_remembers_the_workspace_and_bare_restart_reuses_it(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    target = tmp_path / "mine.yaml"
    target.write_text("workspace: {title: mine}\n")
    monkeypatch.setattr(serving, "pids_listening", lambda port: [])
    served = serves(monkeypatch)
    run([str(target)], monkeypatch)  # serving records the file …
    run(["restart"], monkeypatch)  # … and a bare restart serves it again
    assert [workspace.path for workspace in served] == [str(target.resolve())] * 2
    assert f"restarting {target.resolve()}" in capsys.readouterr().out


def test_bare_restart_reuses_the_last_type_workspace(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(serving, "pids_listening", lambda port: [])
    served = serves(monkeypatch)
    run(["workspace", "detection"], monkeypatch)
    run(["restart"], monkeypatch)
    assert served[-1].template == "detection"
    assert "restarting the detection type workspace" in said(capsys)


def test_bare_restart_without_history_serves_an_empty_workspace(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(serving, "pids_listening", lambda port: [])
    served = serves(monkeypatch)
    run(["restart"], monkeypatch)
    (workspace,) = served
    assert workspace.path == "" and workspace.template == ""
    assert "restarting an empty workspace (nothing was served before)" in said(capsys)


def test_a_remembered_file_that_vanished_is_one_clean_error(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    target = tmp_path / "gone.yaml"
    target.write_text("workspace: {}\n")
    serves(monkeypatch)
    run([str(target)], monkeypatch)
    target.unlink()
    monkeypatch.setattr(serving, "pids_listening", lambda port: [])
    with pytest.raises(SystemExit):
        run(["restart"], monkeypatch)
    assert "does not exist" in said_all(capsys)


def test_bare_stop_uses_the_last_workspaces_port(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    target = tmp_path / "odd.yaml"
    target.write_text("workspace: {port: 9998}\n")
    serves(monkeypatch)
    run([str(target)], monkeypatch)
    asked: List[int] = []
    monkeypatch.setattr(serving, "pids_listening", noting(asked, []))
    run(["stop"], monkeypatch)
    assert set(asked) == {9998}
    assert "demoapp: nothing listening on port 9998" in said(capsys)


class TestAlreadyRunning:
    """Starting the app when it is already serving is not a failure: what the user asked for — the app reachable at
    that address — is already true, so the answer is the URL. A port held by anything ELSE is still an error: only
    the probe can tell the two apart."""

    @staticmethod
    def _server(monkeypatch: Any, holder: Any) -> serving.Server:
        monkeypatch.setattr(serving, "port_in_use", lambda host, port: True)
        monkeypatch.setattr(serving, "meta_at", lambda host, port: holder)
        monkeypatch.setattr(serving, "port_holder", lambda port: " (pid 5 postgres)")
        return serving.Server(lifecycle.spec, DemoWorkspace())

    def test_a_server_already_serving_answers_with_its_url(self, monkeypatch: Any, capsys: Any) -> None:
        self._server(monkeypatch, meta(path="/tmp/my.workspace.yaml")).serve()
        out = capsys.readouterr().out  # raw: the next line pins where the URL sits
        assert "already running" in flat(out) and "/tmp/my.workspace.yaml" in out
        assert f"  http://127.0.0.1:{D}\n" in out, "the URL sits alone on its line so terminals linkify it"
        assert "'demoapp stop'" in out, "the report names the command that actually exists"

    def test_a_template_workspace_is_named_by_its_type(self, monkeypatch: Any, capsys: Any) -> None:
        self._server(monkeypatch, meta(template="classification")).serve()
        assert "the classification type workspace" in said(capsys)

    def test_an_empty_workspace_is_named_as_one(self, monkeypatch: Any, capsys: Any) -> None:
        self._server(monkeypatch, meta()).serve()
        assert "an empty workspace" in said(capsys)

    def test_a_port_held_by_something_else_is_still_an_error(self, monkeypatch: Any) -> None:
        with pytest.raises(PortInUseError, match=rf"port {D} on 127.0.0.1 is in use \(pid 5 postgres\)"):
            self._server(monkeypatch, None).serve()

    def test_a_server_of_another_app_is_something_else(self, monkeypatch: Any) -> None:
        with pytest.raises(PortInUseError, match="is in use"):
            self._server(monkeypatch, meta(app="otherapp")).serve()

    def test_the_cli_reports_it_without_failing(self, monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
        target = tmp_path / "w.yaml"
        target.write_text("workspace: {title: mine}\n")
        monkeypatch.setattr(serving, "port_in_use", lambda host, port: True)
        monkeypatch.setattr(serving, "meta_at", lambda host, port: meta(path=str(target.resolve())))
        run([str(target)], monkeypatch)  # no SystemExit: an already-running server is a report, not a failure
        assert f"http://127.0.0.1:{D}" in capsys.readouterr().out


def test_list_prints_every_running_server_lowest_port_first(monkeypatch: Any, capsys: Any) -> None:
    servers: List[Dict[str, Any]] = [
        {"host": "127.0.0.1", "port": 8787, "label": "/home/me/first.workspace.yaml"},
        {"host": "127.0.0.1", "port": 8788, "label": "the detection type workspace"},
    ]
    monkeypatch.setattr(serving, "running_servers", lambda spec: servers)
    run(["list"], monkeypatch)
    assert capsys.readouterr().out == (
        "8787  /home/me/first.workspace.yaml  http://127.0.0.1:8787\n"
        "8788  the detection type workspace  http://127.0.0.1:8788\n"
    )


def test_list_ends_each_line_with_the_url_built_from_the_servers_own_host(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(
        serving, "running_servers", lambda spec: [{"host": "10.0.0.5", "port": 9001, "label": "the x type workspace"}]
    )
    run(["list"], monkeypatch)
    assert capsys.readouterr().out == "9001  the x type workspace  http://10.0.0.5:9001\n"


def test_list_with_nothing_running_says_how_to_start_one(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(serving, "running_servers", lambda spec: [])
    run(["list"], monkeypatch)
    out = said(capsys)
    assert out == (
        "no demoapp is running — start one with `demoapp workspace classification` or `demoapp open <workspace.yaml>`"
    )


class TestAnUnknownOptionIsRefused:
    """An option a command does not declare is an ERROR, never silently dropped.

    Measured with an app on the same seam: ``stop --port 8799`` (an option ``stop`` did not have) stopped the ONLY
    running server — exactly what a bare stop did — because the unknown option fell through to the config overrides and
    nothing read it. The app's ``strict_flags`` refuses it, naming the closest declared option.
    """

    @staticmethod
    def _one_server(monkeypatch: Any) -> List[Any]:
        stopped: List[Any] = []
        server = {"host": "127.0.0.1", "port": 8788, "path": "/a.yaml", "template": "", "identity": "/a.yaml"}
        monkeypatch.setattr(serving, "running_servers", lambda spec: [dict(server, label="/a.yaml")])
        monkeypatch.setattr(serving, "stop_servers", lambda spec, chosen: stopped.append(chosen))
        return stopped

    def test_a_misspelled_option_stops_nothing_and_names_the_right_one(self, monkeypatch: Any, capsys: Any) -> None:
        stopped = self._one_server(monkeypatch)
        with pytest.raises(SystemExit):
            run(["stop", "--al"], monkeypatch)
        assert stopped == [], "the running server is left alone"
        out = said_all(capsys)
        assert "unknown option 'al'" in out and "Did you mean 'all'?" in out

    def test_an_option_another_command_has_is_refused_too(self, monkeypatch: Any, capsys: Any) -> None:
        stopped = self._one_server(monkeypatch)
        with pytest.raises(SystemExit):
            run(["list", "--port", "8799"], monkeypatch)
        assert "unknown option 'port'" in said_all(capsys)
        assert stopped == []

    def test_a_declared_option_still_works(self, monkeypatch: Any) -> None:
        stopped = self._one_server(monkeypatch)
        run(["stop", "--all"], monkeypatch)
        assert [server["port"] for server in stopped[0]] == [8788]


class TestTalkingToARunningServer:
    """``post_json`` / ``get_json``: an app's own commands ask a running server, and every failure is one line."""

    def test_an_answer_is_its_json(self, monkeypatch: Any) -> None:
        import io

        sent: List[Any] = []

        def urlopen(request: Any, timeout: float) -> Any:
            sent.append((getattr(request, "full_url", request), timeout))
            return io.BytesIO(b'{"ok": true}')

        monkeypatch.setattr(serving, "_urlopen", urlopen)
        assert serving.post_json(lifecycle.spec, "127.0.0.1", 9001, "/api/x", {"a": 1}) == {"ok": True}
        assert serving.get_json(lifecycle.spec, "127.0.0.1", 9001, "/api/y") == {"ok": True}
        assert sent == [("http://127.0.0.1:9001/api/x", 600), ("http://127.0.0.1:9001/api/y", 60)]

    def test_a_refusal_names_the_servers_message(self, monkeypatch: Any) -> None:
        import io
        import urllib.error

        def refuse(request: Any, timeout: float) -> Any:
            body = io.BytesIO(b'{"error": "no such record"}')
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, body)  # type: ignore[arg-type]

        monkeypatch.setattr(serving, "_urlopen", refuse)
        with pytest.raises(ServeError, match="^demoapp on port 9001 refused: no such record$"):
            serving.post_json(lifecycle.spec, "127.0.0.1", 9001, "/api/x", {})

    def test_a_refusal_without_json_names_the_status(self, monkeypatch: Any) -> None:
        import io
        import urllib.error

        def refuse(request: Any, timeout: float) -> Any:
            raise urllib.error.HTTPError("u", 500, "Server Error", {}, io.BytesIO(b"<html>"))  # type: ignore[arg-type]

        monkeypatch.setattr(serving, "_urlopen", refuse)
        with pytest.raises(ServeError, match="refused: Server Error$"):
            serving.post_json(lifecycle.spec, "127.0.0.1", 9001, "/api/x", {})

    def test_a_server_that_stopped_answering_is_said(self) -> None:
        with pytest.raises(ServeError, match="^demoapp on port 9001 stopped answering"):
            serving.get_json(lifecycle.spec, "127.0.0.1", 9001, "/api/y")
        with pytest.raises(ServeError, match="^demoapp on port 9001 stopped answering"):
            serving.post_json(lifecycle.spec, "127.0.0.1", 9001, "/api/y", {})


class TestTheProbe:
    def test_a_real_answer_is_read(self, monkeypatch: Any) -> None:
        import io

        monkeypatch.setattr(serving, "_urlopen", lambda url, timeout: io.BytesIO(b'{"app": "demoapp", "pid": 1}'))
        assert serving.app_at(lifecycle.spec, "127.0.0.1", 1) == {"app": "demoapp", "pid": 1}

    def test_an_answer_that_is_no_object_is_none(self, monkeypatch: Any) -> None:
        import io

        monkeypatch.setattr(serving, "_urlopen", lambda url, timeout: io.BytesIO(b"[1, 2]"))
        assert serving.meta_at("127.0.0.1", 1) is None

    def test_a_port_bound_here_is_in_use(self) -> None:
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            assert serving.port_in_use("127.0.0.1", holder.getsockname()[1]) is True
