"""portaide — the framework a workspace web app is built on.

An app built on portaide is ONE served object (its workspace) behind a Flask page, managed from the command line:

- :mod:`portaide.spec` — what the app tells the framework: :class:`AppSpec` (its name, state folder, CLI module,
  identity), the :class:`Served` protocol its workspace implements, and the optional :class:`Hooks`;
- :mod:`portaide.serving` — serving in the foreground or detached, stop / restart / list by the server's IDENTITY,
  never by a port alone, the browser opened on an answer;
- :mod:`portaide.cli` — the lifecycle commands, added to the app's own liquifai CLI;
- :mod:`portaide.web` — the page's shell: ``/api/meta``, ⏻ and ↻, and every open page told when its server goes;
- :mod:`portaide.mcp` — the MCP server base: a client of the served page that asks which server to talk to;
- :mod:`portaide.query` — the filter language a records list is searched with.
"""

from importlib.metadata import PackageNotFoundError, version

from portaide.errors import PortaideError
from portaide.spec import AppSpec, Hooks, Served

try:
    __version__ = version("portaide")
except PackageNotFoundError:  # pragma: no cover - running from a checkout that was never installed
    __version__ = "0.0.0.dev0"

__all__ = ["AppSpec", "Hooks", "PortaideError", "Served", "__version__"]
