"""Every pane of a page can be dragged to the left, right, top or bottom of another pane.

A page hands ``Portail.layout(root, {default, saved, save, shown, resized})`` its panes one by one
(``layout.add(name, element, where)``). The layout is a tree of rows and columns whose ends name panes::

    {"row": [{"pane": "a"}, {"column": [{"pane": "b"}, {"pane": "c", "share": 2}]}]}

A ``share`` is a pane's room beside its siblings (1 when not written). Dragging a pane's handle (the element inside it
marked ``data-pane-handle``) onto another pane splits that pane in the direction of the edge nearest the pointer; the
dropped pane takes half of its room. The layout moves the page's own elements and never builds them again, so what a
pane holds survives a move. The page keeps the tree where it likes: ``save(tree)`` is called once per drop and once
per border released.

``portail.layout.checked_layout`` is the same shape rule on the server, for an app that keeps the tree in its
workspace.

The behaviour lives in the page's JavaScript, so it is proven by driving a real page.
"""

import math
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

import pytest
from flask import Flask

from portail.serving import Server
from portail.web import STATIC, mount_shell
from tests.browsers import chromium_page
from tests.demo_cli import DEMO_SPEC, DemoWorkspace

#: The page every test drives: five panes the tests name ``a`` … ``e``, each with a handle and a text box, and a
#: ``make`` that builds a layout the way an app does. ``away`` lists the panes the page says are not shown (a pane
#: the page itself keeps elsewhere for a while); ``elsewhere`` is such a place. ``fold`` folds a pane to its handle, as
#: a page's minimize button does.
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>layout</title>
<link rel="stylesheet" href="portail/shell.css">
<style>
  html, body { margin: 0; height: 100%; }
  body { display: flex; flex-direction: column; }
  #top { flex: none; height: 32px; display: flex; gap: 4px; align-items: center; }
  #root { flex: 1; min-height: 0; }
  #elsewhere { flex: none; height: 40px; }
  .pane { overflow: hidden; }
  .grip { display: inline-block; padding: 2px 8px; cursor: grab; user-select: none; }
  .pane.fold > input { display: none; }
</style></head>
<body>
<header id="top"></header>
<main id="root"></main>
<div id="elsewhere"></div>
<div id="stock" hidden></div>
<script src="portail/shell.js"></script>
<script src="portail/layout.js"></script>
<script>
  window.saved = []; window.resizes = 0; window.clicked = []; window.away = []; window.folded = [];
  window.fold = (name, on) => {
    paneOf(name).classList.toggle("fold", on);
    window.folded = on ? [...window.folded, name] : window.folded.filter((n) => n !== name);
    window.layout.refresh();
  };
  window.paneOf = (name) => {
    let el = document.getElementById("pane-" + name);
    if (!el) {
      el = document.createElement("section");
      el.id = "pane-" + name;
      el.className = "pane";
      el.innerHTML = `<span class="grip" data-pane-handle>${name}</span><input id="input-${name}">`;
      el.querySelector(".grip").addEventListener("click", () => window.clicked.push(name));
      document.getElementById("stock").append(el);
    }
    return el;
  };
  window.make = (opts) => {
    window.layout = Portail.layout(document.getElementById("root"), {
      default: opts.default,
      saved: opts.saved === undefined ? null : opts.saved,
      save: (tree) => window.saved.push(tree),
      shown: (name, el) => !el.hidden && !window.away.includes(name),
      folded: (name) => window.folded.includes(name),
      resized: () => { window.resizes += 1; },
    });
    for (const [name, where] of opts.panes) window.layout.add(name, window.paneOf(name), where);
    return window.layout.tree();
  };
  Portail.start({ buttons: "#top" });
