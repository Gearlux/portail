"""portaide is LLM-free, and the MCP SDK stays behind its optional extra.

``portaide/mcp.py`` IS the MCP server base — a client of the served page that spawns no model and calls no LLM API —
so the ``mcp`` SDK is imported there and nowhere else, and nothing else in portaide imports ``portaide.mcp``: an app
that never installs ``portaide[mcp]`` serves, stops and lists without the SDK. No module imports an LLM package.
"""

import re
from pathlib import Path

import portaide

PACKAGE = Path(portaide.__file__).parent
_LLM = ("import anthropic", "import pydantic_ai", "from anthropic", "from pydantic_ai")
_MCP_SDK = re.compile(r"^\s*(import mcp\b|from mcp\b)", re.MULTILINE)
_MCP_BASE = re.compile(
    r"^\s*(import portaide\.mcp\b|from portaide\.mcp\b|from portaide import .*\bmcp\b)", re.MULTILINE
)


def test_portaide_imports_no_llm_sdk() -> None:
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text()
        for needle in _LLM:
            assert needle not in text, f"{path.name} imports an LLM package — portaide stays LLM-free"


def test_the_mcp_sdk_is_imported_by_the_mcp_base_only() -> None:
    for path in PACKAGE.rglob("*.py"):
        if path == PACKAGE / "mcp.py":
            continue
        assert not _MCP_SDK.search(path.read_text()), f"{path.name} imports the MCP SDK — only mcp.py may"
    assert _MCP_SDK.search((PACKAGE / "mcp.py").read_text()), "mcp.py IS the MCP server base"


def test_nothing_in_portaide_imports_the_mcp_base() -> None:
    for path in PACKAGE.rglob("*.py"):
        assert not _MCP_BASE.search(path.read_text()), f"{path.name} imports portaide.mcp — it is an optional extra"
