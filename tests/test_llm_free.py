"""portail is LLM-free, and the MCP SDK stays behind its optional extra.

``portail/mcp.py`` IS the MCP server base — a client of the served page that spawns no model and calls no LLM API —
so the ``mcp`` SDK is imported there and nowhere else, and nothing else in portail imports ``portail.mcp``: an app
that never installs ``portail[mcp]`` serves, stops and lists without the SDK. No module imports an LLM package.
"""

import re
from pathlib import Path

import portail

PACKAGE = Path(portail.__file__).parent
_LLM = ("import anthropic", "import pydantic_ai", "from anthropic", "from pydantic_ai")
_MCP_SDK = re.compile(r"^\s*(import mcp\b|from mcp\b)", re.MULTILINE)
_MCP_BASE = re.compile(r"^\s*(import portail\.mcp\b|from portail\.mcp\b|from portail import .*\bmcp\b)", re.MULTILINE)


def test_portail_imports_no_llm_sdk() -> None:
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text()
        for needle in _LLM:
            assert needle not in text, f"{path.name} imports an LLM package — portail stays LLM-free"


def test_the_mcp_sdk_is_imported_by_the_mcp_base_only() -> None:
    for path in PACKAGE.rglob("*.py"):
        if path == PACKAGE / "mcp.py":
            continue
        assert not _MCP_SDK.search(path.read_text()), f"{path.name} imports the MCP SDK — only mcp.py may"
    assert _MCP_SDK.search((PACKAGE / "mcp.py").read_text()), "mcp.py IS the MCP server base"


def test_nothing_in_portail_imports_the_mcp_base() -> None:
    for path in PACKAGE.rglob("*.py"):
        assert not _MCP_BASE.search(path.read_text()), f"{path.name} imports portail.mcp — it is an optional extra"