</script>
</body></html>
"""

Tree = Dict[str, Any]
#: A layout without its shares: a pane is its name, a split is ``(kind, [children])``.
Shape = Union[str, Tuple[str, List[Any]]]

#: Three panes side by side, what most tests start from.
THREE: Tree = {"row": [{"pane": "a"}, {"pane": "b"}, {"pane": "c"}]}

#: Every pane's first place: named by the tree, so no ``where`` is needed.
NAMED = None

#: How far a result may sit from the expected size: the borders take a few pixels.
SLACK = 8


def _shape(node: Tree) -> Shape:
    if "pane" in node:
        return str(node["pane"])
    kind = "row" if "row" in node else "column"
    return (kind, [_shape(child) for child in node[kind]])


@pytest.fixture(scope="module")
def url() -> Iterator[str]:
    """The test page on a real socket, with the shell mounted for ``portail/shell.js`` and ``portail/layout.js``."""
    from werkzeug.serving import make_server

    app = Flask("layout-page", static_folder=None)
    app.add_url_rule("/", "page", lambda: PAGE)
    mount_shell(app, Server(DEMO_SPEC, DemoWorkspace()))
    socket_server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=socket_server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{socket_server.server_port}/"
    finally:
        socket_server.shutdown()
        thread.join()


@pytest.fixture()
def page(url: str) -> Iterator[Any]:
    with chromium_page(url, viewport={"width": 1200, "height": 800}) as page:
        yield page


def _make(
    page: Any,
    default: Tree,
    panes: Optional[List[Tuple[str, Optional[Dict[str, str]]]]] = None,
    saved: Any = None,
) -> Tree:
    """Build the layout; ``panes`` defaults to every pane the default names, each in its named place."""
    if panes is None:
        panes = [(name, NAMED) for name in _names(default)]
    tree: Tree = page.evaluate("(opts) => make(opts)", {"default": default, "panes": panes, "saved": saved})
    return tree


def _names(node: Tree) -> List[str]:
    if "pane" in node:
        return [str(node["pane"])]
    kind = "row" if "row" in node else "column"
    return [name for child in node[kind] for name in _names(child)]


def _tree(page: Any) -> Tree:
    tree: Tree = page.evaluate("() => layout.tree()")
    return tree


def _saved(page: Any) -> List[Tree]:
    saved: List[Tree] = page.evaluate("() => window.saved")
    return saved


def _box(page: Any, name: str) -> Dict[str, float]:
    box: Dict[str, float] = page.evaluate(
        "(name) => { const r = document.getElementById('pane-' + name).getBoundingClientRect();"
        " return {x: r.left, y: r.top, width: r.width, height: r.height, right: r.right, bottom: r.bottom}; }",
        name,
    )
    return box


def _root(page: Any) -> Dict[str, float]:
    box: Dict[str, float] = page.evaluate(
        "() => { const r = document.getElementById('root').getBoundingClientRect();"
        " return {x: r.left, y: r.top, width: r.width, height: r.height, right: r.right, bottom: r.bottom}; }"
    )
    return box


def _borders(page: Any) -> List[Dict[str, float]]:
    """The borders on screen — every ``.portail-border`` inside the layout that takes room."""
    borders: List[Dict[str, float]] = page.evaluate(
        """() => [...document.querySelectorAll('#root .portail-border')]
            .map((el) => el.getBoundingClientRect())
            .filter((r) => r.width > 0 && r.height > 0)
            .map((r) => ({x: r.left, y: r.top, width: r.width, height: r.height}))"""
    )
    return borders


def _border_between(page: Any, first: str, second: str) -> Dict[str, float]:
    """The border that sits between two panes side by side or stacked."""
    a, b = _box(page, first), _box(page, second)
    for border in _borders(page):
        middle_x, middle_y = border["x"] + border["width"] / 2, border["y"] + border["height"] / 2
        if a["right"] - 1 <= middle_x <= b["x"] + 1 or a["bottom"] - 1 <= middle_y <= b["y"] + 1:
            return border
    raise AssertionError(f"no border between {first} and {second}: {_borders(page)}")


def _press_handle(page: Any, name: str) -> None:
    handle = page.locator(f"#pane-{name} [data-pane-handle]").bounding_box()
    page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
    page.mouse.down()


def _point_in(page: Any, name: str, at: Tuple[float, float]) -> Tuple[float, float]:
    box = _box(page, name)
    return box["x"] + box["width"] * at[0], box["y"] + box["height"] * at[1]


#: Where in a pane the pointer is released for each of its four zones: the zone is the edge NEAREST the pointer.
AT = {"left": (0.1, 0.5), "right": (0.9, 0.5), "above": (0.5, 0.1), "below": (0.5, 0.9)}


def _drag(page: Any, name: str, onto: str, side: str) -> None:
    """A real pointer drag of ``name``'s handle, released in ``onto``'s zone for ``side``."""
    _press_handle(page, name)
    x, y = _point_in(page, onto, AT[side])
    page.mouse.move(x, y, steps=8)
    page.mouse.up()


def _drag_to(page: Any, name: str, x: float, y: float) -> None:
    _press_handle(page, name)
    page.mouse.move(x, y, steps=8)
    page.mouse.up()


def _drag_border(page: Any, border: Dict[str, float], dx: float = 0, dy: float = 0, release: bool = True) -> None:
    x, y = border["x"] + border["width"] / 2, border["y"] + border["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=6)
    if release:
        page.mouse.up()


class TestTheFirstLayout:
    def test_without_a_saved_layout_the_default_is_drawn(self, page: Any) -> None:
        _make(page, {"row": [{"pane": "a"}, {"column": [{"pane": "b"}, {"pane": "c"}]}]})
        a, b, c = _box(page, "a"), _box(page, "b"), _box(page, "c")
        assert a["right"] <= b["x"], "a sits left of the column"
        assert abs(b["x"] - c["x"]) < 1 and b["bottom"] <= c["y"], "b sits on top of c"
        assert abs(a["width"] - b["width"]) <= SLACK, "a and the column share the row evenly"
        assert abs(a["height"] - _root(page)["height"]) <= 1, "a pane alone in its column fills the height"

    def test_a_saved_layout_wins_over_the_default(self, page: Any) -> None:
        tree = _make(page, THREE, saved={"row": [{"pane": "c"}, {"pane": "b"}, {"pane": "a"}]})
        assert _shape(tree) == ("row", ["c", "b", "a"])
        assert _box(page, "c")["right"] <= _box(page, "b")["x"] <= _box(page, "a")["x"]

    def test_shares_divide_the_room(self, page: Any) -> None:
        _make(page, THREE, saved={"row": [{"pane": "a", "share": 3}, {"pane": "b", "share": 1}]})
        assert abs(_box(page, "a")["width"] - 3 * _box(page, "b")["width"]) <= SLACK

    def test_no_share_makes_a_pane_smaller_than_60_px(self, page: Any) -> None:
        """A pane squeezed to nothing cannot be grabbed back: whatever the shares say, every shown pane keeps 60 px."""
        _make(page, THREE, saved={"row": [{"pane": "a", "share": 1000}, {"pane": "b"}, {"pane": "c"}]})
        assert _box(page, "b")["width"] >= 60 - 1 and _box(page, "c")["width"] >= 60 - 1

    @pytest.mark.parametrize(
        "saved",
        [
            {"grid": [{"pane": "a"}]},
            {"row": []},
            {"row": [{"pane": "a"}, {"pane": "a"}]},
            {"row": [{"pane": "a", "share": 0}, {"pane": "b"}]},
            {"pane": "a", "row": [{"pane": "b"}]},
            "a",
            3,
        ],
        ids=["unknown-kind", "empty-split", "a-pane-twice", "share-zero", "two-kinds", "a-string", "a-number"],
    )
    def test_a_saved_layout_that_is_no_layout_draws_the_default(self, page: Any, saved: Any) -> None:
        """What a browser or a file kept may be anything: the page still opens, on the default."""
        tree = _make(page, THREE, saved=saved)
        assert _shape(tree) == ("row", ["a", "b", "c"])
        assert _saved(page) == [], "nothing is written over what was kept until the person moves something"

    def test_a_saved_name_no_pane_answers_is_skipped_and_kept(self, page: Any) -> None:
        """A pane of another machine's page (a graph this one lacks): the others take its room, and the tree keeps
        it, so the pane is back in its place on the page that has it."""
        tree = _make(
            page,
            THREE,
            panes=[("a", NAMED), ("b", NAMED), ("c", {"pane": "b", "side": "below"})],
            saved={"row": [{"pane": "a"}, {"pane": "ghost"}, {"pane": "b"}]},
        )
        assert _shape(tree) == ("row", ["a", "ghost", ("column", ["b", "c"])])
        assert abs(_box(page, "a")["width"] - _box(page, "b")["width"]) <= SLACK, "ghost takes no room"
        assert len([b for b in _borders(page) if b["height"] > b["width"]]) == 1, "one border between a and b"
        _drag(page, "c", "a", "left")
        assert "ghost" in _names(_saved(page)[-1])

    def test_a_pane_the_layout_does_not_name_goes_where_the_page_says(self, page: Any) -> None:
        tree = _make(
            page,
            {"row": [{"pane": "a"}, {"pane": "b"}]},
            panes=[("a", NAMED), ("b", NAMED), ("c", {"pane": "b", "side": "below"})],
        )
        assert _shape(tree) == ("row", ["a", ("column", ["b", "c"])])

    def test_a_place_beside_a_pane_the_layout_does_not_name_is_the_whole_layouts_edge(self, page: Any) -> None:
        tree = _make(
            page,
            {"row": [{"pane": "a"}, {"pane": "b"}]},
            panes=[("a", NAMED), ("b", NAMED), ("c", {"pane": "nowhere", "side": "below"})],
        )
        assert _shape(tree) == ("column", [("row", ["a", "b"]), "c"])

    def test_without_a_place_a_new_pane_goes_to_the_whole_layouts_right(self, page: Any) -> None:
        tree = _make(page, {"row": [{"pane": "a"}, {"pane": "b"}]}, panes=[("a", NAMED), ("b", NAMED), ("c", None)])
        assert _shape(tree) == ("row", ["a", "b", "c"])

    def test_a_pane_the_layout_names_goes_to_its_own_place_whatever_the_page_says(self, page: Any) -> None:
        tree = _make(
            page,
            THREE,
            panes=[("a", NAMED), ("b", NAMED), ("c", {"pane": "a", "side": "below"})],
            saved={"row": [{"pane": "a"}, {"pane": "c"}, {"pane": "b"}]},
        )
        assert _shape(tree) == ("row", ["a", "c", "b"])

    def test_the_first_layout_is_not_saved(self, page: Any) -> None:
        _make(page, THREE)
        assert _saved(page) == []


class TestADrop:
    @pytest.mark.parametrize(
        "side, shape",
        [
            ("left", ("row", ["c", "a", "b"])),
            ("right", ("row", ["a", "c", "b"])),
            ("above", ("row", [("column", ["c", "a"]), "b"])),
            ("below", ("row", [("column", ["a", "c"]), "b"])),
        ],
    )
    def test_a_drop_splits_the_pane_toward_the_edge_nearest_the_pointer(
        self, page: Any, side: str, shape: Shape
    ) -> None:
        _make(page, THREE)
        _drag(page, "c", "a", side)
        assert _shape(_tree(page)) == shape

    def test_the_dropped_pane_takes_half_the_room_of_the_pane_it_was_dropped_on(self, page: Any) -> None:
        """Beside a pane of its own row: a and c take a's old room together, b keeps its own."""
        _make(page, THREE)
        _drag(page, "c", "a", "right")
        a, b, c = (_box(page, name)["width"] for name in "abc")
        assert abs(a - c) <= SLACK and abs(b - (a + c)) <= SLACK

    def test_dropped_across_the_row_the_two_share_the_pane_s_place(self, page: Any) -> None:
        _make(page, THREE)
        _drag(page, "c", "a", "below")
        a, b, c = _box(page, "a"), _box(page, "b"), _box(page, "c")
        assert abs(a["height"] - c["height"]) <= SLACK, "a and c stack evenly"
        assert abs(a["width"] - c["width"]) < 1 and abs(a["width"] - b["width"]) <= SLACK, "the column takes a's place"

    def test_the_moved_pane_is_the_same_element_and_keeps_what_it_holds(self, page: Any) -> None:
        _make(page, THREE)
        page.fill("#input-c", "typed before the move")
        page.evaluate("() => { document.getElementById('pane-c').mark = 7; }")
        _drag(page, "c", "a", "below")
        assert page.evaluate("() => document.getElementById('pane-c').mark") == 7
        assert page.input_value("#input-c") == "typed before the move"
        assert page.evaluate("() => document.querySelectorAll('#pane-c').length") == 1

    def test_a_split_left_with_one_pane_dissolves(self, page: Any) -> None:
        _make(page, {"row": [{"pane": "a"}, {"column": [{"pane": "b"}, {"pane": "c"}]}]})
        _drag(page, "c", "a", "right")
        assert _shape(_tree(page)) == ("row", ["a", "c", "b"])

    def test_a_split_that_dissolves_into_its_own_direction_joins_it_and_its_panes_keep_their_room(
        self, page: Any
    ) -> None:
        _make(page, {"row": [{"pane": "a"}, {"column": [{"pane": "b"}, {"row": [{"pane": "c"}, {"pane": "d"}]}]}]})
        before = {name: _box(page, name)["width"] for name in "cd"}
        _drag(page, "b", "a", "right")
        assert _shape(_tree(page)) == ("row", ["a", "b", "c", "d"])
        for name in "cd":
            assert abs(_box(page, name)["width"] - before[name]) <= SLACK, f"{name} keeps its width"

    def test_a_drop_on_its_own_pane_changes_nothing(self, page: Any) -> None:
        before = _make(page, THREE)
        _drag(page, "b", "b", "below")
        assert _tree(page) == before and _saved(page) == []

    def test_a_drop_outside_every_pane_changes_nothing(self, page: Any) -> None:
        before = _make(page, THREE)
        _drag_to(page, "b", 600, 10)  # over the header, outside the layout
        assert _tree(page) == before and _saved(page) == []

    def test_escape_during_a_drag_changes_nothing(self, page: Any) -> None:
        before = _make(page, THREE)
        _press_handle(page, "c")
        x, y = _point_in(page, "a", AT["below"])
        page.mouse.move(x, y, steps=8)
        page.keyboard.press("Escape")
        page.mouse.up()
        assert _tree(page) == before and _saved(page) == []
        assert (
            page.evaluate("() => [...document.querySelectorAll('.portail-drop')].some((el) => el.offsetWidth)") is False
        )

    def test_a_press_that_barely_moves_is_a_click(self, page: Any) -> None:
        """The handle is often a button already (a pane's close chip): under 4 px, the press is that button's."""
        before = _make(page, THREE)
        _press_handle(page, "b")
        handle = page.locator("#pane-b [data-pane-handle]").bounding_box()
        page.mouse.move(handle["x"] + handle["width"] / 2 + 2, handle["y"] + handle["height"] / 2 + 1)
        page.mouse.up()
        assert page.evaluate("() => window.clicked") == ["b"]
        assert _tree(page) == before and _saved(page) == []

    def test_a_drag_that_moves_is_no_click(self, page: Any) -> None:
        _make(page, THREE)
        _drag(page, "c", "a", "below")
        assert page.evaluate("() => window.clicked") == []

    def test_the_zone_under_the_pointer_is_shown_while_dragging_and_gone_after(self, page: Any) -> None:
        _make(page, THREE)
        _press_handle(page, "c")
        x, y = _point_in(page, "a", AT["right"])
        page.mouse.move(x, y, steps=8)
        a = _box(page, "a")
        zone = page.evaluate(
            """() => { const el = [...document.querySelectorAll('.portail-drop')].find((z) => z.offsetWidth);
                       if (!el) return null; const r = el.getBoundingClientRect();
                       return {x: r.left, y: r.top, width: r.width, height: r.height}; }"""
        )
        assert zone is not None, "a zone is drawn while the pointer is over a pane"
        assert abs(zone["x"] - (a["x"] + a["width"] / 2)) <= 2 and abs(zone["width"] - a["width"] / 2) <= 2
        assert abs(zone["y"] - a["y"]) <= 2 and abs(zone["height"] - a["height"]) <= 2, "the right half of a"
        page.mouse.up()
        assert (
            page.evaluate("() => [...document.querySelectorAll('.portail-drop')].some((el) => el.offsetWidth)") is False
        )

    def test_every_drop_is_saved_once_as_the_tree_it_left(self, page: Any) -> None:
        _make(page, THREE)
        _drag(page, "c", "a", "below")
        assert _saved(page) == [_tree(page)]
        _drag(page, "b", "a", "left")
        assert _saved(page)[1:] == [_tree(page)]

    def test_the_page_hears_that_the_panes_were_resized(self, page: Any) -> None:
        _make(page, THREE)
        before = page.evaluate("() => window.resizes")
        _drag(page, "c", "a", "below")
        assert page.evaluate("() => window.resizes") > before


