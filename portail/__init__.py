"""portail — the framework a workspace web app is built on.

An app built on portail is ONE served object (its workspace) behind a Flask page, managed from the command line:

- :mod:`portail.spec` — what the app tells the framework: :class:`AppSpec` (its name, state folder, CLI module,
  identity), the :class:`Served` protocol its workspace implements, and the optional :class:`Hooks`;
- :mod:`portail.serving` — serving in the foreground or detached, stop / restart / list by the server's IDENTITY,
  never by a port alone, the browser opened on an answer;
- :mod:`portail.cli` — the lifecycle commands, added to the app's own liquifai CLI;
- :mod:`portail.web` — the page's shell: ``/api/meta``, ⏻ and ↻, and every open page told when its server goes;
- :mod:`portail.mcp` — the MCP server base: a client of the served page that asks which server to talk to;
- :mod:`portail.query` — the filter language a records list is searched with.
"""

from importlib.metadata import PackageNotFoundError, version

from portail.errors import PortailError
from portail.spec import AppSpec, Hooks, Served

try:
    __version__ = version("portail")
except PackageNotFoundError:  # pragma: no cover - running from a checkout that was never installed
    __version__ = "0.0.0.dev0"

__all__ = ["AppSpec", "Hooks", "PortailError", "Served", "__version__"]
