"""The page's shell: ``/api/meta``, ⏻ and ↻, and every open page told when its server goes.

- :func:`mount_shell` adds the shell's routes to an app's own Flask app; :func:`create_shell` is a Flask app holding
  only the shell (what a server without an app of its own serves);
- :mod:`portaide.web.pages` is the server's one word to its pages, ``leaving``;
- ``portaide/web/static/`` holds the page side, vanilla JavaScript with no build step: ``shell.js`` (the
  ``Portaide`` namespace), ``shell.css``, ``server-events.js`` (the shared worker) and a minimal ``index.html``.
"""

from portaide.web.pages import PageChannel, channel_of, mount_pages, tell_pages_leaving
from portaide.web.shell import STATIC, create_shell, mount_shell, server_of

__all__ = [
    "PageChannel",
    "STATIC",
    "channel_of",
    "create_shell",
    "mount_pages",
    "mount_shell",
    "server_of",
    "tell_pages_leaving",
]