class TestTheLayoutsOwnEdges:
    """A pane dropped on the edge of the whole layout spans it — the only way to put a pane under every other one
    once no row runs the full width."""

    def test_a_drop_on_the_bottom_edge_puts_the_pane_under_everything(self, page: Any) -> None:
        _make(page, THREE)
        root = _root(page)
        _drag_to(page, "c", root["x"] + root["width"] / 2, root["bottom"] - 3)
        assert _shape(_tree(page)) == ("column", [("row", ["a", "b"]), "c"])
        assert abs(_box(page, "c")["width"] - root["width"]) <= 1

    def test_a_drop_on_the_left_edge_puts_the_pane_left_of_everything(self, page: Any) -> None:
        _make(page, {"column": [{"pane": "a"}, {"pane": "b"}, {"pane": "c"}]})
        root = _root(page)
        _drag_to(page, "c", root["x"] + 3, root["y"] + root["height"] / 2)
        assert _shape(_tree(page)) == ("row", ["c", ("column", ["a", "b"])])

    def test_the_zone_of_an_edge_spans_the_whole_layout(self, page: Any) -> None:
        _make(page, THREE)
        root = _root(page)
        _press_handle(page, "c")
        page.mouse.move(root["x"] + root["width"] / 2, root["bottom"] - 3, steps=8)
        zone = page.evaluate(
            """() => { const el = [...document.querySelectorAll('.portail-drop')].find((z) => z.offsetWidth);
                       const r = el.getBoundingClientRect(); return {x: r.left, width: r.width}; }"""
        )
        page.mouse.up()
        assert abs(zone["x"] - root["x"]) <= 1 and abs(zone["width"] - root["width"]) <= 1


