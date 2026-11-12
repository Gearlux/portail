"""The server's word to its open pages: ``leaving`` — this server is going, and why.

Before, a page could only ASK its server; nothing told it the server was gone, so ``<name> stop`` left every tab on
screen looking alive until a click failed. Each page now holds ``GET /api/events`` open — a server-sent
event stream (``text/event-stream``), which werkzeug's threaded server streams as it does any response, with no
WebSocket dependency — and on its way out the server sends ``leaving`` down every one (:func:`tell_pages_leaving`,
called by ``Server.serve``). ``why`` is ``exit`` unless ``<name> restart`` said ``restart`` first
(``POST /api/leaving``): the page then waits for the new server instead of closing.

What a stream carries::

    retry: 1000
    event: hello
    data: {"pid": 28992}

    : still here                     (every heartbeat_s while nothing happens)

    event: leaving
    data: {"why": "exit", "pid": 28992, "kept_in": "", "kept_until": ""}

The heartbeat is what finds a tab the browser closed: its write fails, werkzeug closes the generator and the page
leaves the channel. The stream's thread is a daemon (werkzeug's threaded server never joins one), so the streams
are still open when ``app.run`` has returned — which is when the server tells them.
"""

import json
import os
import queue
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Generator, List, Literal, Optional, Union, get_args

from loggair import get_logger

if TYPE_CHECKING:  # annotation only: the serving module imports this package
    from portaide.serving import Server

logger = get_logger(__name__)

#: Why the server is going: ``exit`` (every stop) or ``restart`` (the same workspace comes back on this port).
LeavingWhy = Literal["exit", "restart"]
_WHYS = get_args(LeavingWhy)

#: How soon a page's browser opens a dropped stream again (the ``retry:`` field, in ms).
RECONNECT_MS = 1000
#: How long a stop waits for each page to have been told — a page that does not read holds it no longer.
TELL_WAIT_S = 1.0
#: Where the channel lives on the Flask app.
_EXTENSION = "portaide.pages"

LeavingNote = Dict[str, Union[int, str]]


class _Page:
    """One open stream: what the server has to say to it (``None``: end without a word), and whether it was said."""

    def __init__(self) -> None:
        self.said: "queue.Queue[Optional[LeavingNote]]" = queue.Queue()
        self.told = threading.Event()


class PageChannel:
    """Every page open on this server, and the one thing the server ever tells them.

    Args:
        heartbeat_s: How often a quiet stream says it is still there.
    """

    def __init__(self, heartbeat_s: float = 15.0) -> None:
        self.heartbeat_s = heartbeat_s
        self._why: LeavingWhy = "exit"
        self._pages: List[_Page] = []
        self._lock = threading.Lock()

    @property
    def why(self) -> LeavingWhy:
        """Why this server will go — ``exit`` until a restart says ``restart``."""
        return self._why

    @why.setter
    def why(self, value: str) -> None:
        if value not in _WHYS:
            raise ValueError(f"why must be one of {', '.join(repr(w) for w in _WHYS)} — got {value!r}")
        self._why = value  # type: ignore[assignment]  # checked against the Literal's values just above

    @property
    def pages(self) -> int:
        """How many streams are open."""
        with self._lock:
            return len(self._pages)

    def stream(self) -> Generator[str, None, None]:
        """One page's event stream: the greeting, a heartbeat while nothing happens, then ``leaving`` and its end."""
        page = _Page()
        with self._lock:
            self._pages.append(page)
        try:
            yield f"retry: {RECONNECT_MS}\nevent: hello\ndata: {json.dumps({'pid': os.getpid()})}\n\n"
            while True:
                try:
                    note = page.said.get(timeout=self.heartbeat_s)
                except queue.Empty:
                    yield ": still here\n\n"
                    continue
                if note is None:
                    return
                yield f"event: leaving\ndata: {json.dumps(note)}\n\n"
                # Resumed only once werkzeug has written the chunk above: the page has been told.
                page.told.set()
                return
        finally:
            with self._lock:
                if page in self._pages:
                    self._pages.remove(page)

    def announce(self, note: LeavingNote, wait_s: float = TELL_WAIT_S) -> int:
        """Send ``leaving`` to every open page; answers how many were told within ``wait_s``."""
        with self._lock:
            pages = list(self._pages)
        for page in pages:
            page.said.put(note)
        deadline = time.monotonic() + wait_s
        return sum(page.told.wait(max(0.0, deadline - time.monotonic())) for page in pages)

    def end_silently(self) -> None:
        """End every stream without a word — what a page sees of a server that vanished (tests use it)."""
        with self._lock:
            pages = list(self._pages)
        for page in pages:
            page.said.put(None)


def mount_pages(app: Any) -> PageChannel:  # app: a Flask app
    """Give ``app`` its channel — ``mount_shell`` calls it once."""
    channel = PageChannel()
    app.extensions[_EXTENSION] = channel
    return channel


def channel_of(app: Any) -> PageChannel:  # app: a Flask app
    """The channel :func:`mount_pages` gave ``app``."""
    channel: PageChannel = app.extensions[_EXTENSION]
    return channel


def tell_pages_leaving(app: Any, server: "Server") -> int:  # app: a Flask app, or a stand-in without pages
    """Tell every page open on ``app`` that the server is going — on the server's own stop, before anything slow.

    Answers how many pages were told (0 when no channel is mounted on ``app``).
    """
    try:
        channel = channel_of(app)
    except (KeyError, AttributeError):
        return 0
    note: LeavingNote = {"why": channel.why, "pid": os.getpid(), **server.leaving_note()}
    told = channel.announce(note)
    logger.debug(f"told {told} of {channel.pages + told} open page(s) the server is going ({channel.why})")
    return told


__all__ = [
    "LeavingWhy",
    "PageChannel",
    "RECONNECT_MS",
    "channel_of",
    "mount_pages",
    "tell_pages_leaving",
]
