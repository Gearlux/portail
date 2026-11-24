"""``<name> stop`` — discover every running server, ask which to stop.

With several workspaces serving side by side, a bare stop cannot mean one port: it DISCOVERS the running servers (a
per-port registry written around serving, verified live by the meta probe — stale entries pruned; the default port is
probed even unregistered) and, with several up, lists them and asks — all, one, or none. ``--all`` decides without
asking; non-interactive runs with several servers must pass it or name a workspace. A NAMED workspace stops the server
actually SERVING it, wherever it runs — which is what makes an ``--auto-port`` server stoppable at all (its file still
says the old port). Nothing else is stopped: a stop signals the processes listening on the stopped server's port and
nothing more — a process that binds no port (an MCP server of a client) is never signalled.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional

import pytest

from portail import serving
from portail.errors import ServeError
from tests.cli_output import said
from tests.conftest import run
from tests.demo_cli import DEMO_PORT, DemoWorkspace, lifecycle
from tests.fleet import Fleet, Machine, noting, only_these_were_stopped, register

D = DEMO_PORT


def _saved(tmp_path: Path, port: int = D, name: str = "w.workspace.yaml") -> Path:
    target = tmp_path / name
    DemoWorkspace(port=port).save(str(target))
    return target


class TestTheRegistry:
    def test_serve_registers_the_live_port_and_unregisters_after(self, state: Path, monkeypatch: Any) -> None:
        seen: Dict[str, bool] = {}
        monkeypatch.setattr(
            serving.Server, "serve", lambda self: seen.update(registered=(state / "servers" / f"{D}.json").is_file())
        )
        lifecycle.serve(DemoWorkspace())
        assert seen["registered"] is True, "the entry exists WHILE serving"
        assert not (state / "servers" / f"{D}.json").exists(), "and is gone after"

    def test_the_entry_names_the_workspace_and_this_process(self, state: Path, monkeypatch: Any) -> None:
        import json

        entries: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            serving.Server,
            "serve",
            lambda self: entries.append(json.loads((state / "servers" / f"{D}.json").read_text())),
        )
        lifecycle.serve(DemoWorkspace(template="detection"))
        assert entries == [{"host": "127.0.0.1", "port": D, "path": "", "template": "detection", "pid": os.getpid()}]

    def test_a_busy_port_never_clobbers_the_running_servers_entry(self, state: Path, monkeypatch: Any) -> None:
        """The same workspace already serving: serve() prints its report — the REAL server's registry entry must
        survive this process's exit."""
        register(state, D)
        monkeypatch.setattr(serving, "port_in_use", lambda host, port: True)
        monkeypatch.setattr(serving, "meta_at", lambda host, port: {"app": "demoapp", "workspace": {"path": ""}})
        monkeypatch.setattr(serving.Server, "serve", lambda self: None)
        lifecycle.serve(DemoWorkspace())
        assert (state / "servers" / f"{D}.json").is_file()

    def test_an_unwritable_state_folder_never_stops_serving(self, state: Path, monkeypatch: Any) -> None:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("a file where the folder should be")
        served: List[Any] = []
        monkeypatch.setattr(serving.Server, "serve", lambda self: served.append(self.served))
        lifecycle.open()
        assert len(served) == 1


class TestDiscovery:
    def test_live_servers_list_with_their_live_identity(self, state: Path, monkeypatch: Any) -> None:
        register(state, 9001, path="/stale/claim.yaml")
        Fleet(monkeypatch, {9001: {"path": "/live/truth.workspace.yaml"}})
        by_port = {server["port"]: server for server in serving.running_servers(lifecycle.spec)}
        assert by_port[9001]["path"] == "/live/truth.workspace.yaml", "the LIVE meta wins over the registry copy"

    def test_a_dead_registry_entry_is_pruned(self, state: Path, monkeypatch: Any) -> None:
        register(state, 9002)
        Fleet(monkeypatch, {})
        assert serving.running_servers(lifecycle.spec) == []
        assert not (state / "servers" / "9002.json").exists()

    def test_an_unreadable_registry_entry_is_pruned(self, state: Path, monkeypatch: Any) -> None:
        (state / "servers").mkdir(parents=True)
        (state / "servers" / "9003.json").write_text("{not json")
        Fleet(monkeypatch, {})
        assert serving.running_servers(lifecycle.spec) == []
        assert not (state / "servers" / "9003.json").exists()

    def test_the_default_port_is_probed_even_unregistered(self, state: Path, monkeypatch: Any) -> None:
        Fleet(monkeypatch, {D: {"path": "/w.yaml"}})
        assert [server["port"] for server in serving.running_servers(lifecycle.spec)] == [D]

    def test_a_server_of_another_app_is_not_ours(self, state: Path, monkeypatch: Any) -> None:
        """The identity is the app's NAME in the live answer — another app on the same seam is never listed."""
        register(state, 9004)
        monkeypatch.setattr(serving, "meta_at", lambda host, port: {"app": "otherapp", "workspace": {"path": "/x"}})
        assert serving.running_servers(lifecycle.spec) == []

    def test_an_entry_carries_everything_a_client_names_a_server_by(self, state: Path, monkeypatch: Any) -> None:
        register(state, 9005)
        monkeypatch.setattr(
            serving,
            "meta_at",
            lambda host, port: {
                "app": "demoapp",
                "pid": 4242,
                "workspace": {"path": "", "template": "detection", "state_dir": "/tmp/f"},
            },
        )
        entry = [server for server in serving.running_servers(lifecycle.spec) if server["port"] == 9005][0]
        assert entry == {
            "host": "127.0.0.1",
            "port": 9005,
            "path": "",
            "template": "detection",
            "identity": "type:detection",
            "label": "the detection type workspace",
            "state_dir": "/tmp/f",
            "pid": 4242,
        }

    def test_the_lowest_port_comes_first(self, state: Path, monkeypatch: Any) -> None:
        for port in (9300, 9100, 9200):
            register(state, port)
        Fleet(monkeypatch, {9300: {}, 9100: {}, 9200: {}})
        assert [server["port"] for server in serving.running_servers(lifecycle.spec)] == [9100, 9200, 9300]


