"""An app whose workspace is a FOLDER, with a serving option of its own.

Three things an app of that shape needs from portail (``tests/demo_folder_cli.py`` is one):

* the workspace it names on the command line is a folder — ``AppSpec(workspace="folder")``. Measured before: ``open
  <folder>`` was refused as ``workspace file … does not exist`` and ``restart <folder>`` as ``neither a workspace file
  nor a type workspace``;
* an option of the app's own (``--notes-dir``) is carried into a ``--background`` child and into a restart. Measured
  before: the child's argv was rebuilt from the path and the port alone;
* the app picks which lifecycle commands portail registers, and keeps a ``/api/workspace`` route of its own.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from flask import Flask, jsonify

from portail import AppSpec, serving
from portail.web import mount_shell
from tests.cli_output import said
from tests.demo_cli import DemoWorkspace
from tests.demo_folder_cli import FOLDER_PORT, FOLDER_SPEC, FolderWorkspace, app, lifecycle
from tests.fleet import Fleet, serves

P = FOLDER_PORT


@pytest.fixture(autouse=True)
def _the_folder_apps_port_reads_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serving, "port_in_use", lambda host, port: False)


def run(argv: List[str], monkeypatch: Any) -> None:
    """Run the folder app's command line with ``argv`` (after the program name)."""
    from liquifai.context import set_context

    set_context(None)
    monkeypatch.setattr(sys, "argv", ["folderapp", *argv])
    app.run()


def running(path: Path, notes_dir: str = "") -> Dict[str, Any]:
    """What a running folder server's live answer says it serves."""
    return {"path": str(path), "app": "folderapp", "options": {"notes_dir": notes_dir} if notes_dir else {}}


class OptionFleet(Fleet):
    """A fleet whose servers answer the options they were started with."""

    def _meta(self, host: str, port: int) -> Optional[Dict[str, Any]]:
        info = self.alive.get(port)
        if info is None:
            return None
        workspace = {"path": info["path"], "template": "", "state_dir": info["path"], "options": info["options"]}
        return {"app": info["app"], "title": "x", "workspace": workspace}


