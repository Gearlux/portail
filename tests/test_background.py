"""``<name> [workspace] --background`` / ``-b`` — serve detached, and say where it went.

Serving holds the terminal (``app.run()``), so the only way to get a prompt back was another terminal or ctrl-Z.

What the flag must NOT change: the port conflict is resolved by the PARENT, the process holding the terminal —
``--auto-port`` and the ``[Y/n]`` ask keep working, and the child is then pinned to the port that came out of it. And
the parent does not return until the server ANSWERS: a detached process that dies on a bad workspace would otherwise
leave the user with a prompt, no server, and nothing said.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from portail import serving
from portail.errors import ServeError
from tests.cli_output import said
from tests.demo_cli import DEMO_PORT, DEMO_SPEC, DemoWorkspace, app, lifecycle, open_type

URL = f"http://127.0.0.1:{DEMO_PORT}"


class Child:
    """A spawned server that answers after ``answers_after`` probes (never = dies)."""

    def __init__(self, answers_after: int = 1, exit_code: Optional[int] = None, output: str = "") -> None:
        self.pid = 4242
        self.answers_after = answers_after
        self.exit_code = exit_code
        self.output = output
        self.probes = 0

    def poll(self) -> Optional[int]:
        return self.exit_code


class Spawn:
    """Captures the argv/cwd/env a background start hands to Popen, and fakes the readiness probe."""

    def __init__(self, monkeypatch: Any, child: Optional[Child] = None) -> None:
        self.child = child or Child()
        self.argv: List[str] = []
        self.cwd = ""
        self.env: Dict[str, str] = {}
        self.served: List[Any] = []
        monkeypatch.setattr(serving.subprocess, "Popen", self._popen)
        monkeypatch.setattr(serving, "meta_at", self._meta)
        monkeypatch.setattr(serving, "port_in_use", lambda host, port: False)
        monkeypatch.setattr(serving.Server, "serve", lambda server: self.served.append(server.served))

    def _popen(self, argv: List[str], **kwargs: Any) -> Child:
        self.argv = list(argv)
        self.cwd = str(kwargs.get("cwd") or "")
        self.env = dict(kwargs.get("env") or {})
        sink: Any = kwargs.get("stderr")
        if self.child.output and hasattr(sink, "write"):
            sink.write(self.child.output.encode())
            sink.flush()
        return self.child

    def _meta(self, host: str, port: int) -> Optional[Dict[str, Any]]:
        self.child.probes += 1
        if self.child.probes >= self.child.answers_after:
            return {"app": "demoapp", "title": "x", "workspace": {"path": "", "template": ""}}
        return None


class TestTheFlagIsDeclared:
    def test_every_serving_command_takes_b(self) -> None:
        """``-b`` is what the user typed; liquifai expands a declared short before parsing."""
        for name in ("open", "workspace", "restart"):
            assert getattr(app._commands[name], "__liquifai_short__", {}) == {"b": "background"}, name

    def test_the_short_expands_to_the_long_option(self) -> None:
        from liquifai.core import expand_shorts

        shorts = getattr(app._commands["open"], "__liquifai_short__")
        assert expand_shorts(["w.yaml", "-b"], shorts) == ["w.yaml", "--background"]


class TestWhatTheChildIsAskedToDo:
    def test_a_saved_workspace_is_reopened_by_path_on_the_resolved_port(self, tmp_path: Path, monkeypatch: Any) -> None:
        spawn = Spawn(monkeypatch)
        target = DemoWorkspace(port=8791, path=str(tmp_path / "w.workspace.yaml"))
        lifecycle.serve(target, background=True)
        assert spawn.argv[1:3] == ["-m", "tests.demo_cli"]
        assert spawn.argv[3:5] == ["open", str(tmp_path / "w.workspace.yaml")]
        port = spawn.argv[spawn.argv.index("--port") + 1]
        assert port == "8791", "the PARENT resolved the port; the child may not re-pick"
        assert spawn.served == [], "the parent never serves in its own process"

    def test_a_type_workspace_is_reopened_by_type(self, monkeypatch: Any) -> None:
        spawn = Spawn(monkeypatch)
        lifecycle.serve(open_type("classification"), background=True)
        assert spawn.argv[3:5] == ["workspace", "classification"]

    def test_an_empty_workspace_names_no_target(self, monkeypatch: Any) -> None:
        spawn = Spawn(monkeypatch)
        lifecycle.serve(DemoWorkspace(), background=True)
        assert spawn.argv[3] == "open" and spawn.argv[4] == "--port"

    def test_it_runs_from_a_neutral_directory(self, monkeypatch: Any) -> None:
        """A mono-repo checkout shadows installed packages when a child runs from it."""
        spawn = Spawn(monkeypatch)
        lifecycle.serve(DemoWorkspace(), background=True)
        assert spawn.cwd == serving.neutral_cwd()

    def test_the_terminal_prepares_and_the_child_is_told_so(self, monkeypatch: Any, prepared: List[str]) -> None:
        """The app's preparation is said where the person reads — the child's output is discarded — and runs once."""
        spawn = Spawn(monkeypatch)
        lifecycle.serve(DemoWorkspace(title="mine"), background=True)
        assert prepared == ["mine"]
        assert spawn.env[DEMO_SPEC.env("prepared")] == "1"

    def test_the_child_skips_the_preparation_it_was_handed(self, monkeypatch: Any, prepared: List[str]) -> None:
        import os

        monkeypatch.setattr(serving.Server, "serve", lambda server: None)
        monkeypatch.setenv(DEMO_SPEC.env("prepared"), "1")
        lifecycle.serve(DemoWorkspace())
        assert prepared == [] and DEMO_SPEC.env("prepared") not in os.environ


class TestTheParentWaitsForAnAnswer:
    def test_it_returns_once_the_server_answers_and_names_the_pid(self, monkeypatch: Any, capsys: Any) -> None:
        Spawn(monkeypatch)
        lifecycle.serve(DemoWorkspace(), background=True)
        out = said(capsys)
        assert URL in out
        assert "background pid 4242" in out and "'demoapp stop' stops it" in out
        assert "demoapp.log" in out, "the child's stdout is discarded — say where the log went"

    def test_a_child_that_dies_first_is_an_error_carrying_its_own_output(self, monkeypatch: Any) -> None:
        Spawn(monkeypatch, Child(answers_after=99, exit_code=1, output="Cannot resolve class: det.DetSource\n"))
        with pytest.raises(ServeError, match="Cannot resolve class: det.DetSource"):
            lifecycle.serve(DemoWorkspace(), background=True)

    def test_a_child_that_dies_silently_says_its_code(self, monkeypatch: Any) -> None:
        Spawn(monkeypatch, Child(answers_after=99, exit_code=3))
        with pytest.raises(ServeError, match=r"^demoapp exited before it answered \(pid 4242, code 3\)$"):
            lifecycle.serve(DemoWorkspace(), background=True)

    def test_a_child_still_coming_up_is_reported_not_declared_dead(self, monkeypatch: Any, capsys: Any) -> None:
        Spawn(monkeypatch, Child(answers_after=99))
        monkeypatch.setattr(serving, "_BACKGROUND_WAIT_SECONDS", 0.0)
        lifecycle.serve(DemoWorkspace(), background=True)
        out = said(capsys)
        assert "still starting" in out and "pid 4242" in out

    def test_a_child_that_answers_on_a_later_probe_is_waited_for(self, monkeypatch: Any, capsys: Any) -> None:
        Spawn(monkeypatch, Child(answers_after=2))
        monkeypatch.setattr(serving.time, "sleep", lambda seconds: None)
        lifecycle.serve(DemoWorkspace(), background=True)
        assert "background pid 4242" in said(capsys)

    def test_without_the_flag_this_process_still_serves(self, monkeypatch: Any) -> None:
        spawn = Spawn(monkeypatch)
        lifecycle.serve(DemoWorkspace())
        assert spawn.served and spawn.argv == [], "the foreground path spawns nothing"

    def test_the_command_line_takes_b(self, monkeypatch: Any, cli_context: None) -> None:
        spawn = Spawn(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["demoapp", "workspace", "detection", "-b"])
        app.run()
        assert spawn.argv[3:5] == ["workspace", "detection"]
