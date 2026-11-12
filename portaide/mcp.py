"""The MCP server base — an app's served workspace as AI-callable tools.

An app's MCP server (stdio, spawned by the MCP client) is a CLIENT of the app already serving a session: what the
person dropped, edited and is looking at lives only in that process, so the tools relay to its HTTP API rather than
rebuilding a workspace. portaide holds the parts that do not depend on what the workspace holds:

* :class:`LLMSafeMCPServer` — an ``MCPServer`` that advertises every tool schema in the subset LLM function-calling
  APIs accept, and tools ONLY; :func:`make_server` builds one for an :class:`~portaide.spec.AppSpec`;
* :class:`ServerChoice` — WHICH running server a call talks to: a tool's ``port`` argument names one, else the one
  this connection chose earlier is kept until it dies, else the client's user is ASKED — through the SDK's
  ``Resolve``/``Elicit`` resolvers, so the question rides whichever protocol the client speaks. A client that cannot
  ask is refused while several run; the only one running is used after a one-time "connect?" whose "always" answer
  is kept per client name in ``<state folder>/mcp-clients.json``;
* :meth:`ServerChoice.call` — the HTTP relay, every failure IN-BAND (``{"error": ...}``): a tool never raises, and
  with nothing running the error names how to start the app. The MCP process never starts the app itself.

The servers are found by :func:`portaide.serving.running_servers` (the registry plus the default port, each verified
LIVE by its identity), imported when a call needs it: :func:`_running_servers` and :func:`_request` are the test seams.

NOTE: no ``from __future__ import annotations`` — the SDK introspects live annotation objects to build the tool
argument schemas and the resolver plans.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Union

import mcp.types as mcp_types
from confluid import sanitize_schema
from loggair import get_logger
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, Field, create_model

from portaide.errors import PortaideError
from portaide.spec import AppSpec

logger = get_logger(__name__)

#: The answer that keeps a client connecting to the only server without asking — its text carries the client's
#: name, so the person sees WHO will stop asking.
ALWAYS = "Always connect automatically when only one runs ({client})"
#: The answer that connects once, keeping nothing.
THIS_TIME = "Connect this time"
#: In the app's state folder: each MCP client's kept answer, ``{"<client name>": {"auto_connect": true}}``. Delete
#: the file to be asked again.
PREFERENCES_FILE = "mcp-clients.json"
#: How long one relayed request may take, in seconds.
DEFAULT_TIMEOUT = 120.0

#: The question's two forms: which server (several run), connect or not (one runs).
_SERVER_FIELD = "server"
_CONNECT_FIELD = "connect"


class RelayError(PortaideError, RuntimeError):
    """A tool could not get its answer from a served workspace — reported in-band, never raised out of a tool."""


class LLMSafeMCPServer(MCPServer):
    """An ``MCPServer`` that advertises every tool schema in the subset LLM function-calling APIs accept
    (``confluid.sanitize_schema``), and advertises tools ONLY.

    A strict client (an OpenAPI-3.0 function-calling subset with no ``$ref``) can call every tool the moment one
    grows a structured argument. Only the ADVERTISED schema is rewritten; real calls are still validated against the
    original argument model.

    It also drops the request handlers a tools-only server never implements, so the capabilities it announces match
    reality: the SDK advertises resources and prompts unconditionally, which baits LLM clients into
    ``resources/list``, and some models read the empty answer as "server not connected".
    """

    _UNIMPLEMENTED_REQUESTS = (
        mcp_types.ListResourcesRequest,
        mcp_types.ListResourceTemplatesRequest,
        mcp_types.ReadResourceRequest,
        mcp_types.SubscribeRequest,
        mcp_types.UnsubscribeRequest,
        mcp_types.ListPromptsRequest,
        mcp_types.GetPromptRequest,
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The low-level registry is keyed by METHOD NAME ("resources/list"); derive it from each request model's own
        # `method` default so the tuple above is the one source.
        for request_type in self._UNIMPLEMENTED_REQUESTS:
            method = request_type.model_fields["method"].default
            self._lowlevel_server._request_handlers.pop(method, None)

    async def list_tools(self) -> List[MCPTool]:
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema = sanitize_schema(tool.input_schema)
            if tool.output_schema is not None:
                tool.output_schema = sanitize_schema(tool.output_schema)
        return tools


def make_server(spec: AppSpec, instructions: str) -> LLMSafeMCPServer:
    """The app's MCP server, named after it, carrying the ``instructions`` the client shows its model."""
    return LLMSafeMCPServer(spec.name, instructions=instructions)


# ---------------------------------------------------------------------------- the seams


def _running_servers(spec: AppSpec) -> List[Dict[str, Any]]:
    """Every server of ``spec``'s app running, lowest port first — :func:`portaide.serving.running_servers`.

    Imported here, at call time: the MCP base needs serving only when a tool is called.
    """
    from portaide.serving import running_servers

    return running_servers(spec)


