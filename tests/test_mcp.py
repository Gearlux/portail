"""The MCP server base (``portaide.mcp``): an app's running workspace as AI-callable tools.

A tiny demo app builds its server on the base — :func:`make_server`, a :class:`ServerChoice`, and tools that relay to
its page — and the tests call those tools through the SDK's in-process client, so the "which server?" question
travels as it does for a real client. Server discovery (``_running_servers``) and the HTTP relay (``_request``) are
faked, as the base's seams; nothing is served.

WHICH server: a ``port`` argument names it; otherwise the one this connection chose, kept until it dies; several
running → the client's user is asked (a client that cannot ask is refused, naming the ports); one running → it is
used, after a one-time "connect?" whose "always" answer is kept per client in ``<state folder>/mcp-clients.json``.
"""

import asyncio
import io
import json
import sys
import types
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Annotated, Any, Dict, Iterator, List, Optional

import pytest
from mcp.client import Client
from mcp.server.lowlevel.server import NotificationOptions
from mcp.types import ElicitRequestParams, ElicitResult, Implementation
from pydantic import BaseModel

import portaide.mcp as base
from portaide.mcp import LLMSafeMCPServer, RelayError, ServerChoice, Target, make_server, query_string
from portaide.spec import AppSpec

# Keywords the Gemini / OpenAPI-3.0 function-calling subset rejects.
_FORBIDDEN = ('"$ref"', '"$defs"', '"definitions"', '"const"', '"additionalProperties"')

START_HINT = "start one with `demoapp open <workspace.yaml>`"
INSTRUCTIONS = "Tools over the demoapp workspace being served."
TOOLS = ("page_state", "find_records")

FIRST = {"host": "127.0.0.1", "port": 8787, "label": "the plain type workspace", "identity": "type:plain"}
SECOND = {"host": "127.0.0.1", "port": 8788, "label": "/tmp/second.workspace.yaml", "identity": "/tmp/second.yaml"}
THIRD = {"host": "127.0.0.1", "port": 8789, "label": "/tmp/third.workspace.yaml", "identity": "/tmp/third.yaml"}

ALWAYS_HERE = "Always connect automatically when only one runs (claude-code)"


class Window(BaseModel):
    """A structured argument, so the schema sanitising has a ``$ref`` to remove."""

    x0: float = 0.0
    x1: float = 1.0


class DemoApp:
    """The demo app's MCP server: a fresh one is a fresh MCP process — no server chosen yet."""

    def __init__(self, spec: AppSpec) -> None:
        self.spec = spec
        self.server = make_server(spec, INSTRUCTIONS)
        self.choice = ServerChoice(spec, start_hint=START_HINT)
        choice = self.choice

        @self.server.tool()
        def page_state(target: Annotated[Target, choice.resolve], port: int = 0) -> Dict[str, Any]:
            """What the page shows.

            Args:
                port: The server's port; 0 = the one this connection uses.
            """
            return choice.call(target, "GET", "/api/state")

        @self.server.tool()
        def find_records(
            target: Annotated[Target, choice.resolve], where: str = "", window: Optional[Window] = None, port: int = 0
        ) -> Dict[str, Any]:
            """Records matching a filter.

            Args:
                where: The filter.
                window: Where to look.
                port: The server's port; 0 = the one this connection uses.
            """
            body = window.model_dump() if window is not None else None
            return choice.call(target, "POST", f"/api/records?{query_string(where=where)}", body)

    def tools(self) -> List[Any]:
        return asyncio.run(self.server.list_tools())


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def app(state: Path) -> DemoApp:
    return DemoApp(AppSpec(name="demoapp", cli_module="demoapp.cli", state_folder=state))


class _User:
    """The person at the client's question: answers each with the next of ``answers`` (``None`` dismisses it) and
    keeps every question asked."""

    def __init__(self, *answers: Optional[str]) -> None:
        self.answers = list(answers)
        self.asked: List[ElicitRequestParams] = []

    async def __call__(self, context: Any, params: ElicitRequestParams) -> ElicitResult:
        self.asked.append(params)
        answer = self.answers.pop(0)
        if answer is None:
            return ElicitResult(action="decline")
        field = next(iter(params.requested_schema["properties"]))  # type: ignore[union-attr]
        return ElicitResult(action="accept", content={field: answer})

    def choices(self, index: int = -1) -> List[str]:
        schema = self.asked[index].requested_schema  # type: ignore[union-attr]
        return list(next(iter(schema["properties"].values()))["enum"])


