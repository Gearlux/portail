"""portaide's exceptions: one root, each also the builtin it replaces."""

from liquifai.exceptions import LiquifaiError


class PortaideError(Exception):
    """The root of every exception portaide raises on purpose."""


class ServeError(PortaideError, LiquifaiError, OSError):
    """The workspace cannot be served, stopped or restarted as asked — one ``Error: …`` line, exit 1.

    A ``LiquifaiError`` so the CLI renders it through liquifai's failure contract (one line, the traceback only
    under ``--debug``); still an ``OSError`` for a Python caller.
    """


class PortInUseError(PortaideError, OSError):
    """The port the workspace asks for is held by another program."""
