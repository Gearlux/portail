"""A second demo app: its workspace is a FOLDER, and its serving commands take an option of the app's own.

``folderapp [folder] [--notes-dir DIR]`` serves a folder of files (default: the current directory) — there is no
workspace file, the folder IS the workspace. ``--notes-dir`` is the app's own option: portail carries it into a
``--background`` child and into a restart, so the server that comes back is the one that was asked for.

The app registers ``open`` and ``restart`` itself (they carry its option) and takes ``stop`` and ``list`` from portail.
"""

from pathlib import Path
from typing import Any, Dict

from liquifai import LiquifyApp

from portail import AppSpec
from portail.cli import add_lifecycle_commands

#: The demo's default port — far from any port a person's own servers use in this workspace.
FOLDER_PORT = 8720


class FolderWorkspace:
    """A folder served as a workspace, implementing :class:`~portail.spec.Served`."""

    def __init__(self, path: str, notes_dir: str = "") -> None:
        self.host = "127.0.0.1"
        self.port = FOLDER_PORT
        self.path = path
        self.template = ""
        self.title = Path(path).name
        self.options: Dict[str, str] = {"notes_dir": notes_dir} if notes_dir else {}

    def state_dir(self) -> Path:
        return Path(self.path)

    def info(self) -> Dict[str, Any]:
        return {"path": self.path, "template": "", "state_dir": self.path, "options": dict(self.options)}


def open_folder(path: str, notes_dir: str = "") -> FolderWorkspace:
    return FolderWorkspace(path, notes_dir=notes_dir)


def open_here() -> FolderWorkspace:
    return FolderWorkspace(str(Path.cwd().resolve()))


FOLDER_SPEC = AppSpec(
    name="folderapp", cli_module="tests.demo_folder_cli", default_port=FOLDER_PORT, workspace="folder"
)

app = LiquifyApp(name="folderapp", description="The demo folder app.", strict_flags=True)
lifecycle = add_lifecycle_commands(
    app, FOLDER_SPEC, open_file=open_folder, open_empty=open_here, commands=("stop", "list")
)


@app.command(name="open", default=True, positionals=["folder"], short={"b": "background"})
def open_(folder: str = "", notes_dir: str = "", port: int = 0, background: bool = False) -> None:
    """Serve a folder (the default command)."""
    options = {"notes_dir": notes_dir} if notes_dir else {}
    lifecycle.open(folder, port=port, background=background, browser="never", options=options)


@app.command(name="restart", positionals=["target"])
def restart(target: str = "", notes_dir: str = "", port: int = 0) -> None:
    """Stop the server and serve its folder again."""
    options = {"notes_dir": notes_dir} if notes_dir else {}
    lifecycle.restart(target, port=port, options=options)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
