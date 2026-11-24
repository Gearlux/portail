"""portail's exceptions: one root, each also the builtin it replaces."""

from liquifai.exceptions import LiquifaiError


class PortailError(Exception):
    """The root of every exception portail raises on purpose."""


class ServeError(PortailError, LiquifaiError, OSError):
    """The workspace cannot be served, stopped or restarted as asked — one ``Error: …`` line, exit 1.

    A ``LiquifaiError`` so the CLI renders it through liquifai's failure contract (one line, the traceback only
    under ``--debug``); still an ``OSError`` for a Python caller.
    """


class PortInUseError(PortailError, OSError):
    """The port the workspace asks for is held by another program."""