class TestClosedPanes:
    def _close(self, page: Any, name: str, closed: bool = True) -> None:
        page.evaluate("([name, closed]) => { paneOf(name).hidden = closed; layout.refresh(); }", [name, closed])

    def test_a_closed_pane_takes_no_room_and_has_no_border(self, page: Any) -> None:
        _make(page, THREE)
        self._close(page, "b")
        assert abs(_box(page, "a")["width"] - _box(page, "c")["width"]) <= SLACK
        assert len(_borders(page)) == 1

    def test_a_closed_pane_keeps_its_place_and_opens_there_again(self, page: Any) -> None:
        _make(page, THREE)
        self._close(page, "b")
        self._close(page, "b", closed=False)
        a, b, c = (_box(page, name) for name in "abc")
        assert a["right"] <= b["x"] and b["right"] <= c["x"]
        assert _shape(_tree(page)) == ("row", ["a", "b", "c"])

    def test_closing_and_opening_a_pane_moves_no_other_so_its_scroll_stays(self, page: Any) -> None:
        """An element taken out of the document and put back starts at its top again: an open or a close must move
        nothing that stays where it was."""
        _make(page, THREE)
        page.evaluate(
            """() => { const a = paneOf('a'); a.style.overflow = 'auto';
                       a.insertAdjacentHTML('beforeend', '<div style="height: 3000px"></div>'); a.scrollTop = 500; }"""
        )
        self._close(page, "b")
        self._close(page, "b", closed=False)
        assert page.evaluate("() => paneOf('a').scrollTop") == 500

    def test_a_pane_not_shown_takes_no_room_even_with_padding(self, page: Any) -> None:
        """The page says a pane is not shown (the others while one is maximized) but leaves it where it is: it is not
        displayed, so not even its padding takes room — and it is displayed again once it is shown."""
        _make(page, THREE)
        page.evaluate("() => { paneOf('b').style.padding = '0 24px'; window.away = ['b']; layout.refresh(); }")
        assert _box(page, "b")["width"] == 0
        assert abs(_box(page, "a")["width"] - _box(page, "c")["width"]) <= SLACK
        page.evaluate("() => { window.away = []; layout.refresh(); }")
        assert _box(page, "b")["width"] > 300

    def test_closing_is_no_move_and_is_not_saved(self, page: Any) -> None:
        _make(page, THREE)
        self._close(page, "b")
        assert _saved(page) == []

    def test_a_split_whose_panes_are_all_closed_takes_no_room(self, page: Any) -> None:
        _make(page, {"row": [{"pane": "a"}, {"column": [{"pane": "b"}, {"pane": "c"}]}]})
        self._close(page, "b")
        self._close(page, "c")
        assert abs(_box(page, "a")["width"] - _root(page)["width"]) <= 1
        assert _borders(page) == []

    def test_a_pane_the_page_says_is_not_shown_is_left_where_the_page_put_it(self, page: Any) -> None:
        """A page may keep a pane somewhere else for a while (beside a picture, say): the layout gives its place
        no room and never pulls the element back — until the page says it is shown again."""
        _make(page, THREE)
        page.evaluate(
            "() => { window.away = ['b']; document.getElementById('elsewhere').append(paneOf('b')); layout.refresh(); }"
        )
        assert page.evaluate("() => paneOf('b').parentElement.id") == "elsewhere"
        assert abs(_box(page, "a")["width"] - _box(page, "c")["width"]) <= SLACK
        page.evaluate("() => { window.away = []; layout.refresh(); }")
        assert page.evaluate("() => !!paneOf('b').closest('#root')")
        assert _box(page, "a")["right"] <= _box(page, "b")["x"] <= _box(page, "c")["x"]