class TestAWorkspaceThatIsAFolder:
    def test_the_spec_says_what_a_workspace_is(self) -> None:
        assert FOLDER_SPEC.workspace == "folder"
        assert AppSpec(name="x", cli_module="x").workspace == "file"

    def test_a_folder_is_opened_by_its_absolute_path(self, tmp_path: Path, monkeypatch: Any) -> None:
        (tmp_path / "graphs").mkdir()
        monkeypatch.chdir(tmp_path)
        served = serves(monkeypatch)
        run(["graphs"], monkeypatch)
        (opened,) = served
        assert isinstance(opened, FolderWorkspace) and opened.path == str((tmp_path / "graphs").resolve())

    def test_a_folder_that_is_not_there_is_one_clean_line_naming_a_folder(
        self, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        serves(monkeypatch)
        with pytest.raises(SystemExit):
            run([str(tmp_path / "nope")], monkeypatch)
        out = said(capsys)
        assert "workspace folder" in out and "nope" in out and "does not exist" in out
        assert "Traceback" not in out

    def test_a_file_is_not_a_folder_apps_workspace(self, tmp_path: Path) -> None:
        (tmp_path / "w.yaml").write_text("x: 1\n")
        with pytest.raises(FileNotFoundError, match="workspace folder .* does not exist"):
            lifecycle.open_file(str(tmp_path / "w.yaml"))

    def test_a_bare_start_serves_the_folder_it_is_typed_in_and_remembers_it(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.chdir(tmp_path)
        served = serves(monkeypatch)
        run([], monkeypatch)
        assert served[0].path == str(tmp_path.resolve())
        assert serving.last_spec(FOLDER_SPEC) == f"file:{tmp_path.resolve()}", "a bare restart serves it again"

    def test_stop_names_a_running_server_by_its_folder(self, tmp_path: Path, monkeypatch: Any) -> None:
        one, two = tmp_path / "one", tmp_path / "two"
        one.mkdir()
        two.mkdir()
        fleet = OptionFleet(monkeypatch, {P: running(one), P + 1: running(two)})
        (FOLDER_SPEC.state_dir() / "servers").mkdir(parents=True)
        for port in (P, P + 1):
            (FOLDER_SPEC.state_dir() / "servers" / f"{port}.json").write_text(
                f'{{"host": "127.0.0.1", "port": {port}, "path": "", "template": "", "pid": 1}}'
            )
        run(["stop", str(two)], monkeypatch)
        assert fleet.ports == [P + 1]

    def test_restart_takes_a_folder(self, tmp_path: Path, monkeypatch: Any) -> None:
        (tmp_path / "graphs").mkdir()
        OptionFleet(monkeypatch, {})
        served = serves(monkeypatch)
        run(["restart", str(tmp_path / "graphs")], monkeypatch)
        assert served[0].path == str((tmp_path / "graphs").resolve())

    def test_a_number_that_is_a_folder_names_the_folder_not_a_port(self, tmp_path: Path, monkeypatch: Any) -> None:
        (tmp_path / "8790").mkdir()
        monkeypatch.chdir(tmp_path)
        assert lifecycle._port_named("8790") is None


class TestAnOptionOfTheAppsOwn:
    def test_the_app_registers_its_own_serving_commands(self) -> None:
        """portail registered only what the app asked for; ``open`` and ``restart`` are the app's, with its option."""
        assert set(app._commands) == {"open", "restart", "stop", "list"}
        assert app._default_cmd is app._commands["open"]

    def test_the_option_reaches_the_workspace(self, tmp_path: Path, monkeypatch: Any) -> None:
        served = serves(monkeypatch)
        run([str(tmp_path), "--notes-dir", "/n"], monkeypatch)
        assert getattr(served[0], "options") == {"notes_dir": "/n"}

    def test_a_background_child_is_started_with_it(self, tmp_path: Path) -> None:
        argv = serving.background_command(FOLDER_SPEC, FolderWorkspace(str(tmp_path), notes_dir="/n"))
        assert argv[3:5] == ["open", str(tmp_path)]
        assert argv[argv.index("--notes-dir") + 1] == "/n"

    def test_a_workspace_without_options_adds_none(self, tmp_path: Path) -> None:
        argv = serving.background_command(FOLDER_SPEC, FolderWorkspace(str(tmp_path)))
        assert "--notes-dir" not in argv
        demo = serving.background_command(AppSpec(name="demoapp", cli_module="tests.demo_cli"), DemoWorkspace())
        assert demo[3:] == ["open", "--port", str(DemoWorkspace().port), "--browser", "never"]

    def test_a_restart_by_port_keeps_the_option_the_server_runs_with(self, tmp_path: Path, monkeypatch: Any) -> None:
        OptionFleet(monkeypatch, {P: running(tmp_path, notes_dir="/n")})
        served = serves(monkeypatch)
        run(["restart", str(P)], monkeypatch)
        assert getattr(served[0], "options") == {"notes_dir": "/n"}

    def test_a_restart_that_names_the_option_replaces_it(self, tmp_path: Path, monkeypatch: Any) -> None:
        OptionFleet(monkeypatch, {P: running(tmp_path, notes_dir="/n")})
        served = serves(monkeypatch)
        run(["restart", str(P), "--notes-dir", "/other"], monkeypatch)
        assert getattr(served[0], "options") == {"notes_dir": "/other"}


class TestAnAppsOwnWorkspaceRoute:
    def test_the_shell_leaves_the_route_to_an_app_that_answers_it(self, tmp_path: Path) -> None:
        own = Flask("own")

        @own.get("/api/workspace")
        def workspace() -> Any:
            return jsonify({"folder": str(tmp_path), "mine": True})

        server = serving.Server(FOLDER_SPEC, FolderWorkspace(str(tmp_path)))
        client = mount_shell(own, server).test_client()
        assert client.get("/api/workspace").get_json() == {"folder": str(tmp_path), "mine": True}
        assert client.get("/api/meta").get_json()["app"] == "folderapp"

    def test_the_bare_shell_still_answers_it(self, tmp_path: Path) -> None:
        from portail.web import create_shell

        server = serving.Server(FOLDER_SPEC, FolderWorkspace(str(tmp_path)))
        assert create_shell(server).test_client().get("/api/workspace").get_json() == server.served.info()
