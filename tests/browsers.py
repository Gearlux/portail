"""Shared plumbing for the browser-driven tests: a workspace on a real socket, and a page on it.

The shell has no JS harness, so behaviour that lives in its JavaScript is proven by driving the real page with
Playwright over a served demo workspace. ``playwright`` is a ``dev`` dependency; the skip in ``chromium_page`` guards
only an environment that installed the package and not the browser.
"""

import contextlib
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from portail.serving import Server
from portail.spec import Served
from tests.demo_cli import DEMO_SPEC


@contextlib.contextmanager
def serving_app(workspace: Served, port: int = 0) -> Iterator[Tuple[Any, Any, Server]]:
    """The workspace on a real socket with its app and server — the browser needs HTTP, not a test client."""
    from werkzeug.serving import make_server

    from portail.web import create_shell

    server = Server(DEMO_SPEC, workspace)
    app = create_shell(server)
    socket_server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=socket_server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, socket_server, server
    finally:
        socket_server.shutdown()
        thread.join()


@contextlib.contextmanager
def served(workspace: Served) -> Iterator[str]:
    """The workspace on a real socket, its address."""
    with serving_app(workspace) as (_app, socket_server, _server):
        yield f"http://127.0.0.1:{socket_server.server_port}/"


#: What "ready" means, as the atoms the wait is built from AND the names a failure reports — one list.
_READY = [
    ("the shell (window.Portail)", "window.Portail"),
    ("the server's meta (Portail.meta.workspace)", "window.Portail && Portail.meta && Portail.meta.workspace"),
    ("the buttons (#ws-exit)", "document.getElementById('ws-exit')"),
]
_READY_EXPR = "() => " + " && ".join(f"({fragment})" for _, fragment in _READY)

#: ONE Playwright and ONE chromium per test file (``tests/conftest.py``): a browser per page cost 0.77 s of setup.
_PLAYWRIGHT: Any = None
_BROWSER: Any = None


def _shared_browser() -> Any:
    """The browser, started on first use and relaunched if it died (a dead shared one would fail every later test)."""
    global _PLAYWRIGHT, _BROWSER
    if _BROWSER is not None and not _BROWSER.is_connected():
        _BROWSER = None
    if _BROWSER is None:
        if _PLAYWRIGHT is None:
            sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
            _PLAYWRIGHT = sync_playwright().start()
        try:
            _BROWSER = _PLAYWRIGHT.chromium.launch()
        except Exception as exc:  # the package without its browser
            pytest.skip(f"no chromium for playwright: {exc}")
    return _BROWSER


def close_shared_browser() -> None:
    """Close the browser and stop Playwright. Never raises: a browser that already died must not turn a run red."""
    global _PLAYWRIGHT, _BROWSER
    for shut in (lambda: _BROWSER and _BROWSER.close(), lambda: _PLAYWRIGHT and _PLAYWRIGHT.stop()):
        try:
            shut()
        except Exception:  # noqa: BLE001 - nothing at session end is worth a failure
            pass
    _BROWSER = None
    _PLAYWRIGHT = None


def ready(page: Any, timeout: float = 30000) -> None:
    """Wait until the page's shell knows its server and shows its buttons — a failure names what was missing."""
    try:
        page.wait_for_function(_READY_EXPR, timeout=timeout)
    except Exception as exc:
        missing = [name for name, fragment in _READY if not page.evaluate(f"() => !!({fragment})")]
        raise AssertionError(
            f"the page at {page.url} was not ready after {timeout / 1000:g}s — still missing: "
            + (", ".join(missing) or "nothing (the wait itself failed)")
        ) from exc


@contextlib.contextmanager
def chromium_page(url: str, ready_timeout: float = 30000, viewport: Optional[Dict[str, int]] = None) -> Iterator[Any]:
    """A chromium page on ``url``, waited for until the shell is ready — in a CONTEXT of its own (cookies, storage
    and permissions live there), the browser shared.

    NOTE for anything that waits in these tests: Playwright does NOT await an ``async`` ``wait_for_function``
    predicate — the Promise it returns is truthy, so such a wait passes instantly. Wait on a response or on a
    synchronous predicate instead.
    """
    browser = _shared_browser()
    context = browser.new_context(viewport=viewport or {"width": 1200, "height": 800})
    try:
        page = context.new_page()
        logged: List[str] = []
        page.on("pageerror", lambda error: logged.append(f"pageerror: {error}"))
        page.goto(url)
        try:
            ready(page, ready_timeout)
        except AssertionError as exc:
            raise AssertionError(f"{exc}; the page logged: {' | '.join(logged) or 'nothing'}") from exc
        yield page
    finally:
        context.close()


def answer_confirm(page: Any, answer: bool) -> None:
    """``window.confirm`` answered by the test and recorded in ``window.asked``, instead of a native dialog: the page
    asks only after a fetch, and a native dialog opening while Playwright's click still waits on the page froze that
    wait half the time (measured)."""
    page.evaluate(
        "(answer) => { window.asked = []; window.confirm = (said) => { window.asked.push(said); return answer; }; }",
        answer,
    )