class TestTheBorders:
    def test_one_border_sits_between_every_two_shown_neighbours(self, page: Any) -> None:
        _make(page, {"row": [{"pane": "a"}, {"column": [{"pane": "b"}, {"pane": "c"}]}]})
        borders = _borders(page)
        assert len(borders) == 2
        assert len([b for b in borders if b["height"] > b["width"]]) == 1, "one between a and the column"
        assert len([b for b in borders if b["width"] > b["height"]]) == 1, "one between b and c"

    def test_dragging_a_border_moves_room_between_its_two_panes_only(self, page: Any) -> None:
        _make(page, THREE)
        before = {name: _box(page, name)["width"] for name in "abc"}
        _drag_border(page, _border_between(page, "a", "b"), dx=100)
        assert abs(_box(page, "a")["width"] - (before["a"] + 100)) <= 2
        assert abs(_box(page, "b")["width"] - (before["b"] - 100)) <= 2
        assert abs(_box(page, "c")["width"] - before["c"]) <= 1

    def test_a_border_follows_the_pointer_beside_panes_with_padding(self, page: Any) -> None:
        """A pane's padding and border are its own whatever its share: only the room the shares divide moves, so the
        border lands where it is let go."""
        _make(page, THREE)
        page.evaluate("() => { for (const n of 'abc') paneOf(n).style.padding = '0 24px'; layout.refresh(); }")
        before = {name: _box(page, name)["width"] for name in "abc"}
        _drag_border(page, _border_between(page, "a", "b"), dx=100)
        assert abs(_box(page, "a")["width"] - (before["a"] + 100)) <= 1
        assert abs(_box(page, "b")["width"] - (before["b"] - 100)) <= 1

    def test_a_border_stops_where_a_pane_would_get_smaller_than_60_px(self, page: Any) -> None:
        _make(page, THREE)
        border = _border_between(page, "a", "b")
        _drag_border(page, border, dx=_root(page)["right"] - 5 - border["x"])  # far past b's other side
        assert abs(_box(page, "b")["width"] - 60) <= 1

    def test_a_border_is_saved_once_when_released_and_never_on_its_way(self, page: Any) -> None:
        _make(page, THREE)
        _drag_border(page, _border_between(page, "a", "b"), dx=80, release=False)
        assert _saved(page) == []
        page.mouse.up()
        assert _saved(page) == [_tree(page)]

    def test_the_page_hears_every_step_of_a_border_drag(self, page: Any) -> None:
        """A picture re-fits to its room while the border moves, not only when it is let go."""
        _make(page, THREE)
        before = page.evaluate("() => window.resizes")
        _drag_border(page, _border_between(page, "a", "b"), dx=80)
        assert page.evaluate("() => window.resizes") >= before + 3

    def test_a_double_click_evens_the_two_panes_beside_it_and_leaves_the_others(self, page: Any) -> None:
        _make(page, THREE)
        _drag_border(page, _border_between(page, "a", "b"), dx=120)
        c = _box(page, "c")["width"]
        border = _border_between(page, "a", "b")
        page.mouse.dblclick(border["x"] + border["width"] / 2, border["y"] + border["height"] / 2)
        assert abs(_box(page, "a")["width"] - _box(page, "b")["width"]) <= 2
        assert abs(_box(page, "c")["width"] - c) <= 1
        assert _saved(page)[-1] == _tree(page)

    def test_a_border_is_a_separator_a_keyboard_can_reach(self, page: Any) -> None:
        _make(page, {"row": [{"pane": "a"}, {"column": [{"pane": "b"}, {"pane": "c"}]}]})
        said = page.evaluate(
            """() => [...document.querySelectorAll('#root .portail-border')]
                .filter((el) => el.offsetWidth || el.offsetHeight)
                .map((el) => [el.getAttribute('role'), el.getAttribute('aria-orientation'), el.tabIndex])"""
        )
        assert sorted(said) == [["separator", "horizontal", 0], ["separator", "vertical", 0]]

    def test_a_focused_border_moves_20_px_by_its_arrow_keys_and_100_with_shift(self, page: Any) -> None:
        _make(page, THREE)
        border = _border_between(page, "a", "b")
        page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y).focus()",
            [border["x"] + border["width"] / 2, border["y"] + border["height"] / 2],
        )
        start = {name: _box(page, name)["width"] for name in "abc"}
        page.keyboard.press("ArrowRight")
        assert abs(_box(page, "a")["width"] - (start["a"] + 20)) <= 1
        assert abs(_box(page, "b")["width"] - (start["b"] - 20)) <= 1
        page.keyboard.press("Shift+ArrowLeft")
        assert abs(_box(page, "a")["width"] - (start["a"] - 80)) <= 1
        assert abs(_box(page, "c")["width"] - start["c"]) <= 1
        assert len(_saved(page)) == 2 and _saved(page)[-1] == _tree(page), "each key press is saved"

    def test_a_focused_border_between_stacked_panes_moves_by_the_up_and_down_keys(self, page: Any) -> None:
        _make(page, {"column": [{"pane": "a"}, {"pane": "b"}]})
        border = _border_between(page, "a", "b")
        page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y).focus()",
            [border["x"] + border["width"] / 2, border["y"] + border["height"] / 2],
        )
        start = _box(page, "a")["height"]
        page.keyboard.press("ArrowDown")
        assert abs(_box(page, "a")["height"] - (start + 20)) <= 1
        page.keyboard.press("ArrowRight")
        assert abs(_box(page, "a")["height"] - (start + 20)) <= 1, "a key across the border moves nothing"

    def test_the_room_dragged_is_in_the_tree_as_shares(self, page: Any) -> None:
        """A share, never pixels: a wider window widens every pane."""
        _make(page, THREE)
        _drag_border(page, _border_between(page, "a", "b"), dx=100)
        shares = [child["share"] for child in _tree(page)["row"]]
        widths = [_box(page, name)["width"] for name in "abc"]
        assert abs(shares[0] / shares[2] - widths[0] / widths[2]) < 0.05


