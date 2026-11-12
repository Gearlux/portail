# The MCP server base (`portaide.mcp`)

An app built on portaide can expose its served workspace as tools an AI agent calls over the
[Model Context Protocol](https://modelcontextprotocol.io). The MCP server is a separate process the MCP client
spawns (stdio); it is a **client of the app already serving**: what the person opened, edited and is looking at lives
only in that process, so every tool relays to the page's HTTP API rather than rebuilding a workspace.

Install the extra: `pip install 'portaide[mcp]'` (MCP Python SDK 2.x). Nothing else in portaide imports
`portaide.mcp`, so an app without the extra serves, stops and lists as usual.

## What the base gives an app

| piece | what it does |
| --- | --- |
| `make_server(spec, instructions)` | an `LLMSafeMCPServer` named after the app |
| `LLMSafeMCPServer` | advertises every tool schema in the subset LLM function-calling APIs accept (no `$ref`, `const`, `additionalProperties`, …) and **tools only** — no phantom resources or prompts |
| `ServerChoice(spec, start_hint=..., timeout=120.0)` | which running server of the app a call talks to, and the relay to it |
| `choice.resolve` | the marker of a tool's target parameter: `Annotated[Target, choice.resolve]` |
| `choice.call(target, method, path, body=None)` | one JSON request to the settled server; every failure comes back **in-band** as `{"error": "..."}` |
| `query_string(**params)` | a URL query that leaves out the arguments a tool was not given (`None`, `""`) |

## A minimal server

```python
from typing import Annotated, Any, Dict

from portaide import AppSpec
from portaide.mcp import ServerChoice, Target, make_server, query_string

SPEC = AppSpec(name="demoapp", cli_module="demoapp.cli")

server = make_server(SPEC, instructions="Tools over the demoapp workspace being served. Start with page_state.")
choice = ServerChoice(SPEC, start_hint="start one with `demoapp open <workspace.yaml>`")


@server.tool()
def page_state(target: Annotated[Target, choice.resolve], port: int = 0) -> Dict[str, Any]:
    """What the page currently shows.

    Args:
        port: The server's port (`demoapp list`); 0 = the one this connection uses — named or picked earlier,
            else the user is asked. A port named is kept for later calls.
    """
    return choice.call(target, "GET", "/api/state")


@server.tool()
def list_records(target: Annotated[Target, choice.resolve], where: str = "", port: int = 0) -> Dict[str, Any]:
    """One page of the records the workspace lists."""
    return choice.call(target, "GET", f"/api/records?{query_string(where=where)}")


def main() -> None:
    server.run()  # the console script the MCP client spawns
```

The `target` parameter is filled by the SDK's resolvers and is never advertised to the agent; `port` is. Every tool
takes both, `target` first.

## Which server

Several servers of one app may run (`<app> list`). A call talks to:

1. **none running** → no server; the error is `no demoapp is running — <start_hint>`. The MCP process never starts
   the app itself, and every later call looks again, so the next server started is the one it answers from.
2. **`port` named** → that server, which becomes this connection's; nothing answering there → the error lists the
   running ones.
3. **the one chosen earlier**, while it still runs — the same host, port **and** identity (a port restarted with
   another workspace is another server).
4. **several running** → the client's user is asked which. A client that cannot ask (no MCP elicitation) is refused
   with `several demoapp servers are running — pass port: …`, so an agent passes `port`.
5. **one running** → used once the user agreed to a "connect?" question. Answering
   `Always connect automatically when only one runs (<client>)` is kept per client name in
   `<state folder>/mcp-clients.json` (`~/.demoapp/mcp-clients.json`); delete the file to be asked again. A client
   that cannot ask just connects.

The questions go through the SDK's `Resolve`/`Elicit` resolvers, never `ctx.elicit()`, so they ride whichever
protocol the client speaks. The choice is the `ServerChoice`'s: one MCP process per client connection, so a reconnect
starts with none.

## The rules a tool keeps

- **Answer in-band, never raise.** `choice.call` turns a refusal, an HTTP error (worded by the server's own
  `{"error": ...}`), and a server that does not answer into `{"error": "..."}`.
- **Never start the app.** The not-running error names the command; the person runs it.
- **The process keeps the code it started with.** A tool added or changed reaches a client when the client
  reconnects the MCP server; restarting the web app does not refresh it.

## Testing an app's tools

`portaide.mcp._running_servers(spec)` (server discovery) and `portaide.mcp._request(server, method, path, body, *,
app, timeout)` (the HTTP relay) are the seams: monkeypatch both, then call the tools through the SDK's in-process
`mcp.client.Client(server, elicitation_callback=...)` so the question travels as it does for a real client. portaide's
own `tests/test_mcp.py` does exactly this with a demo app.