def _call(
    app: DemoApp,
    name: str = "page_state",
    args: Optional[Dict[str, Any]] = None,
    *,
    user: Optional[_User] = None,
    client: str = "claude-code",
    mode: str = "legacy",
) -> Dict[str, Any]:
    """One tool call over the SDK's in-process client — ``user`` None = a client that cannot ask."""

    async def go() -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"elicitation_callback": user} if user is not None else {}
        info = Implementation(name=client, version="0")
        connect = Client(app.server, client_info=info, mode=mode, **kwargs)  # type: ignore[arg-type]
        async with connect as connection:
            result = await connection.call_tool(name, args or {})
        assert not result.is_error, result.content
        return dict(json.loads(result.content[0].text))  # type: ignore[union-attr]

    return asyncio.run(go())


def _answering(monkeypatch: pytest.MonkeyPatch, running: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Serve ``running`` (a live list the test may change) and record each request made."""
    hit: List[Dict[str, Any]] = []

    def request(
        server: Dict[str, Any], method: str, path: str, body: Optional[Dict[str, Any]], **options: Any
    ) -> Dict[str, Any]:
        hit.append({"port": int(server["port"]), "method": method, "path": path, "body": body, **options})
        return {"asked": server["port"]}

    monkeypatch.setattr(base, "_running_servers", lambda spec: list(running))
    monkeypatch.setattr(base, "_request", request)
    return hit


def _ports(hit: List[Dict[str, Any]]) -> List[int]:
    return [one["port"] for one in hit]


class TestTheServerShape:
    def test_the_server_uses_the_sanitizing_subclass_named_after_the_app(self, app: DemoApp) -> None:
        assert isinstance(app.server, LLMSafeMCPServer)
        assert app.server.name == "demoapp"
        assert app.server.instructions == INSTRUCTIONS

    def test_advertises_tools_only_no_phantom_resources_or_prompts(self, app: DemoApp) -> None:
        caps = app.server._lowlevel_server.get_capabilities(NotificationOptions(), {})
        assert caps.tools is not None
        assert caps.resources is None
        assert caps.prompts is None

    def test_the_tool_set_is_the_demo_apps(self, app: DemoApp) -> None:
        assert tuple(sorted(t.name for t in app.tools())) == tuple(sorted(TOOLS))

    def test_real_tool_schemas_are_clean(self, app: DemoApp) -> None:
        """The structured ``window`` argument would advertise a ``$ref``; the sanitised schema inlines it."""
        for tool in app.tools():
            blob = json.dumps(tool.input_schema)
            assert not any(tok in blob for tok in _FORBIDDEN), tool.name
            if tool.output_schema is not None:
                assert not any(tok in json.dumps(tool.output_schema) for tok in _FORBIDDEN), tool.name

    def test_every_tool_takes_a_port_and_nothing_of_the_choice(self, app: DemoApp) -> None:
        """``port`` is the agent's way to name the server; the chosen server itself is filled by the resolvers,
        never advertised as an argument."""
        for tool in app.tools():
            properties = tool.input_schema["properties"]
            assert properties["port"] == {"default": 0, "title": "Port", "type": "integer"}, tool.name
            assert "target" not in properties, tool.name

    def test_the_ready_made_target_alias_is_the_same_parameter(self, app: DemoApp) -> None:
        assert app.choice.target == Annotated[Target, app.choice.resolve]

    def test_the_relay_sends_the_method_path_body_app_name_and_timeout(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hit = _answering(monkeypatch, [FIRST])
        _call(app, "find_records", {"where": "class = 7", "window": {"x0": 0.5, "x1": 2.0}})
        assert hit == [
            {
                "port": 8787,
                "method": "POST",
                "path": "/api/records?where=class+%3D+7",
                "body": {"x0": 0.5, "x1": 2.0},
                "app": "demoapp",
                "timeout": base.DEFAULT_TIMEOUT,
            }
        ]

    def test_the_sdk_floor_is_declared_as_the_mcp_extra(self) -> None:
        import tomllib

        data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
        assert any(dep.startswith("mcp>=2") for dep in data["project"]["optional-dependencies"]["mcp"])


class TestWhenNoServerIsRunning:
    @pytest.mark.parametrize("name", TOOLS)
    def test_every_tool_answers_in_band_naming_the_start_command(
        self, name: str, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(base, "_running_servers", lambda spec: [])
        out = _call(app, name, user=_User())
        assert out == {"error": f"no demoapp is running — {START_HINT}"}

    def test_a_port_named_while_none_runs_is_the_same_answer(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(base, "_running_servers", lambda spec: [])
        assert _call(app, args={"port": 8788}) == {"error": f"no demoapp is running — {START_HINT}"}

    def test_the_same_process_answers_from_the_next_server_started(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With nothing serving, the tools answer with the start command, and every call looks for a server afresh —
        so the next server started is the one it answers from, and the MCP client never restarts its process."""
        running: List[Dict[str, Any]] = []
        _answering(monkeypatch, running)
        assert "no demoapp is running" in _call(app)["error"]
        running.append(SECOND)
        assert _call(app) == {"asked": 8788}

    def test_without_a_start_hint_the_error_points_at_the_apps_help(
        self, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        choice = ServerChoice(AppSpec(name="demoapp", cli_module="demoapp.cli", state_folder=state))
        assert choice.not_running == "no demoapp is running — start one — `demoapp --help` says how"

    def test_the_servers_are_those_serving_finds_for_the_spec(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real lookup asks ``portaide.serving.running_servers`` for this spec's servers, imported at call time."""
        asked: List[AppSpec] = []
        serving = types.ModuleType("portaide.serving")

        def running_servers(spec: AppSpec) -> List[Dict[str, Any]]:
            asked.append(spec)
            return [FIRST]

        serving.running_servers = running_servers  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "portaide.serving", serving)
        assert base._running_servers(app.spec) == [FIRST] and asked == [app.spec]


class TestSeveralRunning:
    """The user picks, never the server."""

    def test_the_user_is_asked_which_one_and_the_tools_use_it(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hit = _answering(monkeypatch, [FIRST, SECOND])
        user = _User("8788  /tmp/second.workspace.yaml")
        assert _call(app, user=user) == {"asked": 8788}
        assert user.asked[0].message == "Several demoapp servers are running — which one should the tools use?"
        assert user.choices() == ["8787  the plain type workspace", "8788  /tmp/second.workspace.yaml"]
        assert _ports(hit) == [8788]

    def test_the_choice_holds_without_a_question_even_when_another_starts(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        running = [FIRST, SECOND]
        _answering(monkeypatch, running)
        user = _User("8788  /tmp/second.workspace.yaml")
        _call(app, user=user)
        running.append(THIRD)
        assert _call(app, "find_records", user=user) == {"asked": 8788}
        assert len(user.asked) == 1

    def test_the_chosen_one_dying_asks_again(self, app: DemoApp, monkeypatch: pytest.MonkeyPatch) -> None:
        running = [FIRST, SECOND, THIRD]
        _answering(monkeypatch, running)
        user = _User("8788  /tmp/second.workspace.yaml", "8789  /tmp/third.workspace.yaml")
        _call(app, user=user)
        running.remove(SECOND)
        assert _call(app, user=user) == {"asked": 8789}
        assert user.choices() == ["8787  the plain type workspace", "8789  /tmp/third.workspace.yaml"]

    def test_another_workspace_on_the_chosen_port_is_not_the_one_chosen(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The choice is the WORKSPACE on that port: restarted with another workspace there, the server chosen has
        died, and the user is asked again."""
        running = [FIRST, SECOND]
        _answering(monkeypatch, running)
        user = _User("8788  /tmp/second.workspace.yaml", "8787  the plain type workspace")
        _call(app, user=user)
        running[1] = {**THIRD, "port": 8788}
        assert _call(app, user=user) == {"asked": 8787}
        assert len(user.asked) == 2

    def test_dismissing_the_question_answers_in_band_and_the_next_call_asks_again(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hit = _answering(monkeypatch, [FIRST, SECOND])
        user = _User(None, "8787  the plain type workspace")
        assert _call(app, user=user) == {
            "error": "no demoapp chosen — pass port: 8787 the plain type workspace, 8788 /tmp/second.workspace.yaml"
        }
        assert hit == []
        assert _call(app, user=user) == {"asked": 8787}

    def test_the_one_picked_stopping_while_the_question_was_open_is_not_connected_to(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        running = [FIRST, SECOND]
        hit = _answering(monkeypatch, running)

        class StopsWhileAsked(_User):
            async def __call__(self, context: Any, params: ElicitRequestParams) -> ElicitResult:
                running.remove(SECOND)
                return await super().__call__(context, params)

        out = _call(app, user=StopsWhileAsked("8788  /tmp/second.workspace.yaml"))
        assert out == {"error": "the demoapp picked stopped — pass port: 8787 the plain type workspace"}
        assert hit == [] and app.choice.chosen is None

    def test_a_client_that_cannot_ask_is_refused_naming_the_ports(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hit = _answering(monkeypatch, [FIRST, SECOND])
        assert _call(app) == {
            "error": "several demoapp servers are running — pass port: "
            "8787 the plain type workspace, 8788 /tmp/second.workspace.yaml"
        }
        assert hit == []

    def test_the_newer_protocol_asks_the_same_question(self, app: DemoApp, monkeypatch: pytest.MonkeyPatch) -> None:
        """A client on the newer protocol gets the question inside the tool's answer and calls again with the pick;
        the SDK's resolver carries both, so the tools ask once."""
        _answering(monkeypatch, [FIRST, SECOND])
        user = _User("8788  /tmp/second.workspace.yaml")
        assert _call(app, user=user, mode="auto") == {"asked": 8788}
        assert len(user.asked) == 1

    def test_two_apps_in_one_process_keep_their_own_choice(self, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The choice is the ServerChoice's, never module state: another app's server is asked afresh."""
        _answering(monkeypatch, [FIRST, SECOND])
        one = DemoApp(AppSpec(name="demoapp", cli_module="demoapp.cli", state_folder=state))
        other = DemoApp(AppSpec(name="otherapp", cli_module="otherapp.cli", state_folder=state))
        _call(one, args={"port": 8788})
        assert _call(other)["error"].startswith("several otherapp servers are running")


class TestAPortNamedByTheCaller:
    def test_the_named_port_is_used_without_a_question(self, app: DemoApp, monkeypatch: pytest.MonkeyPatch) -> None:
        _answering(monkeypatch, [FIRST, SECOND])
        user = _User()
        assert _call(app, args={"port": 8788}, user=user) == {"asked": 8788}
        assert user.asked == []

    def test_it_becomes_this_connections_server(self, app: DemoApp, monkeypatch: pytest.MonkeyPatch) -> None:
        _answering(monkeypatch, [FIRST, SECOND])
        _call(app, args={"port": 8788})
        user = _User()
        assert _call(app, "find_records", user=user) == {"asked": 8788}
        assert user.asked == []

    def test_a_port_overrides_the_choice_made_earlier(self, app: DemoApp, monkeypatch: pytest.MonkeyPatch) -> None:
        _answering(monkeypatch, [FIRST, SECOND])
        _call(app, user=_User("8788  /tmp/second.workspace.yaml"))
        assert _call(app, args={"port": 8787}) == {"asked": 8787}
        assert _call(app) == {"asked": 8787}

    def test_a_port_nothing_answers_on_names_the_running_ones(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hit = _answering(monkeypatch, [FIRST, SECOND])
        assert _call(app, args={"port": 8790}) == {
            "error": "no demoapp answers on port 8790 — running: "
            "8787 the plain type workspace, 8788 /tmp/second.workspace.yaml"
        }
        assert hit == [] and app.choice.chosen is None


class TestOneRunning:
    """The only one is used — once the client's user agreed to it, or told this client to always connect (kept per
    client name across connections, in the app's state folder)."""

    def test_the_first_connection_asks_and_this_time_is_not_kept(
        self, app: DemoApp, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answering(monkeypatch, [SECOND])
        user = _User("Connect this time")
        assert _call(app, user=user) == {"asked": 8788}
        assert user.asked[0].message == "One demoapp is running: 8788 /tmp/second.workspace.yaml. Connect?"
        assert user.choices() == [ALWAYS_HERE, "Connect this time"]
        assert not (state / base.PREFERENCES_FILE).exists()
        # Same connection: chosen, no new question. A new connection asks again.
        assert _call(app, user=user) == {"asked": 8788} and len(user.asked) == 1
        again = _User("Connect this time")
        _call(DemoApp(app.spec), user=again)
        assert len(again.asked) == 1

    def test_always_is_kept_for_that_client_only(
        self, app: DemoApp, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answering(monkeypatch, [SECOND])
        _call(app, user=_User(ALWAYS_HERE))
        assert app.choice.preferences_path() == state / "mcp-clients.json"
        assert json.loads((state / base.PREFERENCES_FILE).read_text()) == {"claude-code": {"auto_connect": True}}
        silent = _User()
        assert _call(DemoApp(app.spec), user=silent) == {"asked": 8788} and silent.asked == []
        other = _User("Connect this time")
        assert _call(DemoApp(app.spec), user=other, client="review-agent") == {"asked": 8788}
        assert other.choices()[0] == "Always connect automatically when only one runs (review-agent)"

    def test_a_second_client_saying_always_keeps_the_first(
        self, app: DemoApp, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answering(monkeypatch, [SECOND])
        state.mkdir(parents=True, exist_ok=True)
        (state / base.PREFERENCES_FILE).write_text(json.dumps({"claude-code": {"auto_connect": True}}))
        _call(app, user=_User("Always connect automatically when only one runs (review-agent)"), client="review-agent")
        assert json.loads((state / base.PREFERENCES_FILE).read_text()) == {
            "claude-code": {"auto_connect": True},
            "review-agent": {"auto_connect": True},
        }

    @pytest.mark.parametrize("text", ["{not json", "[]"])
    def test_an_unreadable_preference_file_is_no_preference(
        self, text: str, app: DemoApp, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answering(monkeypatch, [SECOND])
        state.mkdir(parents=True, exist_ok=True)
        (state / base.PREFERENCES_FILE).write_text(text)
        user = _User("Connect this time")
        assert _call(app, user=user) == {"asked": 8788} and len(user.asked) == 1

    def test_an_answer_that_cannot_be_kept_still_connects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A state folder that cannot be written (here a FILE in its place) loses the "always", never the
        connection."""
        _answering(monkeypatch, [SECOND])
        blocked = tmp_path / "not-a-folder"
        blocked.write_text("")
        app = DemoApp(AppSpec(name="demoapp", cli_module="demoapp.cli", state_folder=blocked))
        assert _call(app, user=_User(ALWAYS_HERE)) == {"asked": 8788}

    def test_the_only_one_stopping_while_the_question_was_open_is_the_start_command(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        running = [SECOND]
        _answering(monkeypatch, running)

        class StopsWhileAsked(_User):
            async def __call__(self, context: Any, params: ElicitRequestParams) -> ElicitResult:
                running.clear()
                return await super().__call__(context, params)

        assert _call(app, user=StopsWhileAsked("Connect this time")) == {
            "error": f"no demoapp is running — {START_HINT}"
        }

    def test_dismissing_it_answers_in_band(self, app: DemoApp, monkeypatch: pytest.MonkeyPatch) -> None:
        hit = _answering(monkeypatch, [SECOND])
        assert _call(app, user=_User(None)) == {
            "error": "no demoapp chosen — pass port: 8788 /tmp/second.workspace.yaml"
        }
        assert hit == []

    def test_dismissed_after_the_only_one_stopped_is_the_start_command(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        running = [SECOND]
        _answering(monkeypatch, running)

        class StopsAndDismisses(_User):
            async def __call__(self, context: Any, params: ElicitRequestParams) -> ElicitResult:
                running.clear()
                return await super().__call__(context, params)

        assert _call(app, user=StopsAndDismisses(None)) == {"error": f"no demoapp is running — {START_HINT}"}

    def test_a_client_that_cannot_ask_connects(self, app: DemoApp, monkeypatch: pytest.MonkeyPatch) -> None:
        _answering(monkeypatch, [SECOND])
        assert _call(app) == {"asked": 8788}

    def test_the_last_one_left_after_the_chosen_died_is_asked_about_too(
        self, app: DemoApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        running = [FIRST, SECOND]
        _answering(monkeypatch, running)
        user = _User("8788  /tmp/second.workspace.yaml", "Connect this time")
        _call(app, user=user)
        running.remove(SECOND)
        assert _call(app, user=user) == {"asked": 8787}
        assert user.asked[-1].message == "One demoapp is running: 8787 the plain type workspace. Connect?"

    def test_an_unnamed_client_can_only_connect_this_time(self, app: DemoApp, monkeypatch: pytest.MonkeyPatch) -> None:
        """No client name, nothing to keep "always" under — the question offers only this time."""
        _answering(monkeypatch, [SECOND])
        user = _User("Connect this time")
        assert _call(app, user=user, client="") == {"asked": 8788}
        assert user.choices() == ["Connect this time"]


class _Answer(io.BytesIO):
    """What ``urlopen`` hands back: a readable body that is its own context manager."""

    def __enter__(self) -> "_Answer":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class TestTheRelay:
    """``_request``: one JSON request, every failure a :class:`RelayError` that ``call`` reports in-band."""

    @pytest.fixture
    def sent(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[List[Any]]:
        requests: List[Any] = []
        self.answer: Any = _Answer(b'{"ok": true}')

        def urlopen(request: Any, timeout: float) -> Any:
            requests.append((request, timeout))
            if isinstance(self.answer, BaseException):
                raise self.answer
            return self.answer

        monkeypatch.setattr(base.urllib.request, "urlopen", urlopen)
        yield requests

    def test_a_json_body_is_posted_and_the_answer_read(self, sent: List[Any]) -> None:
        out = base._request(FIRST, "POST", "/api/x?a=1", {"b": 2}, app="demoapp", timeout=5.0)
        request, timeout = sent[0]
        assert out == {"ok": True} and timeout == 5.0
        assert request.full_url == "http://127.0.0.1:8787/api/x?a=1" and request.get_method() == "POST"
        assert json.loads(request.data) == {"b": 2} and request.get_header("Content-type") == "application/json"

    def test_a_request_without_a_body_sends_none(self, sent: List[Any]) -> None:
        base._request(FIRST, "GET", "/api/state", None, app="demoapp")
        request, timeout = sent[0]
        assert request.data is None and request.get_header("Content-type") is None
        assert timeout == base.DEFAULT_TIMEOUT

    def test_an_answer_that_is_not_an_object_is_wrapped(self, sent: List[Any]) -> None:
        self.answer = _Answer(b"[1, 2]")
        assert base._request(FIRST, "GET", "/", None, app="demoapp") == {"value": [1, 2]}

    def test_a_json_error_answer_is_worded_by_the_server(self, sent: List[Any]) -> None:
        self.answer = urllib.error.HTTPError("u", 404, "NOT FOUND", Message(), io.BytesIO(b'{"error": "no record r9"}'))
        with pytest.raises(RelayError, match="^no record r9$"):
            base._request(FIRST, "GET", "/", None, app="demoapp")

    def test_a_non_json_error_answer_is_its_status(self, sent: List[Any]) -> None:
        self.answer = urllib.error.HTTPError("u", 500, "INTERNAL", Message(), io.BytesIO(b"<html>"))
        with pytest.raises(RelayError, match="^500 INTERNAL$"):
            base._request(FIRST, "GET", "/", None, app="demoapp")

    def test_a_server_that_does_not_answer_is_named_with_the_app(self, sent: List[Any]) -> None:
        self.answer = urllib.error.URLError("refused")
        with pytest.raises(RelayError, match="^demoapp at 127.0.0.1:8787 did not answer: "):
            base._request(FIRST, "GET", "/", None, app="demoapp")

    def test_call_reports_every_failure_in_band(self, app: DemoApp, sent: List[Any]) -> None:
        self.answer = urllib.error.URLError("refused")
        assert app.choice.call(Target(FIRST), "GET", "/")["error"].startswith("demoapp at 127.0.0.1:8787")
        assert app.choice.call(Target(error="nothing here"), "GET", "/") == {"error": "nothing here"}

    def test_the_settled_refusal_is_raised_by_server(self, app: DemoApp) -> None:
        with pytest.raises(RelayError, match="nothing here"):
            app.choice.server(Target(error="nothing here"))
        assert app.choice.server(Target(FIRST)) is FIRST

    def test_query_string_leaves_out_what_was_not_given(self) -> None:
        assert query_string(offset=0, limit=10, where="", sort=None, q="a b") == "offset=0&limit=10&q=a+b"