class TestFoldedPanes:
    """A page may FOLD a pane to its head (a minimize button): the pane stays in its place, shown, at the size of what
    it still shows — and a border beside it moves the room between the open panes around it."""

    def test_a_folded_pane_takes_only_its_own_size(self, page: Any) -> None:
        _make(page, THREE)
        page.evaluate("() => fold('b', true)")
        assert _box(page, "b")["width"] < 60, "the width of its handle, not a share"
        assert abs(_box(page, "a")["width"] - _box(page, "c")["width"]) <= SLACK
        assert _box(page, "a")["right"] <= _box(page, "b")["x"] <= _box(page, "c")["x"], "in its place"

    def test_unfolded_it_takes_its_share_again(self, page: Any) -> None:
        _make(page, THREE)
        page.evaluate("() => fold('b', true)")
        page.evaluate("() => fold('b', false)")
        widths = [_box(page, name)["width"] for name in "abc"]
        assert max(widths) - min(widths) <= SLACK

    def test_a_border_beside_a_folded_pane_moves_room_between_the_open_panes_around_it(self, page: Any) -> None:
        _make(page, THREE)
        page.evaluate("() => fold('b', true)")
        before = {name: _box(page, name)["width"] for name in "abc"}
        _drag_border(page, _border_between(page, "a", "b"), dx=100)
        assert abs(_box(page, "a")["width"] - (before["a"] + 100)) <= 2
        assert abs(_box(page, "c")["width"] - (before["c"] - 100)) <= 2
        assert abs(_box(page, "b")["width"] - before["b"]) <= 1

    def test_a_split_whose_shown_panes_are_all_folded_takes_only_their_size(self, page: Any) -> None:
        _make(page, {"row": [{"pane": "a"}, {"column": [{"pane": "b"}, {"pane": "c"}]}]})
        page.evaluate("() => { fold('b', true); fold('c', true); }")
        assert _box(page, "b")["width"] < 60 and _box(page, "c")["width"] < 60
        assert _box(page, "a")["width"] > _root(page)["width"] - 80

    def test_folding_is_no_move_and_is_not_saved(self, page: Any) -> None:
        before = _make(page, THREE)
        page.evaluate("() => fold('b', true)")
        assert _tree(page) == before and _saved(page) == []


