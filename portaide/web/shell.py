"""The page's shell: the routes every app's page needs to know its server, stop it, restart it and hear it go.

``mount_shell(app, server)`` adds to an app's own Flask app:

* ``GET /api/meta`` — ``{"app": spec.name, "title", "workspace": served.info(), "pid"}``: the IDENTITY every
  discovery reads (a server is the app's when ``app`` is its name), and the process id a restarting page waits to
  change;
* ``GET /api/workspace`` — ``served.info()``, read fresh by the page before it asks about unsaved changes;
* ``POST /api/exit`` / ``POST /api/restart`` — the page's ⏻ and ↻: the answer goes out FIRST, then the server stops
  itself with the signal ``<name> stop`` sends, or launches ``<name> restart <port> --background``;
* ``GET /api/events`` — the server-sent event stream every page listens to (``portaide.web.pages``);
* ``POST /api/leaving`` — ``{"why": "restart"}``, what ``<name> restart`` says before its stop;
* ``GET /portaide/<file>`` — the shell's own scripts and styles (``shell.js``, ``shell.css``, ``server-events.js``),
  under a path no app's ``/static`` collides with;
* ``GET /`` — a minimal page, only when the app routes nothing there yet (a bare app and the tests have a page).

Every address the page uses is RELATIVE (``api/meta``, ``portaide/shell.js``), so a path proxy serving the app under a
prefix keeps working.
"""

from pathlib import Path
from typing import Any, Callable, Optional

from flask import Flask, Response, jsonify, request, send_from_directory

from portaide.errors import ServeError
from portaide.serving import Server
from portaide.web.pages import channel_of, mount_pages

#: The shell's static files, shipped in the package.
STATIC = Path(__file__).resolve().parent / "static"

#: Where the Flask app keeps its :class:`~portaide.serving.Server`.
_EXTENSION = "portaide.server"


def server_of(app: Flask) -> Server:
    """The :class:`~portaide.serving.Server` :func:`mount_shell` mounted on ``app``."""
    server: Server = app.extensions[_EXTENSION]
    return server


def _refused(exc: Exception) -> Any:
    return jsonify({"error": str(exc)}), 400


def _first_then(answer: Callable[[], Any], then: Callable[[], None]) -> Any:
    """The answer goes out FIRST; ``then`` runs once it has been sent (werkzeug closes the response after writing it),
    so the page always learns what happens rather than seeing its request cut off. A refusal is a 400 naming it."""
    try:
        response = jsonify(answer())
    except ServeError as exc:
        return _refused(exc)
    response.call_on_close(then)
    return response


def mount_shell(app: Flask, server: Server, *, index: Optional[bool] = None) -> Flask:
    """Add the shell's routes to ``app`` for ``server`` and answer ``app``.

    Args:
        app: The app's own Flask app — its page and API stay its own.
        server: What the routes stop, restart and describe.
        index: Serve the shell's minimal page at ``/``. Unset → only when ``app`` routes nothing at ``/`` yet (mount
            the shell AFTER the app's own routes for that check to see them).
    """
    mount_pages(app)
    app.extensions[_EXTENSION] = server

    @app.get("/api/meta")
    def portaide_meta() -> Any:
        return jsonify(server.meta())

    @app.get("/api/workspace")
    def portaide_workspace() -> Any:
        return jsonify(server.served.info())

    @app.post("/api/exit")
    def portaide_exit() -> Any:
        return _first_then(server.exiting, server.stop_serving)

    @app.post("/api/restart")
    def portaide_restart() -> Any:
        return _first_then(server.restarting, server.restart_serving)

    @app.get("/api/events")
    def portaide_events() -> Any:
        # One stream per browser (the page's shared worker), held open until the server goes.
        return Response(
            channel_of(app).stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/leaving")
    def portaide_leaving() -> Any:
        body = request.get_json(silent=True) or {}
        try:
            channel_of(app).why = str(body.get("why", ""))
        except ValueError as exc:
            return _refused(exc)
        return jsonify({"why": channel_of(app).why})

    @app.get("/portaide/<path:name>")
    def portaide_static(name: str) -> Any:
        response = send_from_directory(str(STATIC), name)
        response.headers["Cache-Control"] = "no-cache"  # a restarted server's code reaches the next reload
        return response

    if index is None:
        index = not any(rule.rule == "/" for rule in app.url_map.iter_rules())
    if index:

        @app.get("/")
        def portaide_index() -> Any:
            return send_from_directory(str(STATIC), "index.html")

    return app


def create_shell(server: Server) -> Flask:
    """A Flask app holding only the shell (and its minimal page) for ``server`` — what a :class:`Server` without an app
    of its own serves."""
    app = Flask(server.spec.name, static_folder=None)
    return mount_shell(app, server)


__all__ = ["STATIC", "create_shell", "mount_shell", "server_of"]