def _request(
    server: Dict[str, Any],
    method: str,
    path: str,
    body: Optional[Dict[str, Any]],
    *,
    app: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """One JSON request to ``server``; a JSON error answer becomes a :class:`RelayError` worded by the server.

    A JSON answer that is not an object comes back as ``{"value": <answer>}``.
    """
    url = f"http://{server['host']}:{server['port']}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:  # noqa: BLE001 - a non-JSON error body: the status is the message
            detail = None
        raise RelayError(str(detail or f"{exc.code} {exc.reason}")) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RelayError(f"{app} at {server['host']}:{server['port']} did not answer: {exc}") from None
    if not isinstance(payload, dict):
        return {"value": payload}
    return payload


def query_string(**params: Any) -> str:
    """``params`` as a URL query, leaving out every ``None`` and ``""`` — the arguments a tool was not given."""
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None and v != ""})


# ---------------------------------------------------------------------------- which server


@dataclass
class Target:
    """What the resolvers settled for one tool call: the server to ask, or the in-band refusal."""

    server: Optional[Dict[str, Any]] = None
    error: str = ""


def _line(server: Dict[str, Any]) -> str:
    """A server as the app's ``list`` names it: its port, then its workspace."""
    return f"{server['port']} {server['label']}"


def _listing(servers: List[Dict[str, Any]]) -> str:
    return ", ".join(_line(server) for server in servers)


def _answer(server: Dict[str, Any]) -> str:
    """A server as the question offers it."""
    return f"{server['port']}  {server['label']}"