class TestTheShellFilesShipIt:
    def test_the_layout_is_a_file_of_the_shell(self) -> None:
        assert (STATIC / "layout.js").is_file()


class TestTheLayoutRuleOnTheServer:
    """``checked_layout(value)`` answers the layout, or raises ``ValueError`` naming WHERE in it the mistake is — the
    rule an app applies before it keeps a layout in its workspace."""

    @pytest.mark.parametrize(
        "layout",
        [
            {"pane": "a"},
            {"row": [{"pane": "a"}, {"pane": "b", "share": 2.5}]},
            {"column": [{"pane": "a", "share": 1}, {"row": [{"pane": "b"}, {"pane": "c"}], "share": 0.4}]},
            {"row": [{"pane": "a"}]},
        ],
    )
    def test_a_layout_is_answered_as_it_was_given(self, layout: Tree) -> None:
        from portail.layout import checked_layout

        assert checked_layout(layout) == layout

    @pytest.mark.parametrize(
        "layout, where",
        [
            ([], "layout"),
            ({"grid": [{"pane": "a"}]}, "layout"),
            ({"pane": "a", "size": 1}, "layout"),
            ({"pane": "a", "row": [{"pane": "b"}]}, "layout"),
            ({"row": []}, "layout.row"),
            ({"row": {"pane": "a"}}, "layout.row"),
            ({"pane": ""}, "layout.pane"),
            ({"pane": 3}, "layout.pane"),
            ({"row": [{"pane": "a"}, {"pane": "b", "share": 0}]}, "layout.row[1].share"),
            ({"row": [{"pane": "a"}, {"pane": "b", "share": -1}]}, "layout.row[1].share"),
            ({"row": [{"pane": "a"}, {"pane": "b", "share": True}]}, "layout.row[1].share"),
            ({"row": [{"pane": "a"}, {"pane": "b", "share": "1"}]}, "layout.row[1].share"),
            ({"row": [{"pane": "a"}, {"pane": "b", "share": math.nan}]}, "layout.row[1].share"),
            ({"row": [{"pane": "a"}, {"pane": "b", "share": math.inf}]}, "layout.row[1].share"),
            ({"row": [{"pane": "a"}, {"column": [{"pane": "b"}, {"pane": "a"}]}]}, "layout.row[1].column[1].pane"),
        ],
    )
    def test_a_mistake_is_refused_where_it_is(self, layout: Any, where: str) -> None:
        from portail.layout import checked_layout

        with pytest.raises(ValueError) as refusal:
            checked_layout(layout)
        assert str(refusal.value).startswith(where + ":") or str(refusal.value).startswith(
            where + " "
        ), f"the refusal names {where}: {refusal.value}"

    def test_a_pane_named_twice_is_refused_naming_it(self) -> None:
        from portail.layout import checked_layout

        with pytest.raises(ValueError, match="'a'"):
            checked_layout({"row": [{"pane": "a"}, {"pane": "a"}]})

    def test_the_place_named_is_the_callers(self) -> None:
        from portail.layout import checked_layout

        with pytest.raises(ValueError) as refusal:
            checked_layout({"row": []}, where="workspace.layout")
        assert str(refusal.value).startswith("workspace.layout.row")
