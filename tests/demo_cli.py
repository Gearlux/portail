"""The demo app every test builds on: a tiny workspace app on portail, and its command line.

``python -m tests.demo_cli`` is a real app — ``demoapp [file]``, ``demoapp workspace <type>``, ``stop``, ``restart``,
``list`` — whose workspace is a few settings in a YAML file (``workspace: {port: 9001, title: mine}``). Its hooks turn
every lifecycle feature ON: a never-saved workspace keeps a document in its own folder on its way out (``leaving``),
holds work when its folder holds that document or ``work.txt`` (``holds_work``), and is continued from it by a restart
(``continued``). ``prepare`` is recorded in :data:`PREPARED`.
"""

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from liquifai import LiquifyApp
from liquifai.exceptions import LiquifaiError

from portail import AppSpec, Hooks, Served
from portail.cli import add_lifecycle_commands

#: The demo's default port — far from any port a person's own servers use in this workspace.
DEMO_PORT = 8710
TYPES = ("classification", "detection")
#: A never-saved workspace's own document, written by its stop when it holds changes.
DOCUMENT = "document.yaml"
#: What else counts as work in a never-saved workspace's folder.
WORK_FILE = "work.txt"
KEPT = "until the demo forgets it"
#: What a never-saved workspace leaves in the folder it works out of from its first moment (a lock, say): a folder a
#: workspace still works out of is never empty.
HELD = ".held"
#: Every ``prepare`` the hooks ran, by workspace title — the suite clears it per test.
PREPARED: List[str] = []


class DemoFileError(LiquifaiError, ValueError):
    """The named file is not a demo workspace — one ``Error: …`` line."""


class DemoWorkspace:
    """A workspace of a few settings, implementing :class:`~portail.spec.Served`."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEMO_PORT,
        title: str = "workspace",
        text_size: int = 12,
        path: str = "",
        template: str = "",
        folder: Optional[Path] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.title = title
        self.text_size = text_size
        self.path = path
        self.template = template
        self.dirty = False
        self._folder = folder

    def state_dir(self) -> Path:
        if self.path:
            return Path(self.path + ".d")
        if self._folder is None:
            self.work_out_of(Path(tempfile.mkdtemp(prefix="demoapp-")))
        assert self._folder is not None
        return self._folder

    def work_out_of(self, folder: Path) -> None:
        """Work out of ``folder`` from now on — marked as held, as a workspace's own folder always is."""
        self._folder = folder
        if folder.is_dir():
            (folder / HELD).touch()

    def info(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "template": self.template,
            "state_dir": str(self.state_dir()),
            "dirty": self.dirty,
            "text_size": self.text_size,
        }

    def settings(self) -> Dict[str, Any]:
        return {"port": self.port, "host": self.host, "title": self.title, "text_size": self.text_size}

    def save(self, path: str) -> None:
        target = Path(path).resolve()
        target.write_text(yaml.safe_dump({"workspace": self.settings()}))
        self.path, self.dirty = str(target), False

    def keep(self) -> Optional[Path]:
        """A never-saved workspace with changes: its document in its folder (what a restart continues)."""
        if self.path or not self.dirty:
            return None
        document = self.state_dir() / DOCUMENT
        document.write_text(yaml.safe_dump({"workspace": {**self.settings(), "template": self.template}}))
        return document


def _settings(path: Path) -> Dict[str, Any]:
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, dict) or not isinstance(document.get("workspace"), dict):
        raise DemoFileError(f"{path}: a workspace file roots at a 'workspace:' key")
    return dict(document["workspace"])


def open_file(path: str) -> DemoWorkspace:
    fields = _settings(Path(path))
    fields.pop("template", None)
    return DemoWorkspace(**fields, path=path)


def open_type(name: str) -> DemoWorkspace:
    return DemoWorkspace(title=name, template=name)


def holds_work(folder: Path) -> bool:
    return (folder / DOCUMENT).is_file() or (folder / WORK_FILE).is_file()


def continued(folder: Path, template: str) -> DemoWorkspace:
    document = folder / DOCUMENT
    if document.is_file():
        fields = _settings(document)
        fields.pop("template", None)
        workspace = DemoWorkspace(**fields, template=template)
    else:
        workspace = open_type(template) if template else DemoWorkspace()
    workspace.work_out_of(folder)
    workspace.dirty = True  # its work was never written to a workspace file
    return workspace


def _keep(served: Served) -> None:
    if isinstance(served, DemoWorkspace):
        served.keep()


DEMO_HOOKS = Hooks(
    prepare=lambda served: PREPARED.append(served.title),
    leaving=_keep,
    holds_work=holds_work,
    kept_until=lambda: KEPT,
    continued=continued,
)
DEMO_SPEC = AppSpec(name="demoapp", cli_module="tests.demo_cli", default_port=DEMO_PORT, hooks=DEMO_HOOKS)

app = LiquifyApp(name="demoapp", description="The demo workspace app.", strict_flags=True)
lifecycle = add_lifecycle_commands(
    app, DEMO_SPEC, open_file=open_file, open_empty=DemoWorkspace, open_type=open_type, type_names=lambda: list(TYPES)
)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