class TestBareStop:
    def test_one_running_stops_it(self, state: Path, monkeypatch: Any) -> None:
        fleet = Fleet(monkeypatch, {D: {"path": "/w.yaml"}})
        lifecycle.stop()
        assert fleet.ports == [D]

    def _two(self, state: Path, monkeypatch: Any) -> Fleet:
        register(state, D + 1, path="/b.yaml")
        return Fleet(monkeypatch, {D: {"path": "/a.yaml"}, D + 1: {"path": "/b.yaml"}})

    def test_several_ask_and_a_number_stops_that_one(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        fleet = self._two(state, monkeypatch)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "2")
        lifecycle.stop()
        assert fleet.ports == [D + 1]
        out = capsys.readouterr().out
        assert "1." in out and "/a.yaml" in out and "2." in out and "/b.yaml" in out

    def test_answering_all_stops_everything(self, state: Path, monkeypatch: Any) -> None:
        fleet = self._two(state, monkeypatch)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "a")
        lifecycle.stop()
        assert fleet.ports == [D, D + 1]

    def test_q_stops_nothing(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        fleet = self._two(state, monkeypatch)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "q")
        lifecycle.stop()
        assert fleet.stopped == []
        assert "stopped nothing" in said(capsys)

    def test_the_all_flag_never_asks(self, state: Path, monkeypatch: Any) -> None:
        fleet = self._two(state, monkeypatch)
        monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("--all must not ask"))
        lifecycle.stop(all=True)
        assert fleet.ports == [D, D + 1]

    def test_non_interactive_with_several_refuses_naming_the_flag(self, state: Path, monkeypatch: Any) -> None:
        self._two(state, monkeypatch)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(ServeError, match="2 demoapp servers are running — pass --all"):
            lifecycle.stop()

    def test_none_running_stops_the_last_workspaces_port_only(self, state: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(serving, "pids_listening", lambda port: [])
        fleet = Fleet(monkeypatch, {})
        lifecycle.stop()
        assert fleet.ports == [D], "only the app's default port"


class TestSpecificStop:
    def test_a_named_workspace_stops_where_it_actually_serves(
        self, state: Path, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """THE auto-port seam: the file says the default port, the server runs on 9001 — stop 9001."""
        target = _saved(tmp_path)
        register(state, 9001, path=str(target))
        fleet = Fleet(monkeypatch, {9001: {"path": str(target)}})
        lifecycle.stop(str(target))
        assert fleet.ports == [9001]

    def test_a_named_workspace_not_running_stops_its_port(self, state: Path, tmp_path: Path, monkeypatch: Any) -> None:
        target = _saved(tmp_path)
        monkeypatch.setattr(serving, "pids_listening", lambda port: [])
        fleet = Fleet(monkeypatch, {})
        lifecycle.stop(str(target))
        assert fleet.ports == [D]

    def test_a_type_name_stops_the_server_serving_that_type(self, state: Path, monkeypatch: Any) -> None:
        register(state, 8790, template="classification")
        fleet = Fleet(monkeypatch, {8790: {"template": "classification"}})
        lifecycle.stop("classification")
        assert fleet.ports == [8790]


class TestStopNeverBuildsTheWorkspace:
    """A stop of a workspace whose classes are GONE must still stop by its ports — measured: a module the file named
    vanished and a stop died with "Cannot resolve class" instead of stopping anything."""

    def test_stop_reads_ports_through_unresolvable_tags(self, tmp_path: Path, monkeypatch: Any) -> None:
        target = tmp_path / "gone.workspace.yaml"
        target.write_text(
            "workspace: !class:vanished.Workspace\n  port: 9313\n  sources:\n    - !class:vanished_module.Source {}\n"
        )
        seen: List[int] = []
        monkeypatch.setattr(serving, "stop_both", lambda spec, address, **kwargs: seen.append(address.port))
        lifecycle.stop(str(target))
        assert seen == [9313]

    def test_an_unreadable_file_still_stops_the_default_port(self, tmp_path: Path, monkeypatch: Any) -> None:
        target = tmp_path / "broken.yaml"
        target.write_text(":: not yaml ::[")
        seen: List[int] = []
        monkeypatch.setattr(serving, "stop_both", lambda spec, address, **kwargs: seen.append(address.port))
        lifecycle.stop(str(target))
        assert seen == [D]


class TestAStopSignalsOnlyTheServersItStops:
    """Every form of stop signals ONLY the pids listening on the port(s) of the server(s) it stops.

    Measured with an app on the same seam: a stop that believed it ended the LAST server swept every process of the
    app's MCP server, found by command line — seven processes of the user's OTHER sessions, whose tools then died. A
    process that binds no port belongs to whoever started it.
    """

    def test_by_port_the_last_server(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        register(state, 8196, path="/home/me/a.workspace.yaml")
        machine = Machine(monkeypatch, {8196: {"path": "/home/me/a.workspace.yaml"}})
        lifecycle.stop("8196")
        only_these_were_stopped(
            machine, capsys, [81960], "demoapp (/home/me/a.workspace.yaml): stopped pid 81960 (port 8196)\n"
        )

    def test_by_port_with_another_server_up(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        register(state, 8788, path="/a.yaml")
        register(state, 8790, path="/b.yaml")
        machine = Machine(monkeypatch, {8788: {"path": "/a.yaml"}, 8790: {"path": "/b.yaml"}})
        lifecycle.stop("8790")
        only_these_were_stopped(machine, capsys, [87900], "demoapp (/b.yaml): stopped pid 87900 (port 8790)\n")
        assert 87880 in machine.alive, "the other server keeps running"

    def test_by_workspace_file(self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
        target = _saved(tmp_path)
        register(state, 9001, path=str(target))
        machine = Machine(monkeypatch, {9001: {"path": str(target)}})
        lifecycle.stop(str(target))
        only_these_were_stopped(machine, capsys, [90010], f"demoapp ({target}): stopped pid 90010 (port 9001)\n")

    def test_by_type_name(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        register(state, 8790, template="classification")
        machine = Machine(monkeypatch, {8790: {"template": "classification"}})
        lifecycle.stop("classification")
        only_these_were_stopped(
            machine, capsys, [87900], "demoapp (the classification type workspace): stopped pid 87900 (port 8790)\n"
        )

    _BOTH_STOPPED = (
        f"demoapp (/a.yaml): stopped pid {D * 10} (port {D})\n"
        f"demoapp (/b.yaml): stopped pid {(D + 1) * 10} (port {D + 1})\n"
    )

    def test_all(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        register(state, D + 1, path="/b.yaml")
        machine = Machine(monkeypatch, {D: {"path": "/a.yaml"}, D + 1: {"path": "/b.yaml"}})
        lifecycle.stop(all=True)
        only_these_were_stopped(machine, capsys, [D * 10, (D + 1) * 10], self._BOTH_STOPPED)

    def test_bare_stop_of_the_one_server(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        machine = Machine(monkeypatch, {D: {"path": "/a.yaml"}})
        lifecycle.stop()
        only_these_were_stopped(machine, capsys, [D * 10], f"demoapp (/a.yaml): stopped pid {D * 10} (port {D})\n")

    _ASKED = (
        "running demoapp servers:\n"
        "  1. /a.yaml\n"
        f"     http://127.0.0.1:{D}\n"
        "  2. /b.yaml\n"
        f"     http://127.0.0.1:{D + 1}\n"
    )

    def _two_and_the_answer(self, state: Path, monkeypatch: Any, answer: str) -> Machine:
        register(state, D + 1, path="/b.yaml")
        machine = Machine(monkeypatch, {D: {"path": "/a.yaml"}, D + 1: {"path": "/b.yaml"}})
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": answer)
        return machine

    def test_bare_stop_answering_all(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        machine = self._two_and_the_answer(state, monkeypatch, "a")
        lifecycle.stop()
        only_these_were_stopped(machine, capsys, [D * 10, (D + 1) * 10], self._ASKED + self._BOTH_STOPPED)

    def test_bare_stop_answering_one(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        machine = self._two_and_the_answer(state, monkeypatch, "2")
        lifecycle.stop()
        only_these_were_stopped(
            machine,
            capsys,
            [(D + 1) * 10],
            self._ASKED + f"demoapp (/b.yaml): stopped pid {(D + 1) * 10} (port {D + 1})\n",
        )

    def test_named_file_on_an_undiscovered_port(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """The not-discovered fallback: unregistered on 9001, answering as the file named."""
        target = _saved(tmp_path, port=9001)
        machine = Machine(monkeypatch, {9001: {"path": str(target.resolve())}})
        lifecycle.stop(str(target))
        only_these_were_stopped(machine, capsys, [90010], "demoapp: stopped pid 90010 (port 9001)\n")

    def test_bare_stop_falls_back_to_the_last_served_file(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        target = _saved(tmp_path, port=9001)
        serving.remember(lifecycle.spec, f"file:{target}")
        machine = Machine(monkeypatch, {9001: {"path": str(target.resolve())}})
        lifecycle.stop()
        only_these_were_stopped(machine, capsys, [90010], "demoapp: stopped pid 90010 (port 9001)\n")

    def test_bare_stop_falls_back_to_the_last_served_type(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        serving.remember(lifecycle.spec, "type:classification")
        machine = Machine(monkeypatch, {})
        lifecycle.stop()
        only_these_were_stopped(machine, capsys, [], f"demoapp: nothing listening on port {D}\n")

    @pytest.mark.parametrize("remembered", ["", "empty", "file:/gone/w.workspace.yaml"])
    def test_bare_stop_with_no_file_or_type_remembered_refuses_a_listener_that_is_not_the_app(
        self, state: Path, monkeypatch: Any, capsys: Any, remembered: str
    ) -> None:
        """No server of the app answers and no file or type is remembered: the bare stop looks at the default port
        and REFUSES a listener there that does not answer as the app — exactly as ``stop <port>`` does.

        Before (measured): ``stopped pid 87870 (port 8787)`` — it SIGTERMed whatever held the port. The accepted cost:
        a server too wedged to answer its status check is refused the same way, and is ended with ``kill``.
        """
        if remembered:
            serving.remember(lifecycle.spec, remembered)
        machine = Machine(monkeypatch, {}, listeners={D: [55550]})
        with pytest.raises(
            ServeError,
            match=rf"^port {D} is held by something that is not demoapp \(pid 55550\b.*\) — not stopping it$",
        ):
            lifecycle.stop()
        only_these_were_stopped(machine, capsys, [], "")

    def test_all_with_nothing_answering_stops_nothing(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        """``--all`` stops every server of the app; with none answering it says so and signals nothing — whatever
        else listens on the default port."""
        machine = Machine(monkeypatch, {}, listeners={D: [55550]})
        lifecycle.stop(all=True)
        only_these_were_stopped(machine, capsys, [], "no demoapp is running\n")

    def test_all_still_stops_the_last_served_workspace_on_a_port_discovery_does_not_probe(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """A server the registry lost on its workspace's own port: discovery misses it, the last served workspace's
        port finds it, and ``--all`` stops it — naming what it stopped."""
        target = _saved(tmp_path, port=9001)
        serving.remember(lifecycle.spec, f"file:{target}")
        machine = Machine(monkeypatch, {9001: {"path": str(target.resolve())}})
        lifecycle.stop(all=True)
        only_these_were_stopped(
            machine, capsys, [90010], f"demoapp ({target.resolve()}): stopped pid 90010 (port 9001)\n"
        )

    def test_all_stops_a_server_on_the_last_served_port_whatever_it_serves(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        target = _saved(tmp_path, port=9001)
        serving.remember(lifecycle.spec, f"file:{target}")
        machine = Machine(monkeypatch, {9001: {"path": "/other.workspace.yaml"}})
        lifecycle.stop(all=True)
        only_these_were_stopped(
            machine, capsys, [90010], "demoapp (/other.workspace.yaml): stopped pid 90010 (port 9001)\n"
        )

    def test_a_bare_restart_with_nothing_remembered_refuses_a_listener_that_is_not_the_app(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        machine = Machine(monkeypatch, {}, listeners={D: [55550]})
        served: List[Any] = []
        monkeypatch.setattr(lifecycle, "serve", lambda target, **kwargs: served.append(target))
        with pytest.raises(ServeError, match=rf"^port {D} is held by something that is not demoapp"):
            lifecycle.restart()
        assert served == [], "nothing is served on a port a foreign process holds"
        only_these_were_stopped(machine, capsys, [], "restarting an empty workspace (nothing was served before)\n")

    def test_restarting_a_named_workspace_refuses_a_listener_on_its_port_that_is_not_the_app(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        target = _saved(tmp_path, port=9001)
        machine = Machine(monkeypatch, {}, listeners={9001: [55550]})
        served: List[Any] = []
        monkeypatch.setattr(lifecycle, "serve", lambda target, **kwargs: served.append(target))
        with pytest.raises(ServeError, match=r"^port 9001 is held by something that is not demoapp \(pid 55550\b"):
            lifecycle.restart(str(target))
        assert served == []
        only_these_were_stopped(machine, capsys, [], "")

    def test_a_port_nothing_serves(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        machine = Machine(monkeypatch, {})
        lifecycle.stop("8799")
        only_these_were_stopped(machine, capsys, [], "demoapp: nothing listening on port 8799\n")

    def test_restart_is_unchanged(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        machine = Machine(monkeypatch, {D: {"path": ""}})
        monkeypatch.setattr(lifecycle, "serve", lambda target, **kwargs: None)
        monkeypatch.setattr(serving, "tell_server_it_restarts", lambda host, port: None)
        lifecycle.restart()
        only_these_were_stopped(
            machine,
            capsys,
            [D * 10],
            f"restarting an empty workspace (nothing was served before)\ndemoapp: stopped pid {D * 10} (port {D})\n",
        )


class TestTheSignalsAStopSends:
    """What ``stop_port`` sends the listeners it found: SIGTERM, a wait of up to five seconds, then SIGKILL for a
    listener still alive — and a listener that exited between ``lsof`` and the signal is no error. The clock is a
    stand-in, so the five seconds pass at once."""

    @staticmethod
    def _clock(monkeypatch: Any) -> List[float]:
        waits: List[float] = []
        clock = SimpleNamespace(monotonic=lambda: 6.0 if waits else 0.0, sleep=waits.append)
        monkeypatch.setattr(serving, "time", clock)
        return waits

    def test_a_listener_that_ignores_sigterm_is_killed_after_the_wait(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        register(state, 8196, path="/home/me/a.workspace.yaml")
        machine = Machine(monkeypatch, {8196: {"path": "/home/me/a.workspace.yaml"}}, ignores_term=[81960])
        waits = self._clock(monkeypatch)
        lifecycle.stop("8196")
        assert machine.signalled == [(81960, signal.SIGTERM), (81960, signal.SIGKILL)]
        assert waits == [0.2], "it waited before escalating"
        assert 81960 not in machine.alive
        assert capsys.readouterr().out == "demoapp (/home/me/a.workspace.yaml): stopped pid 81960 (port 8196)\n"

    def test_a_listener_gone_before_the_signal_is_no_error(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        register(state, 8196, path="/home/me/a.workspace.yaml")
        machine = Machine(
            monkeypatch,
            {8196: {"path": "/home/me/a.workspace.yaml"}},
            listeners={8196: [81960, 81961]},
            exited=[81961],
        )
        waits = self._clock(monkeypatch)
        lifecycle.stop("8196")
        assert machine.signalled == [(81960, signal.SIGTERM)], "the gone pid could not be signalled"
        assert waits == [], "nothing was left alive to wait for"
        assert capsys.readouterr().out == "demoapp (/home/me/a.workspace.yaml): stopped pid 81960, 81961 (port 8196)\n"


#: A stand-in for a process that binds no port (an MCP client's server), waiting for a signal that must never come.
_SLEEP_FOREVER = "import signal\nsignal.pause()\n"


@pytest.fixture()
def real_processes(tmp_path: Path, monkeypatch: Any) -> Iterator[SimpleNamespace]:
    """Two REAL processes this test owns, killed in teardown: ``mcp`` runs as ``<python> <tmp>/demoapp-mcp``,
    ``server`` stands in for the server listening on 8196.

    The stop under test may list and signal THESE and nothing else: ``lsof`` answers the server's pid for 8196,
    ``ps`` is the real ``ps`` restricted to these two pids, and ``os.kill`` refuses any other pid.
    """
    script = tmp_path / "demoapp-mcp"
    script.write_text(_SLEEP_FOREVER)
    mcp = subprocess.Popen([sys.executable, str(script)])
    server = subprocess.Popen([sys.executable, "-c", _SLEEP_FOREVER])
    children = {mcp.pid: mcp, server.pid: server}
    real_run, real_kill = subprocess.run, os.kill
    commands: List[str] = []

    def run_tool(argv: List[str], **kwargs: Any) -> Any:
        commands.append(argv[0])
        if argv[0] == "ss":
            raise FileNotFoundError("ss")
        if argv[0] == "lsof":
            listening = server.poll() is None and any(arg == "-iTCP:8196" for arg in argv)
            return SimpleNamespace(stdout=f"p{server.pid}\n" if listening else "")
        if argv[0] == "ps":
            return real_run(["ps", "-o", "pid=,args=", "-p", f"{mcp.pid},{server.pid}"], **kwargs)
        raise AssertionError(f"a stop ran {argv!r}")

    def kill(pid: int, sig: int) -> None:
        if pid not in children:
            raise AssertionError(f"a stop signalled pid {pid}, which this test did not start")
        real_kill(pid, sig)
        if sig != 0:
            children[pid].wait(timeout=10)

    def probe(host: str, port: int) -> Optional[Dict[str, Any]]:
        if port != 8196 or server.poll() is not None:
            return None
        return {"app": "demoapp", "workspace": {"path": "/home/me/a.workspace.yaml", "template": ""}}

    monkeypatch.setattr(serving.subprocess, "run", run_tool)
    monkeypatch.setattr(serving.os, "kill", kill)
    monkeypatch.setattr(serving, "meta_at", probe)
    try:
        yield SimpleNamespace(mcp=mcp, server=server, commands=commands)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.kill()
            child.wait()


class TestARealProcessThatBindsNoPortOutlivesTheStop:
    def test_the_last_server_stops_and_the_other_process_keeps_running(
        self, state: Path, real_processes: SimpleNamespace, capsys: Any
    ) -> None:
        register(state, 8196, path="/home/me/a.workspace.yaml")
        lifecycle.stop("8196")
        assert real_processes.server.poll() is not None, "the server on 8196 was stopped"
        assert real_processes.mcp.poll() is None, "the process that binds no port is still running"
        assert "ps" not in real_processes.commands
        assert capsys.readouterr().out == (
            f"demoapp (/home/me/a.workspace.yaml): stopped pid {real_processes.server.pid} (port 8196)\n"
        )


class TestAPortServingSomethingElseIsRefused:
    """The fallback stops a port only when what ANSWERS there is what was named.

    Measured: nothing was serving the type named, so its stop fell through to the type's DEFAULT port and stopped a
    server serving another workspace file — reporting ``stopped pid 28992 (port 8787)``, which names no workspace.
    The fallback exists for a server the registry does not know, and that case is kept: the IDENTITY decides.
    """

    def test_a_type_name_never_stops_a_different_workspace(self, state: Path, monkeypatch: Any) -> None:
        fleet = Fleet(monkeypatch, {D: {"path": "/Users/me/detector.workspace.yaml"}})
        with pytest.raises(ServeError) as refusal:
            lifecycle.stop("detection")
        assert "detector.workspace.yaml" in str(refusal.value), "the refusal names what is actually serving"
        assert "the detection type workspace" in str(refusal.value), "…and what was asked for"
        assert fleet.stopped == [], "nothing was stopped"

    def test_a_file_name_never_stops_a_different_workspace(self, state: Path, tmp_path: Path, monkeypatch: Any) -> None:
        target = _saved(tmp_path, name="mine.workspace.yaml")
        fleet = Fleet(monkeypatch, {D: {"path": "/Users/me/other.workspace.yaml"}})
        with pytest.raises(ServeError, match="other.workspace.yaml"):
            lifecycle.stop(str(target))
        assert fleet.stopped == []

    def test_the_refusal_says_how_to_stop_the_one_that_IS_running(self, state: Path, monkeypatch: Any) -> None:
        Fleet(monkeypatch, {D: {"path": "/Users/me/detector.workspace.yaml"}})
        with pytest.raises(ServeError) as refusal:
            lifecycle.stop("detection")
        assert "'demoapp stop /Users/me/detector.workspace.yaml' stops that one" in str(refusal.value)
        assert "'demoapp stop --all' stops every server" in str(refusal.value)

    def test_a_type_serving_names_its_type_in_the_refusal(self, state: Path, tmp_path: Path, monkeypatch: Any) -> None:
        target = _saved(tmp_path)
        Fleet(monkeypatch, {D: {"template": "classification"}})
        with pytest.raises(ServeError, match="serves the classification type workspace.*'demoapp stop classification'"):
            lifecycle.stop(str(target))

    def test_the_same_workspace_on_an_UNDISCOVERED_port_is_still_stopped(
        self, state: Path, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """What the fallback is FOR: a server the registry does not know, answering as itself."""
        target = _saved(tmp_path, port=9001)
        fleet = Fleet(monkeypatch, {9001: {"path": str(target.resolve())}})
        monkeypatch.setattr(serving, "pids_listening", lambda port: [4242] if port == 9001 else [])
        lifecycle.stop(str(target))
        assert fleet.ports == [9001]

    def test_a_port_held_by_something_that_is_not_the_app_is_refused(
        self, state: Path, tmp_path: Path, monkeypatch: Any
    ) -> None:
        target = _saved(tmp_path)
        fleet = Fleet(monkeypatch, {})  # nothing ANSWERS as the app…
        monkeypatch.setattr(serving, "pids_listening", lambda port: [1234] if port == D else [])
        monkeypatch.setattr(serving, "port_holder", lambda port: "")
        with pytest.raises(ServeError, match="not demoapp"):
            lifecycle.stop(str(target))
        assert fleet.stopped == [], "a foreign process on the port is never SIGTERMed by a stop"

    def test_a_server_of_another_app_is_a_foreign_holder(self, state: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(serving, "meta_at", lambda host, port: {"app": "otherapp", "workspace": {}})
        monkeypatch.setattr(serving, "pids_listening", lambda port: [1234])
        monkeypatch.setattr(serving, "port_holder", lambda port: " (pid 1234 otherapp)")
        stopped: List[int] = []
        monkeypatch.setattr(serving, "stop_port", lambda name, port: noting(stopped, "")(port))
        with pytest.raises(ServeError, match="held by something that is not demoapp \\(pid 1234 otherapp\\)"):
            lifecycle.stop("9001")
        assert stopped == []

    def test_nothing_listening_is_still_the_plain_report(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        monkeypatch.setattr(serving, "pids_listening", lambda port: [999] if port == 8188 else [])
        fleet = Fleet(monkeypatch, {})
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        lifecycle.stop()  # no refusal: nothing identifiable holds the port
        assert fleet.ports == [D]


class TestAPortNumberNamesTheServerOnIt:
    """``stop 8790`` / ``restart 8790`` — the port a server runs on names it, exactly as its workspace file or type
    name does. Measured before: ``stop 8790`` read "8790" as a workspace FILE, found none and fell back to the DEFAULT
    port. ``--port`` keeps its meaning: where the restarted workspace serves."""

    @staticmethod
    def _restartable(monkeypatch: Any) -> List[Any]:
        served: List[Any] = []
        monkeypatch.setattr(lifecycle, "serve", lambda target, **kwargs: served.append(target))
        monkeypatch.setattr(serving, "tell_server_it_restarts", lambda host, port: None)
        return served

    @staticmethod
    def _listening(monkeypatch: Any, fleet: Fleet) -> None:
        monkeypatch.setattr(serving, "pids_listening", lambda port: [4242] if port in fleet.alive else [])

    def test_stop_stops_the_server_on_that_port_and_leaves_the_others(self, state: Path, monkeypatch: Any) -> None:
        register(state, 8788, path="/a.yaml")
        register(state, 8790, path="/b.yaml")
        fleet = Fleet(monkeypatch, {8788: {"path": "/a.yaml"}, 8790: {"path": "/b.yaml"}})
        lifecycle.stop("8790")
        assert fleet.ports == [8790]
        assert 8788 in fleet.alive

    def test_stopping_the_last_server_by_its_port(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        register(state, 8790, template="classification")
        fleet = Fleet(monkeypatch, {8790: {"template": "classification"}})
        lifecycle.stop("8790")
        assert fleet.ports == [8790]
        assert "demoapp (the classification type workspace): stopped (port 8790)" in said(capsys)

    def test_stop_finds_a_server_the_registry_does_not_know(self, state: Path, monkeypatch: Any) -> None:
        fleet = Fleet(monkeypatch, {9001: {"path": "/w.yaml"}})  # never registered
        lifecycle.stop("9001")
        assert fleet.ports == [9001]

    def test_a_port_nothing_serves_is_never_the_default_port(self, state: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(serving, "pids_listening", lambda port: [])
        fleet = Fleet(monkeypatch, {})
        lifecycle.stop("8799")
        assert fleet.ports == [8799], "the port typed, never the default"

    def test_a_port_held_by_something_that_is_not_the_app_is_refused(self, state: Path, monkeypatch: Any) -> None:
        fleet = Fleet(monkeypatch, {})
        monkeypatch.setattr(serving, "pids_listening", lambda port: [1234] if port == 8188 else [])
        monkeypatch.setattr(serving, "port_holder", lambda port: "")
        with pytest.raises(ServeError, match="port 8188 is held by something that is not demoapp"):
            lifecycle.stop("8188")
        assert fleet.stopped == [], "a foreign process is never SIGTERMed"

    def test_a_number_that_is_not_a_port_is_refused(self, state: Path) -> None:
        with pytest.raises(ServeError, match="70000 is not a port"):
            lifecycle.stop("70000")
        with pytest.raises(ServeError, match="0 is not a port"):
            lifecycle.restart("0")

    def test_a_workspace_file_named_like_a_number_is_still_that_file(
        self, state: Path, tmp_path: Path, monkeypatch: Any
    ) -> None:
        target = tmp_path / "8790"
        DemoWorkspace(port=9002).save(str(target))
        monkeypatch.chdir(tmp_path)
        register(state, 8790, path="/other.yaml")
        register(state, 9002, path=str(target))
        fleet = Fleet(monkeypatch, {8790: {"path": "/other.yaml"}, 9002: {"path": str(target.resolve())}})
        lifecycle.stop("8790")
        assert fleet.ports == [9002], "the file wins; the server on 8790 stays"

    def test_the_cli_passes_the_port_through(self, state: Path, monkeypatch: Any) -> None:
        fleet = Fleet(monkeypatch, {9001: {"path": "/w.yaml"}})
        run(["stop", "9001"], monkeypatch)
        assert fleet.ports == [9001]

    def test_restart_serves_the_workspace_on_that_port_on_that_port(
        self, state: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        target = _saved(tmp_path, name="b.workspace.yaml")  # its own port is the default
        identity = str(target.resolve())
        register(state, 8790, path=identity)
        fleet = Fleet(monkeypatch, {8790: {"path": identity}})
        self._listening(monkeypatch, fleet)
        served = self._restartable(monkeypatch)
        lifecycle.restart("8790")
        (workspace,) = served
        assert workspace.path == identity and workspace.port == 8790, "where it ran, not the file's default"
        assert fleet.ports == [8790]
        assert f"restarting {identity} (port 8790)" in capsys.readouterr().out
        assert (state / "last-workspace").read_text().strip() == f"file:{identity}"

    def test_restart_of_a_type_workspace_by_its_port(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        register(state, 8790, template="classification")
        fleet = Fleet(monkeypatch, {8790: {"template": "classification"}})
        self._listening(monkeypatch, fleet)
        served = self._restartable(monkeypatch)
        lifecycle.restart("8790")
        (workspace,) = served
        assert workspace.template == "classification" and workspace.port == 8790
        assert "restarting the classification type workspace (port 8790)" in said(capsys)

    def test_restart_of_an_empty_workspace_by_its_port(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        fleet = Fleet(monkeypatch, {8790: {}})  # unregistered too
        self._listening(monkeypatch, fleet)
        served = self._restartable(monkeypatch)
        lifecycle.restart("8790")
        (workspace,) = served
        assert workspace.path == "" and workspace.template == "" and workspace.port == 8790
        assert "restarting an empty workspace (port 8790)" in said(capsys)

    def test_restart_with_port_moves_the_server_there(self, state: Path, monkeypatch: Any) -> None:
        register(state, 8790, template="classification")
        register(state, 8788, path="/a.yaml")
        fleet = Fleet(monkeypatch, {8790: {"template": "classification"}, 8788: {"path": "/a.yaml"}})
        self._listening(monkeypatch, fleet)
        served = self._restartable(monkeypatch)
        lifecycle.restart("8790", port=8795)
        (workspace,) = served
        assert workspace.port == 8795
        assert fleet.ports == [8790], "the server named is the one stopped"

    def test_restart_of_a_port_nothing_serves_is_refused_naming_what_runs(self, state: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(serving, "pids_listening", lambda port: [])
        register(state, 8788, path="/a.yaml")
        fleet = Fleet(monkeypatch, {8788: {"path": "/a.yaml"}})
        served = self._restartable(monkeypatch)
        with pytest.raises(ServeError, match="nothing is serving on port 8799 — running: 8788 /a.yaml"):
            lifecycle.restart("8799")
        assert served == [] and fleet.stopped == []

    def test_restart_with_nothing_running_says_so(self, state: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(serving, "pids_listening", lambda port: [])
        Fleet(monkeypatch, {})
        with pytest.raises(ServeError, match="nothing is serving on port 8799 — no demoapp is running"):
            lifecycle.restart("8799")

    def test_a_served_file_that_was_deleted_is_refused_before_anything_stops(
        self, state: Path, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Measured: a test-suite run deleted the workspace file its server was serving."""
        gone = str(tmp_path / "failures.workspace.yaml")
        fleet = Fleet(monkeypatch, {8788: {"path": gone}})
        served = self._restartable(monkeypatch)
        with pytest.raises(FileNotFoundError, match="failures.workspace.yaml"):
            lifecycle.restart("8788")
        assert served == [] and fleet.stopped == [], "the running server is left alone"


class TestRestartingAPortSomethingElseHolds:
    """``restart <port>`` where a program that is NOT the app listens.

    Measured with python's ``http.server`` on 8197 and one server running on 8193: ``restart 8197`` said ``nothing is
    serving on port 8197 — running: 8193 …`` — safe, but untrue. A restart now says the refusal ``stop 8197`` gives,
    from the same helper, before anything is stopped or opened.
    """

    HOLDER = 29923

    @staticmethod
    def _serving_nothing(monkeypatch: Any) -> List[Any]:
        served: List[Any] = []
        monkeypatch.setattr(lifecycle, "serve", lambda target, **kwargs: served.append(target))
        monkeypatch.setattr(serving, "tell_server_it_restarts", lambda host, port: None)
        return served

    def _with_a_foreign_program_on_8197(self, state: Path, monkeypatch: Any) -> Machine:
        register(state, 8193, template="detection")
        return Machine(monkeypatch, {8193: {"template": "detection"}}, listeners={8193: [81930], 8197: [self.HOLDER]})

    def test_restart_refuses_with_the_message_a_stop_of_that_port_gives(self, state: Path, monkeypatch: Any) -> None:
        machine = self._with_a_foreign_program_on_8197(state, monkeypatch)
        served = self._serving_nothing(monkeypatch)
        with pytest.raises(ServeError) as stopped:
            lifecycle.stop("8197")
        with pytest.raises(ServeError) as restarted:
            lifecycle.restart("8197")
        assert str(restarted.value) == str(stopped.value), "one wording, from one helper"
        assert str(restarted.value).startswith(
            f"port 8197 is held by something that is not demoapp (pid {self.HOLDER}"
        ) and str(restarted.value).endswith(") — not stopping it")
        assert served == [] and machine.signalled == [], "nothing was served, nothing was signalled"

    def test_nothing_is_stopped_opened_announced_or_remembered(
        self, state: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        machine = self._with_a_foreign_program_on_8197(state, monkeypatch)
        served = self._serving_nothing(monkeypatch)
        with pytest.raises(ServeError, match="held by something that is not demoapp"):
            lifecycle.restart("8197")
        assert (served, machine.signalled) == ([], [])
        assert 81930 in machine.alive and self.HOLDER in machine.alive
        assert capsys.readouterr().out == "", "no 'restarting …' line: nothing was resolved"
        assert not (state / "last-workspace").exists(), "a refused restart is not a workspace served"

    def test_the_port_it_would_move_to_changes_nothing(self, state: Path, monkeypatch: Any) -> None:
        machine = self._with_a_foreign_program_on_8197(state, monkeypatch)
        served = self._serving_nothing(monkeypatch)
        with pytest.raises(ServeError, match=r"^port 8197 is held by something that is not demoapp"):
            lifecycle.restart("8197", port=8199)
        assert (served, machine.signalled) == ([], [])

    def test_a_port_nothing_listens_on_is_still_nothing_serving_naming_what_runs(
        self, state: Path, monkeypatch: Any
    ) -> None:
        register(state, 8193, template="detection")
        machine = Machine(monkeypatch, {8193: {"template": "detection"}})
        served = self._serving_nothing(monkeypatch)
        with pytest.raises(ServeError) as refused:
            lifecycle.restart("8197")
        assert str(refused.value) == "nothing is serving on port 8197 — running: 8193 the detection type workspace"
        assert (served, machine.signalled) == ([], [])

    def test_nothing_listening_and_nothing_running_says_so(self, state: Path, monkeypatch: Any) -> None:
        machine = Machine(monkeypatch, {})
        with pytest.raises(ServeError) as refused:
            lifecycle.restart("8197")
        assert str(refused.value) == "nothing is serving on port 8197 — no demoapp is running"
        assert machine.signalled == []

    def test_a_server_answering_there_is_restarted_as_before(self, state: Path, monkeypatch: Any, capsys: Any) -> None:
        register(state, 8790, template="classification")
        machine = Machine(
            monkeypatch, {8790: {"template": "classification"}}, listeners={8790: [87900], 8197: [self.HOLDER]}
        )
        served = self._serving_nothing(monkeypatch)
        lifecycle.restart("8790")
        (workspace,) = served
        assert workspace.template == "classification" and workspace.port == 8790
        assert machine.signalled == [(87900, signal.SIGTERM)], "only the app's own listener"
        assert capsys.readouterr().out == (
            "restarting the classification type workspace (port 8790)\ndemoapp: stopped pid 87900 (port 8790)\n"
        )
