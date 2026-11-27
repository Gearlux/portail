"""The layout of a page's panes, as an app keeps it in its workspace — the shape rule the server applies.

The page draws and changes the layout (``portail/layout.js``, ``Portail.layout``); an app that keeps it as workspace
content checks what it is handed with :func:`checked_layout` before it stores it. A layout is a tree of rows and
columns whose ends name panes::

    {"row": [{"pane": "list"}, {"column": [{"pane": "info"}, {"pane": "notes", "share": 2}]}]}

Each node holds exactly one of ``pane`` (a name), ``row`` (its children side by side, left to right) or ``column``
(stacked, top to bottom), and optionally ``share`` — its room beside its siblings, a number above 0 (1 when not
written). A pane is named once in a whole layout.
"""

import copy
import math
from typing import Any, Dict

#: The two kinds of split: children side by side, or stacked.
SPLITS = ("row", "column")
_KINDS = ("pane",) + SPLITS
_KEYS = frozenset(_KINDS + ("share",))


def checked_layout(value: Any, where: str = "layout") -> Dict[str, Any]:
    """Answer a copy of ``value`` when it is a layout, else raise :class:`ValueError` naming where the mistake is.

    Args:
        value: What the page sent, or what a workspace file holds.
        where: How the caller names ``value`` — the start of every refusal (``layout.row[1].share: …``).
    """
    _check(value, where, {})
    checked: Dict[str, Any] = copy.deepcopy(value)
    return checked


def _check(node: Any, where: str, seen: Dict[str, str]) -> None:
    if not isinstance(node, dict):
        raise ValueError(f"{where}: a layout is a pane, a row or a column — got {node!r}")
    unknown = sorted(str(key) for key in node if key not in _KEYS)
    if unknown:
        raise ValueError(f"{where}: {unknown[0]!r} is no key of a layout — a node holds pane, row or column, and share")
    kinds = [kind for kind in _KINDS if kind in node]
    if len(kinds) != 1:
        raise ValueError(f"{where}: a node holds exactly one of pane, row or column — got {sorted(map(str, node))}")
    if "share" in node:
        share = node["share"]
        if isinstance(share, bool) or not isinstance(share, (int, float)) or not math.isfinite(share) or share <= 0:
            raise ValueError(f"{where}.share: a share is a number above 0 — got {share!r}")
    kind = kinds[0]
    if kind == "pane":
        name = node["pane"]
        if not isinstance(name, str) or not name:
            raise ValueError(f"{where}.pane: a pane is named by a text — got {name!r}")
        if name in seen:
            raise ValueError(f"{where}.pane: {name!r} is named twice — first at {seen[name]}")
        seen[name] = f"{where}.pane"
        return
    children = node[kind]
    if not isinstance(children, list) or not children:
        raise ValueError(f"{where}.{kind}: a {kind} is a list of panes, rows and columns — got {children!r}")
    for index, child in enumerate(children):
        _check(child, f"{where}.{kind}[{index}]", seen)


__all__ = ["SPLITS", "checked_layout"]
