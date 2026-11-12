"""A busy port — the same workspace reports, ANOTHER one asks, a NEW type workspace moves on its own.

The already-running report answered "is the app reachable here?" but never "is it serving THE WORKSPACE I asked for?"
— opening a second workspace silently handed back the first one's URL. The CLI compares identities before serving. A
SAVED workspace keeps its port: the SAME workspace keeps the report, a DIFFERENT one asks to launch on the next free
port ([Y/n], default yes), and ``--auto-port`` decides yes without asking; a port held by a program that is not the
app is left to serve()'s located error unless ``--auto-port`` goes around it. A bare ``<name>`` keeps the plain report
against a running server of the app — nothing was asked for.

``<name> workspace <type>`` is a NEW workspace each time: it finds its own port when the default one is taken,
whoever holds it, and asks nothing — unless ``--auto-port false``, or ``--port N`` names the port.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from portaide import serving
from portaide.errors import ServeError
from tests.cli_output import said
from tests.conftest import run
from tests.demo_cli import DEMO_PORT, DemoWorkspace, lifecycle
from tests.fleet import meta, serves

D = DEMO_PORT


def _saved(tmp_path: Path, name: str = "one") -> str:
    workspace = DemoWorkspace()
    workspace.save(str(tmp_path / f"{name}.workspace.yaml"))
    return workspace.path


def _busy(monkeypatch: Any, answer: Optional[Dict[str, Any]], busy_ports: Any = (D,)) -> None:
    monkeypatch.setattr(serving, "port_in_use", lambda host, port: port in busy_ports)
    monkeypatch.setattr(serving, "meta_at", lambda host, port: answer)


def _run(argv: List[str], monkeypatch: Any) -> List[Any]:
    served = serves(monkeypatch)
    run(argv, monkeypatch)
    return served


class TestSameWorkspace:
    def test_the_same_workspace_keeps_the_plain_report_path(self, tmp_path: Path, monkeypatch: Any) -> None:
        path = _saved(tmp_path)
        _busy(monkeypatch, meta(path=path))
        monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("the same workspace must never ask"))
        (served,) = _run([path], monkeypatch)
        assert served.port == D, "serve() itself prints the already-running report — unchanged"

    def test_a_bare_empty_workspace_keeps_the_plain_report_path(self, tmp_path: Path, monkeypatch: Any) -> None:
        _busy(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")))
        monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("nothing was asked for — no ask"))
        (served,) = _run([], monkeypatch)
        assert served.port == D


class TestAnotherWorkspace:
    def test_asks_and_yes_launches_on_the_next_free_port(self, tmp_path: Path, monkeypatch: Any) -> None:
        path = _saved(tmp_path)
        _busy(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        prompts: List[str] = []

        def _answer(prompt: str = "") -> str:
            prompts.append(prompt)
            return ""  # Enter = yes

        monkeypatch.setattr("builtins.input", _answer)
        (served,) = _run([path], monkeypatch)
        assert served.port == D + 1, "the next free port"
        assert prompts and str(D + 1) in prompts[0] and "other.workspace" in prompts[0]
        assert prompts[0].startswith("demoapp already serves")

    def test_answering_no_prints_the_report_and_serves_nothing(
        self, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        path = _saved(tmp_path)
        _busy(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        assert _run([path], monkeypatch) == []
        out = capsys.readouterr().out
        assert "other.workspace" in out and "http://" in out, "declined — the running server's report"

    def test_auto_port_decides_without_asking(self, tmp_path: Path, monkeypatch: Any) -> None:
        path = _saved(tmp_path)
        _busy(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")))
        monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("--auto-port must not ask"))
        (served,) = _run([path, "--auto-port"], monkeypatch)
        assert served.port == D + 1

    def test_the_scan_skips_further_busy_ports(self, tmp_path: Path, monkeypatch: Any) -> None:
        path = _saved(tmp_path)
        _busy(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")), busy_ports=(D, D + 1, D + 2))
        (served,) = _run([path, "--auto-port"], monkeypatch)
        assert served.port == D + 3

    def test_non_interactive_without_the_flag_refuses_naming_it(self, tmp_path: Path, monkeypatch: Any) -> None:
        path = _saved(tmp_path)
        _busy(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(ServeError, match="already serves .*other.workspace.yaml — rerun with --auto-port"):
            lifecycle.serve(lifecycle.open_file(path))

    def test_no_free_port_in_reach_is_said(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(serving, "port_in_use", lambda host, port: True)
        with pytest.raises(ServeError, match=rf"no free port in {D + 1}..{D + 100} on 127.0.0.1"):
            serving.next_free_port("127.0.0.1", D)


_HOLDER = " (pid 2279 Code Helper (Plugin))"


def _foreign(monkeypatch: Any, busy_ports: Any = (D,)) -> None:
    """A program that is not the app listens on the port — it answers no meta of the app."""
    _busy(monkeypatch, None, busy_ports)
    monkeypatch.setattr(serving, "port_holder", lambda port: _HOLDER)


def _no_ask(monkeypatch: Any) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("this start must never ask"))


class TestAForeignHolder:
    """A port held by a program that is NOT the app is never asked about. A saved workspace keeps its port and serve()
    gives the located error; only ``--auto-port`` goes around the holder."""

    def test_a_saved_workspace_asks_nothing_and_keeps_its_port(self, tmp_path: Path, monkeypatch: Any) -> None:
        path = _saved(tmp_path)
        _foreign(monkeypatch)
        _no_ask(monkeypatch)
        (served,) = _run([path], monkeypatch)
        assert served.port == D, "serve() raises the located error for it"

    def test_a_bare_empty_workspace_asks_nothing_either(self, monkeypatch: Any) -> None:
        _foreign(monkeypatch)
        _no_ask(monkeypatch)
        (served,) = _run([], monkeypatch)
        assert served.port == D

    def test_auto_port_goes_around_it_without_asking(self, tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
        path = _saved(tmp_path)
        _foreign(monkeypatch, busy_ports=(D, D + 1))
        monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("--auto-port must not ask"))
        (served,) = _run([path, "--auto-port"], monkeypatch)
        assert served.port == D + 2, "the scan skips every busy port"
        assert f"launching on port {D + 2} (port {D} stays with pid 2279 Code Helper (Plugin))" in said(capsys)

    def test_an_unknown_holder_is_named_as_another_program(self, tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
        """``lsof`` may name nobody (missing, or another user's process) — the line still reads."""
        path = _saved(tmp_path)
        _foreign(monkeypatch)
        monkeypatch.setattr(serving, "port_holder", lambda port: "")
        _run([path, "--auto-port"], monkeypatch)
        assert f"launching on port {D + 1} (port {D} stays with another program)" in said(capsys)


class TestANewTypeWorkspaceFindsItsOwnPort:
    """``<name> workspace <type>`` is a NEW workspace each time: the next free port, whoever holds this one."""

    def test_a_program_that_is_not_the_app_holds_the_port(self, monkeypatch: Any, capsys: Any) -> None:
        _foreign(monkeypatch)
        _no_ask(monkeypatch)
        (served,) = _run(["workspace", "classification"], monkeypatch)
        assert served.port == D + 1
        assert f"launching on port {D + 1} (port {D} stays with pid 2279 Code Helper (Plugin))" in said(capsys)

    def test_another_workspace_holds_the_port(self, tmp_path: Path, monkeypatch: Any) -> None:
        _busy(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")))
        _no_ask(monkeypatch)
        (served,) = _run(["workspace", "classification"], monkeypatch)
        assert served.port == D + 1

    def test_the_same_type_is_a_new_workspace_with_a_port_of_its_own(self, monkeypatch: Any) -> None:
        _busy(monkeypatch, meta(template="classification"))
        _no_ask(monkeypatch)
        (served,) = _run(["workspace", "classification"], monkeypatch)
        assert served.port == D + 1

    def test_the_scan_skips_further_busy_ports(self, monkeypatch: Any) -> None:
        _busy(monkeypatch, meta(template="classification"), busy_ports=(D, D + 1, D + 2))
        (served,) = _run(["workspace", "classification"], monkeypatch)
        assert served.port == D + 3

    def test_a_free_port_is_taken_as_it_is(self, monkeypatch: Any) -> None:
        _busy(monkeypatch, None, busy_ports=())
        (served,) = _run(["workspace", "classification"], monkeypatch)
        assert served.port == D

    def test_auto_port_false_gives_it_up_and_the_same_type_keeps_the_report(self, monkeypatch: Any) -> None:
        _busy(monkeypatch, meta(template="classification"))
        _no_ask(monkeypatch)
        (served,) = _run(["workspace", "classification", "--auto-port", "false"], monkeypatch)
        assert served.port == D, "serve() prints the already-running report"

    def test_auto_port_false_asks_against_another_workspace(self, tmp_path: Path, monkeypatch: Any) -> None:
        _busy(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        prompts: List[str] = []

        def _answer(prompt: str = "") -> str:
            prompts.append(prompt)
            return ""

        monkeypatch.setattr("builtins.input", _answer)
        (served,) = _run(["workspace", "classification", "--auto-port=false"], monkeypatch)
        assert served.port == D + 1
        assert len(prompts) == 1

    def test_auto_port_false_leaves_a_program_that_is_not_the_app_to_the_error(self, monkeypatch: Any) -> None:
        _foreign(monkeypatch)
        _no_ask(monkeypatch)
        (served,) = _run(["workspace", "classification", "--auto-port", "false"], monkeypatch)
        assert served.port == D

    def test_a_port_the_person_names_is_not_moved(self, monkeypatch: Any) -> None:
        """``--port 8193`` is what another command of the app talks to — a silent move would send its work
        elsewhere."""
        _busy(monkeypatch, meta(template="classification"), busy_ports=(8193,))
        _no_ask(monkeypatch)
        (served,) = _run(["workspace", "classification", "--port", "8193"], monkeypatch)
        assert served.port == 8193

    def test_a_named_port_held_by_another_workspace_asks_as_before(self, tmp_path: Path, monkeypatch: Any) -> None:
        _busy(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")), busy_ports=(8193,))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        (served,) = _run(["workspace", "classification", "--port", "8193"], monkeypatch)
        assert served.port == 8194

    def test_auto_port_typed_beside_a_named_port_moves_it(self, monkeypatch: Any) -> None:
        _busy(monkeypatch, meta(template="classification"), busy_ports=(8193,))
        _no_ask(monkeypatch)
        (served,) = _run(["workspace", "classification", "--port", "8193", "--auto-port"], monkeypatch)
        assert served.port == 8194


class TestASavedWorkspaceIsNeverMovedByItself:
    def test_it_keeps_the_report_when_the_same_one_runs(self, tmp_path: Path, monkeypatch: Any) -> None:
        path = _saved(tmp_path)
        _busy(monkeypatch, meta(path=path))
        _no_ask(monkeypatch)
        (served,) = _run([path, "--auto-port"], monkeypatch)
        assert served.port == D, "even with --auto-port the SAME saved workspace is not started twice"

    def test_it_keeps_its_port_where_a_program_that_is_not_the_app_listens(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        path = _saved(tmp_path)
        _foreign(monkeypatch)
        _no_ask(monkeypatch)
        (served,) = _run([path], monkeypatch)
        assert served.port == D


class TestAnExplicitPort:
    """``--port N`` on the serving commands. The commands BUILD their workspace, so the flag used to fall through to
    the app's own configuration and reach nothing: the conflict check still probed the default and refused naming
    it."""

    def test_open_serves_on_the_requested_port(self, tmp_path: Path, monkeypatch: Any) -> None:
        _busy(monkeypatch, None, busy_ports=())
        (served,) = _run([_saved(tmp_path), "--port", "8890"], monkeypatch)
        assert served.port == 8890

    def test_a_type_workspace_serves_on_the_requested_port(self, monkeypatch: Any) -> None:
        _busy(monkeypatch, None, busy_ports=())
        (served,) = _run(["workspace", "classification", "--port", "8890"], monkeypatch)
        assert served.port == 8890

    def test_the_conflict_check_probes_the_requested_port_not_the_default(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """The default port holds ANOTHER workspace; asking for 8890 must neither ask nor refuse."""
        path = _saved(tmp_path)
        _busy(monkeypatch, meta(path=str(tmp_path / "other.workspace.yaml")), busy_ports=(D,))
        monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("no question to ask: 8890 is free"))
        (served,) = _run([path, "--port", "8890"], monkeypatch)
        assert served.port == 8890

    def test_a_non_numeric_port_is_a_located_error(self) -> None:
        with pytest.raises(ServeError, match="port number, got 'eighty'"):
            serving.apply_port(DemoWorkspace(), "eighty")
        with pytest.raises(ServeError, match="between 1 and 65535"):
            serving.apply_port(DemoWorkspace(), "70000")

    def test_no_flag_keeps_the_workspaces_own_port(self) -> None:
        workspace = DemoWorkspace(port=8123)
        serving.apply_port(workspace, 0)
        serving.apply_port(workspace, "")
        serving.apply_port(workspace, None)
        assert workspace.port == 8123

    def test_restart_takes_a_port(self, monkeypatch: Any) -> None:
        served: List[int] = []
        target = DemoWorkspace()
        monkeypatch.setattr(lifecycle, "_resolve_target", lambda what: target)
        monkeypatch.setattr(serving, "stop_both", lambda *a, **kwargs: None)
        monkeypatch.setattr(lifecycle, "serve", lambda resolved, **kwargs: served.append(resolved.port))
        lifecycle.restart("", "8891")
        assert served == [8891]

    def test_a_restart_with_port_stops_where_it_will_serve(self, monkeypatch: Any) -> None:
        """A file or type restarted onto another port: what serves THAT port is what the start would collide with."""
        stopped: List[int] = []
        monkeypatch.setattr(lifecycle, "_resolve_target", lambda what: DemoWorkspace())
        monkeypatch.setattr(serving, "stop_both", lambda spec, address, **kwargs: stopped.append(address.port))
        monkeypatch.setattr(lifecycle, "serve", lambda resolved, **kwargs: None)
        lifecycle.restart("detection", 8891)
        assert stopped == [8891]
