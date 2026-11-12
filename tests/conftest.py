"""The suite's isolation: its own temp folder, log folder and state folder, no live HTTP, no real browser tab.

Every fixture here is autouse and touches only what the lifecycle reaches: a test of another module runs as before.
"""

import tempfile
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from portaide import serving
from tests import demo_cli


@pytest.fixture(autouse=True, scope="session")
def _own_temp_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """The whole suite mints its never-saved workspaces' folders under pytest's base temp, never the machine's.

    A never-saved demo workspace mints ``mkdtemp(prefix="demoapp-")``; nothing removes it when pytest exits. Measured
    with an app on the same seam: one full run left 56 of them in the operator's shared temp directory. Redirecting
    ``tempfile.tempdir`` lands them under pytest's base temp (which pytest reclaims, keeping the last three runs) — and
    the neutral directory a background child runs from with them. ``mktemp`` runs FIRST, so pytest fixes its own base
    temp while ``gettempdir()`` still answers the machine's.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("suite-temp")))
        yield


@pytest.fixture(autouse=True)
def _own_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every test logs under its own tmp dir — never the operator's shared ``$LOGGAIR_DIR``."""
    monkeypatch.setenv("LOGGAIR_DIR", str(tmp_path / "logs"))


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The per-user state folder (``~/.demoapp``: the registry, the last served workspace, the never-saved record) is
    under the test's own tmp dir — never the real one.

    ``Path.home`` is replaced rather than ``HOME``: the browser driver the page tests start reads ``HOME`` to find its
    browsers.
    """
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: folder))
    return folder


@pytest.fixture()
def state(home: Path) -> Path:
    """The demo app's state folder."""
    return demo_cli.DEMO_SPEC.state_dir()


@pytest.fixture(autouse=True)
def _nothing_handed_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """No environment variable a start hands its child leaks in from the machine or from another test."""
    for word in (serving.CONTINUE_FOLDER, serving.PREPARED):
        monkeypatch.delenv(demo_cli.DEMO_SPEC.env(word), raising=False)


@pytest.fixture(autouse=True)
def _no_live_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must NEVER reach a live server over HTTP — a server the operator runs above all.

    The lifecycle probes ports to tell "the app is already serving this" from "the port is taken" (``meta_at``), and
    a test reaching that probe would ask whatever answers there (measured with an app on the same seam: a 52 s suite
    stretching past 8 minutes on 60 s socket waits). Every request of the lifecycle is refused up front; a test that
    wants HTTP behaviour patches ``serving._urlopen`` itself (and then wins over this).
    """
    import urllib.error

    def refuse(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError("tests never talk to a live server")

    monkeypatch.setattr(serving, "_urlopen", refuse)


@pytest.fixture(autouse=True)
def _the_default_port_reads_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """The machine's own listener on the demo's DEFAULT port never decides a test.

    A port a test binds itself (``socket.bind(("127.0.0.1", 0))``) is still probed for real, and a test about a busy
    port patches the probe.
    """
    default = demo_cli.DEMO_PORT
    real = serving.port_in_use
    monkeypatch.setattr(serving, "port_in_use", lambda host, port: False if port == default else real(host, port))


@pytest.fixture(autouse=True)
def browser_opens(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """No test opens a tab in the operator's browser: the addresses a start would open are recorded instead. A test
    about a machine without a browser patches ``webbrowser.open`` itself."""
    import webbrowser

    opened: List[str] = []

    def record(url: str, *args: object, **kwargs: object) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    return opened


@pytest.fixture(autouse=True)
def prepared() -> Iterator[List[str]]:
    """What the demo's ``prepare`` hook ran on (workspace titles), this test only."""
    demo_cli.PREPARED.clear()
    yield demo_cli.PREPARED
    demo_cli.PREPARED.clear()


@pytest.fixture()
def cli_context() -> Iterator[None]:
    """A liquifai context of its own for each command-line run."""
    from liquifai.context import set_context

    set_context(None)
    yield
    set_context(None)


@pytest.fixture(autouse=True, scope="module")
def _one_browser_per_module() -> Iterator[None]:
    """Share one chromium within a test FILE, and stop Playwright at the end of it.

    Playwright's sync API keeps an asyncio event loop RUNNING in this thread while it is started, and
    ``asyncio.run()`` refuses to run inside one: held open for the session it broke every asyncio test after a browser
    test (measured). Closing at the module boundary keeps the sharing within a file and no loop running after it.
    """
    yield
    from tests.browsers import close_shared_browser

    close_shared_browser()


def run(argv: List[str], monkeypatch: Any) -> None:
    """Run the demo app's command line with ``argv`` (after the program name)."""
    import sys

    from liquifai.context import set_context

    set_context(None)
    monkeypatch.setattr(sys, "argv", ["demoapp", *argv])
    demo_cli.app.run()