def _same(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """One server, by where it answers AND what it serves: a port restarted with another workspace is another
    server — the one chosen has died."""
    return (a["host"], a["port"], a.get("identity")) == (b["host"], b["port"], b.get("identity"))


def _client_name(ctx: Context) -> str:
    """The name the client gave when it connected (``claude-code``) — blank when it gave none."""
    params = ctx.session.client_params
    return params.client_info.name if params is not None and params.client_info is not None else ""


def _can_ask(ctx: Context) -> bool:
    """Whether the client declared it can put a form question to its user (MCP elicitation)."""
    capabilities = ctx.session.client_capabilities
    return capabilities is not None and capabilities.elicitation is not None


def _choice_model(field: str, title: str, answers: List[str]) -> type[BaseModel]:
    """The one-field form a question asks — its answers a closed set, as the spec requires.

    Always rendered as an ``enum``: pydantic renders a one-answer ``Literal`` as ``const``, which a client shows as a
    fixed value rather than a choice to make.
    """

    def as_enum(schema: Dict[str, Any]) -> None:
        schema.pop("const", None)
        schema["enum"] = list(answers)

    choices: Any = Literal[tuple(answers)]  # a Literal built from the live answers
    fields: Dict[str, Any] = {field: (choices, Field(title=title, json_schema_extra=as_enum))}
    return create_model("ServerChoiceAnswer", **fields)


class ServerChoice:
    """Which running server of one app the tools talk to — one per MCP process, so per client connection.

    A host's tool takes the settled :class:`Target` as its first parameter, annotated so the resolvers fill it
    (never advertised as an argument), and a ``port: int = 0`` argument the agent names a server with::

        choice = ServerChoice(SPEC, start_hint="start one with `demoapp open <workspace.yaml>`")

        @server.tool()
        def page_state(target: Annotated[Target, choice.resolve], port: int = 0) -> Dict[str, Any]:
            return choice.call(target, "GET", "/api/state")

    In order, a call talks to: none running → no server, the error names :attr:`start_hint`; ``port`` named → that
    one (now this connection's), or the running ones named when nothing answers there; the one chosen earlier, while
    it still runs (same host, port AND identity); several → the client's user is asked which (a client that cannot
    ask is refused, naming them); one → used, once the user agreed — asked unless this client was told to always
    connect, or cannot ask.

    Args:
        spec: The app — its name words every message, its state folder keeps the clients' answers.
        start_hint: How the person starts the app, for the error a call gets while none runs.
        timeout: How long one relayed request may take, in seconds.
    """

    def __init__(self, spec: AppSpec, start_hint: str = "", timeout: float = DEFAULT_TIMEOUT) -> None:
        self.spec = spec
        self.start_hint = start_hint or f"start one — `{spec.name} --help` says how"
        self.timeout = timeout
        #: The server this connection talks to, once named or chosen. The MCP process is spawned once per client
        #: connection (stdio), so this IS the connection's state: a reconnect starts with no choice.
        self.chosen: Optional[Dict[str, Any]] = None
        question = self._make_question()
        #: The ``Resolve`` marker of a tool's target parameter: ``Annotated[Target, choice.resolve]``.
        self.resolve = Resolve(self._make_target(question))
        #: The same parameter type, ready made: ``def tool(target: choice.target, port: int = 0)`` (an alias mypy
        #: cannot read as a type — a type-checked host writes ``Annotated[Target, choice.resolve]``).
        self.target: Any = Annotated[Target, self.resolve]

    # ------------------------------------------------------------------ messages

    @property
    def not_running(self) -> str:
        """The error every call gets while none of the app's servers runs."""
        return f"no {self.spec.name} is running — {self.start_hint}"

    def preferences_path(self) -> Path:
        """The file each client's "always" answer is kept in, in the app's state folder (read when asked)."""
        return self.spec.state_dir() / PREFERENCES_FILE

    # ------------------------------------------------------------------ the kept answers

    def _preferences(self) -> Dict[str, Any]:
        """Every client's kept answer — none when the file is missing or unreadable."""
        path = self.preferences_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            logger.debug(f"ignoring unreadable {path}: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def _auto_connects(self, client: str) -> bool:
        entry = self._preferences().get(client) if client else None
        return isinstance(entry, dict) and entry.get("auto_connect") is True

    def _keep_auto_connect(self, client: str) -> None:
        """Keep ``client``'s "always" answer beside every other client's — a folder that cannot be written loses the
        answer, never the connection."""
        preferences = self._preferences()
        preferences[client] = {"auto_connect": True}
        path = self.preferences_path()
        try:
            if not path.parent.is_dir():
                path.parent.mkdir(parents=True)
            path.write_text(json.dumps(preferences, indent=1, sort_keys=True) + "\n")
        except OSError as exc:
            logger.debug(f"could not keep {client}'s auto-connect answer in {path}: {exc}")

    def _choose(self, server: Dict[str, Any]) -> Target:
        self.chosen = server
        return Target(server)

    # ------------------------------------------------------------------ the resolvers

    def _make_question(self) -> Callable[..., Union[Target, Elicit[Any]]]:
        """The first resolver: the server this call talks to, or the question that decides it."""
        name = self.spec.name

        def question(ctx: Context, port: int = 0) -> Union[Target, Elicit[Any]]:
            servers = _running_servers(self.spec)
            if not servers:
                return Target(error=self.not_running)
            if port:
                named = [server for server in servers if int(server["port"]) == port]
                if not named:
                    return Target(error=f"no {name} answers on port {port} — running: {_listing(servers)}")
                return self._choose(named[0])
            if self.chosen is not None:
                alive = [server for server in servers if _same(server, self.chosen)]
                if alive:
                    return Target(alive[0])
            client = _client_name(ctx)
            if len(servers) > 1:
                if not _can_ask(ctx):
                    return Target(error=f"several {name} servers are running — pass port: {_listing(servers)}")
                return Elicit(
                    f"Several {name} servers are running — which one should the tools use?",
                    _choice_model(_SERVER_FIELD, "Server", [_answer(server) for server in servers]),
                )
            if not _can_ask(ctx) or self._auto_connects(client):
                return self._choose(servers[0])
            answers = ([ALWAYS.format(client=client)] if client else []) + [THIS_TIME]
            return Elicit(
                f"One {name} is running: {_line(servers[0])}. Connect?",
                _choice_model(_CONNECT_FIELD, "Connect", answers),
            )

        return question

    def _make_target(self, question: Callable[..., Any]) -> Callable[..., Target]:
        """The second resolver: the user's answer to the question turned into the server — or the refusal.

        The servers are read again: the answer names a server by its port and workspace as the question listed them,
        and one that stopped while the question was open is not connected to.
        """
        name = self.spec.name

        def target(asked: Annotated[ElicitationResult[Any], Resolve(question)], ctx: Context) -> Target:
            if isinstance(asked, AcceptedElicitation) and isinstance(asked.data, Target):
                return asked.data
            servers = _running_servers(self.spec)
            if not servers:
                return Target(error=self.not_running)
            if not isinstance(asked, AcceptedElicitation):
                return Target(error=f"no {name} chosen — pass port: {_listing(servers)}")
            answer = asked.data.model_dump()
            if _CONNECT_FIELD in answer:
                if answer[_CONNECT_FIELD] != THIS_TIME:
                    self._keep_auto_connect(_client_name(ctx))
                picked = servers[:1] if len(servers) == 1 else []
            else:
                picked = [server for server in servers if _answer(server) == answer[_SERVER_FIELD]]
            if not picked:
                return Target(error=f"the {name} picked stopped — pass port: {_listing(servers)}")
            return self._choose(picked[0])

        return target

    # ------------------------------------------------------------------ the relay

    def server(self, target: Target) -> Dict[str, Any]:
        """The server the resolvers settled on, or their refusal raised as a :class:`RelayError`."""
        if target.server is None:
            raise RelayError(target.error)
        return target.server

    def call(self, target: Target, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One request to the settled server, every failure IN-BAND as ``{"error": ...}`` — the contract each tool
        keeps. ``path`` is the server's route with its query (:func:`query_string`); ``body`` is sent as JSON."""
        try:
            return _request(self.server(target), method, path, body, app=self.spec.name, timeout=self.timeout)
        except RelayError as exc:
            return {"error": str(exc)}


__all__ = [
    "ALWAYS",
    "DEFAULT_TIMEOUT",
    "LLMSafeMCPServer",
    "PREFERENCES_FILE",
    "RelayError",
    "ServerChoice",
    "THIS_TIME",
    "Target",
    "make_server",
    "query_string",
]
