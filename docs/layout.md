# The panes' layout

A page shows several panes: a list, a picture, a few boxes of details. `portail/layout.js` puts them all in one
layout of rows and columns, and lets the person drag any pane to the left, right, top or bottom of another one. The
page keeps the layout where it likes — in its workspace, in the browser — and hands it back on the next load.

## Build one

Load the script after the shell's, and build the layout once the page knows what it kept:

```html
<script src="portail/shell.js"></script>
<script src="portail/layout.js"></script>
```

```js
const layout = Portail.layout(document.getElementById("panes"), {
  default: { row: [{ pane: "list", share: 1 }, { pane: "picture", share: 3 }] },
  saved: keptLayout,                     // null on a first visit
  save: (tree) => keep(tree),            // once per drop, once per border let go
  shown: (name, el) => !el.hidden,       // optional — the default reads `hidden`
  folded: (name, el) => el.classList.contains("minimized"),   // optional — folded to its head
  resized: () => redraw(),               // optional — the panes' room may have changed
});
layout.add("list", document.getElementById("list"), { pane: "picture", side: "left" });
layout.add("picture", document.getElementById("picture"));
```

- `default` is what a page that never moved a pane shows. A `saved` layout that is no layout (a browser's storage
  holding anything else) draws the default.
- `add(name, element, where)` hands the layout a pane. `where` is used only when the layout does not name the pane:
  `{pane, side}` puts it on that side of that pane, and a `where` naming no pane the layout knows (or none at all) puts
  it along that side of the whole layout (`right` when not said).
- `shown` says whether a pane is on screen: one that is not takes no room (it is not displayed at all) and keeps its
  place. `folded` says whether a shown pane is folded to its head — a minimize button: it stays in its place at the
  size of what it still shows.
- `refresh()` after a pane opened, closed, folded or unfolded. `tree()` answers the layout now.

A pane's handle is the element inside it marked `data-pane-handle` — its title, or a name chip:

```html
<section id="list"><span data-pane-handle>list</span> …</section>
```

A press on the handle that moves less than 4 px stays the handle's own click, so a chip that closes its pane keeps
doing that.

## What the layout is

A tree whose ends name panes:

```json
{"row": [
  {"pane": "list", "share": 0.22},
  {"column": [{"pane": "picture", "share": 3}, {"pane": "notes", "share": 1}], "share": 0.78}
]}
```

- `row`: its children side by side, left to right. `column`: stacked, top to bottom. `pane`: one pane, by the name the
  page gave it.
- `share`: a node's room beside its siblings, 1 when not written. A share, never pixels: a wider window widens every
  pane.
- Each pane is named once.

`portail.layout.checked_layout(value)` is the same rule on the server, for an app that keeps the layout in its
workspace: it answers a copy, or raises `ValueError` saying where the mistake is (`layout.row[1].share: a share is a
number above 0 — got 0`).

## What the person can do

| Gesture | What happens |
|---|---|
| drag a pane's handle onto another pane | the zone nearest the pointer — that pane's left, right, top or bottom half — is shown; let go, and the pane takes that half of its room |
| drag it right to the edge of the whole layout | the pane goes along that whole edge |
| Escape while dragging, or let go outside the layout or over its own pane | nothing moves |
| drag a border between two panes | the room moves between those two — a folded pane between them keeps its size; no pane gets smaller than 60 px (`minPx`), padding included |
| focus a border (Tab), then an arrow key | it moves 20 px, 100 px with shift (← → between panes side by side, ↑ ↓ between stacked ones) |
| double-click a border | the two panes beside it share their room evenly |

A row or column left with one pane by a move dissolves into its parent. A closed pane takes no room and keeps its
place: opened again, it is where it was. A pane the page has put somewhere else for a while — inside another pane, say
— is left there until the page says it is shown again. A name the layout holds and the page never adds (a pane this
page lacks) takes no room and stays in the tree.

The layout moves the page's own elements and never builds them again, so what a pane holds — a text typed, a picture
drawn — survives a move. It moves an element only when its place changes: opening or closing a pane moves none, and a
move re-places only the panes whose place it changed (an element put elsewhere in the page starts scrolled to its
top).
