// The page's panes in ONE layout of rows and columns, each pane dragged by its handle to the left, right, top or
// bottom of another (portail.layout is the same shape rule on the server).
//
// A page loads this script after `portail/shell.js` and builds its layout once it knows what it kept:
//
//   const layout = Portail.layout(rootElement, {
//     default: {row: [{pane: "list"}, {pane: "picture"}]},   // what a page that never moved a pane shows
//     saved: theTreeItKept,                                    // or null; one that is no layout draws the default
//     save: (tree) => …,                                       // once per drop, once per border let go
//     shown: (name, element) => !element.hidden,               // optional: is the pane on screen
//     folded: (name, element) => false,                        // optional: folded to its head (a minimize button)
//     resized: () => …,                                        // optional: the panes' room may have changed
//     minPx: 60,                                               // optional: no border makes a pane smaller
//   });
//   layout.add("list", listElement, {pane: "picture", side: "left"});  // where it goes when the tree names it not
//   layout.refresh();   // after a pane opened or closed
//   layout.tree();      // the layout now
//
// A tree node is {pane: name} or {row: [nodes]} / {column: [nodes]}, each with an optional `share` — its room beside
// its siblings, 1 when not written. A pane is dragged by the element inside it marked `data-pane-handle`; a press that
// moves less than 4 px stays that element's click. The drop zone is the edge of the pane under the pointer NEAREST to
// it, and the dropped pane takes half that pane's room; within a few pixels of the whole layout's edge the zone is the
// whole layout's. The layout MOVES the page's own elements and never builds them again, and moves only what has to
// move, so what a pane holds — its scroll, a text typed, a picture drawn — survives.
//
// A pane the page says is not shown takes no room and keeps its place in the tree. One the page has put somewhere
// else (inside another pane, say) is left there until it is shown again. A pane the page says is FOLDED stays in its
// place at the size of what it still shows, and a border beside it moves the room between the open panes around it.
// A border is a separator a keyboard reaches: focused, its arrow keys move it 20 px (100 px with shift).
"use strict";

(() => {
  const Portail = window.Portail || (window.Portail = {});
  const SPLITS = ["row", "column"];
  const SIDES = { left: ["row", true], right: ["row", false], above: ["column", true], below: ["column", false] };
  const CLICK_PX = 4;   // a press moving less is the handle's own click
  const EDGE_PX = 8;    // this close to the whole layout's edge, the drop is the whole layout's
  const KEY_PX = 20;    // what an arrow key moves a focused border (shift: five times as far)

  // ---- the tree: {kind: "pane", name, share} | {kind: "row" | "column", children, share} ------------------------

  // The shape rule (portail.layout.checked_layout's): null when `value` is no layout.
  function parsed(value) {
    const seen = new Set();
    const walk = (node) => {
      if (!node || typeof node !== "object" || Array.isArray(node)) return null;
      const keys = Object.keys(node);
      if (keys.some((key) => !["pane", "row", "column", "share"].includes(key))) return null;
      const kinds = ["pane", ...SPLITS].filter((kind) => kind in node);
      if (kinds.length !== 1) return null;
      let share = 1;
      if ("share" in node) {
        share = node.share;
        if (typeof share !== "number" || !Number.isFinite(share) || share <= 0) return null;
      }
      const kind = kinds[0];
      if (kind === "pane") {
        const name = node.pane;
        if (typeof name !== "string" || !name || seen.has(name)) return null;
        seen.add(name);
        return { kind, name, share };
      }
      if (!Array.isArray(node[kind]) || !node[kind].length) return null;
      const children = [];
      for (const child of node[kind]) {
        const made = walk(child);
        if (!made) return null;
        children.push(made);
      }
      return { kind, children, share };
    };
    return walk(value);
  }

  // The tree in its one form: a split of one child is that child (with the split's share), and a split inside a
  // split of its own kind joins it — each of its children keeping the room it had.
  function normal(node) {
    if (node.kind === "pane") return node;
    const children = [];
    for (const child of node.children.map(normal)) {
      if (child.kind === node.kind) {
        const total = child.children.reduce((sum, grand) => sum + grand.share, 0);
        for (const grand of child.children) {
          grand.share = (child.share * grand.share) / total;
          children.push(grand);
        }
      } else {
        children.push(child);
      }
    }
    node.children = children;
    if (children.length === 1) {
      children[0].share = node.share;
      return children[0];
    }
    return node;
  }

  const tidy = (share) => Number(share.toPrecision(4));

  function written(node, top) {
    const out = node.kind === "pane" ? { pane: node.name } : { [node.kind]: node.children.map((c) => written(c, false)) };
    if (!top) out.share = tidy(node.share);
    return out;
  }

  function leaves(node, into = []) {
    if (node.kind === "pane") into.push(node);
    else node.children.forEach((child) => leaves(child, into));
    return into;
  }

  function parentOf(root, wanted) {
    if (root.kind === "pane") return null;
    for (const child of root.children) {
      if (child === wanted) return root;
      const found = parentOf(child, wanted);
      if (found) return found;
    }
    return null;
  }

  Portail.layout = function layout(root, options) {
    const opts = options || {};
    const minPx = opts.minPx || 60;
    const isShown = opts.shown || ((_name, el) => !el.hidden);
    const isFolded = opts.folded || (() => false);
    const save = opts.save || (() => {});
    const resized = opts.resized || (() => {});
    const panes = new Map();       // name → the page's element
    const names = new Map();       // the page's element → name
    const styled = new Set();      // the elements this layout set a size on
    let tree = parsed(opts.saved);
    tree = tree ? normal(tree) : (parsed(opts.default) && normal(parsed(opts.default)));

    root.classList.add("portail-layout");

    const leafOf = (name) => (tree ? leaves(tree).find((leaf) => leaf.name === name) || null : null);
    const shown = (name) => panes.has(name) && isShown(name, panes.get(name));
    const anyShown = (node) => (node.kind === "pane" ? shown(node.name) : node.children.some(anyShown));
    // Folded: every shown pane under it is folded — it takes the size of what it shows, never a share of the room.
    const folded = (node) => (node.kind === "pane"
      ? shown(node.name) && !!isFolded(node.name, panes.get(node.name))
      : node.children.filter(anyShown).every(folded));
    // A pane this layout may put in its place: one on screen, or one still in a container of the layout's own (a
    // closed pane, hidden by the page where it is). Anywhere else, the page put it there.
    const ours = (el) => !!el.parentElement && (el.parentElement === root || el.parentElement.classList.contains("portail-split"));

    // Put `node` next to `target` on `side`: the target's place becomes a split of the drop's direction holding the
    // two evenly; `normal` then joins it into a parent of its own direction, where the two share the target's room.
    function putBeside(node, target, side) {
      const [kind, first] = SIDES[side] || SIDES.right;
      node.share = 1;
      if (!target) { tree = node; return; }
      const keep = target.share;
      target.share = 1;
      const split = { kind, children: first ? [node, target] : [target, node], share: keep };
      const parent = parentOf(tree, target);
      if (parent) parent.children[parent.children.indexOf(target)] = split;
      else tree = split;
      tree = normal(tree);
    }

    function takeOut(node) {
      const parent = parentOf(tree, node);
      if (!parent) { tree = null; return; }
      parent.children.splice(parent.children.indexOf(node), 1);
      tree = normal(tree);
    }

    // ---- drawing: the tree into the page, moving an element only when it is not where it belongs ----------------

    // `on`: takes a share of the room; `fold`: takes what it shows; neither: takes no room — not even its padding, so
    // it is not displayed (and displayed again, by the layout, once it is shown).
    function size(el, share, on, fold) {
      el.style.flex = fold ? "0 0 auto" : on ? `${share} 1 0px` : "0 0 0px";
      el.style.minWidth = el.style.minHeight = on && !fold ? `${minPx}px` : "0px";
      if (!on && el.style.display !== "none") { el.style.display = "none"; el.dataset.portailHidden = ""; }
      if (on && "portailHidden" in el.dataset) { el.style.display = ""; delete el.dataset.portailHidden; }
      styled.add(el);
    }

    function place(container, items) {
      items.forEach((el, index) => {
        if (container.children[index] !== el) container.insertBefore(el, container.children[index] || null);
      });
      // What is left after them: the layout's own elements go; the page's own are its business.
      for (const el of [...container.children].slice(items.length)) {
        if (el.classList.contains("portail-split") || el.classList.contains("portail-border")) el.remove();
      }
    }

    function borderBefore(child, split) {
      if (!child.border) {
        child.border = document.createElement("div");
        child.border.className = "portail-border";
        child.border.title = "drag to move the room between these two panes — double-click shares it evenly";
        child.border.tabIndex = 0;
        child.border.setAttribute("role", "separator");
        child.border.addEventListener("pointerdown", (e) => dragBorder(e, child.border));
        child.border.addEventListener("dblclick", () => evenBorder(child.border));
        child.border.addEventListener("keydown", (e) => keyBorder(e, child.border));
      }
      child.border.dataset.split = split.kind;
      // A row's border stands upright between its panes; a column's lies across.
      child.border.setAttribute("aria-orientation", split.kind === "row" ? "vertical" : "horizontal");
      child.border.portail = { split, child };
      return child.border;
    }

    function build(node) {
      if (node.kind === "pane") {
        const el = panes.get(node.name);
        if (!el) return null;
        const on = shown(node.name);
        if (!on && !ours(el)) {
          if (styled.has(el)) {
            el.style.flex = el.style.minWidth = el.style.minHeight = "";
            if ("portailHidden" in el.dataset) { el.style.display = ""; delete el.dataset.portailHidden; }
            styled.delete(el);
          }
          return null;
        }
        size(el, node.share, on, on && folded(node));
        return el;
      }
      if (!node.el) {
        node.el = document.createElement("div");
        node.el.className = "portail-split";
      }
      node.el.dataset.split = node.kind;
      const items = [];
      let before = false;   // a shown pane sits before this child in the split
      node.children.forEach((child, index) => {
        const on = anyShown(child);
        if (index > 0) {
          const border = borderBefore(child, node);
          border.style.display = on && before ? "" : "none";
          items.push(border);
        }
        const el = build(child);
        if (el) items.push(el);
        before = before || on;
      });
      place(node.el, items);
      const on = anyShown(node);
      node.el.style.display = on ? "" : "none";
      size(node.el, node.share, on, on && folded(node));
      return node.el;
    }

    function draw() {
      const top = tree ? build(tree) : null;
      place(root, top ? [top] : []);
      if (top) top.style.flex = "1 1 0px";
      resized();
    }

    // ---- the borders: room moved between the two open panes beside one -----------------------------------------

    // The two panes a border moves room between: the nearest open (shown, unfolded) one before it and the nearest at
    // or after it — a folded pane between them keeps its size.
    function neighbours(border) {
      const { split, child } = border.portail;
      const at = split.children.indexOf(child);
      const open = (node) => anyShown(node) && !folded(node);
      const prev = split.children.slice(0, at).reverse().find(open);
      const next = split.children.slice(at).find(open);
      return prev && next ? { split, prev, next } : null;
    }

    const elementOf = (node) => (node.kind === "pane" ? panes.get(node.name) : node.el);

    // What moving `border` by d px does, as a function — or null for a border with nothing to move. Only the room the
    // shares divide moves: a pane's padding and border are its own, whatever its share.
    function mover(border) {
      const pair = neighbours(border);
      if (!pair) return null;
      const row = pair.split.kind === "row";
      const across = row ? "width" : "height";
      const first = elementOf(pair.prev);
      const second = elementOf(pair.next);
      const fixed = (el) => {
        const s = getComputedStyle(el);
        const sides = row ? ["Left", "Right"] : ["Top", "Bottom"];
        return sides.reduce((sum, side) => sum + parseFloat(s["padding" + side]) + parseFloat(s["border" + side + "Width"]), 0);
      };
      const a = first.getBoundingClientRect()[across];
      const b = second.getBoundingClientRect()[across];
      const fa = fixed(first);
      const fb = fixed(second);
      const room = a - fa + (b - fb);
      const weight = pair.prev.share + pair.next.share;
      return (d) => {
        const now = Math.min(Math.max(a + d, minPx), a + b - minPx);
        pair.prev.share = (weight * (now - fa)) / room;
        pair.next.share = weight - pair.prev.share;
        size(first, pair.prev.share, true, false);
        size(second, pair.next.share, true, false);
        resized();
      };
    }

    function dragBorder(e, border) {
      if (e.button !== 0) return;
      const move = mover(border);
      if (!move) return;
      e.preventDefault();
      const axis = border.portail.split.kind === "row" ? "clientX" : "clientY";
      const start = e[axis];
      let moved = false;
      const step = (ev) => { moved = true; move(ev[axis] - start); };
      const up = () => {
        window.removeEventListener("pointermove", step);
        window.removeEventListener("pointerup", up);
        document.documentElement.classList.remove("portail-resizing");
        if (moved) save(written(tree, true));
      };
      document.documentElement.classList.add("portail-resizing");
      window.addEventListener("pointermove", step);
      window.addEventListener("pointerup", up);
    }

    function keyBorder(e, border) {
      const row = border.portail.split.kind === "row";
      const keys = row ? { ArrowLeft: -1, ArrowRight: 1 } : { ArrowUp: -1, ArrowDown: 1 };
      if (!(e.key in keys)) return;
      e.preventDefault();
      const move = mover(border);
      if (!move) return;
      move(keys[e.key] * KEY_PX * (e.shiftKey ? 5 : 1));
      save(written(tree, true));
    }

    function evenBorder(border) {
      const pair = neighbours(border);
      if (!pair) return;
      pair.prev.share = pair.next.share = (pair.prev.share + pair.next.share) / 2;
      draw();
      save(written(tree, true));
    }

    // ---- a pane dragged by its handle -------------------------------------------------------------------------------

    let zone = null;   // the one element showing where a drop would go
    function showZone(rect, side) {
      if (!zone) {
        zone = document.createElement("div");
        zone.className = "portail-drop";
        document.body.append(zone);
      }
      const [kind, first] = SIDES[side];
      const half = kind === "row" ? { width: rect.width / 2, height: rect.height } : { width: rect.width, height: rect.height / 2 };
      const left = kind === "row" && !first ? rect.left + rect.width / 2 : rect.left;
      const top = kind === "column" && !first ? rect.top + rect.height / 2 : rect.top;
      Object.assign(zone.style, { display: "block", left: `${left}px`, top: `${top}px`, width: `${half.width}px`, height: `${half.height}px` });
    }
    const hideZone = () => { if (zone) zone.style.display = "none"; };

    // Where a drop at (x, y) goes: {target, side}, target null for the whole layout — or null for nowhere.
    function dropAt(x, y, dragged) {
      const whole = root.getBoundingClientRect();
      if (x < whole.left || x > whole.right || y < whole.top || y > whole.bottom) return null;
      const edges = { left: x - whole.left, right: whole.right - x, above: y - whole.top, below: whole.bottom - y };
      const nearest = Object.keys(edges).reduce((a, b) => (edges[b] < edges[a] ? b : a));
      if (edges[nearest] <= EDGE_PX) return { target: null, side: nearest, rect: whole };
      for (const leaf of leaves(tree)) {
        if (!shown(leaf.name)) continue;
        const el = panes.get(leaf.name);
        if (!ours(el)) continue;
        const r = el.getBoundingClientRect();
        if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
        if (leaf === dragged) return null;
        const fx = (x - r.left) / (r.width || 1);
        const fy = (y - r.top) / (r.height || 1);
        const near = { left: fx, right: 1 - fx, above: fy, below: 1 - fy };
        const side = Object.keys(near).reduce((a, b) => (near[b] < near[a] ? b : a));
        return { target: leaf, side, rect: r };
      }
      return null;
    }

    function dragPane(e) {
      if (e.button !== 0 || !tree) return;
      const handle = e.target.closest && e.target.closest("[data-pane-handle]");
      if (!handle) return;
      let el = handle;
      while (el && !names.has(el)) el = el.parentElement;
      if (!el) return;
      const leaf = leafOf(names.get(el));
      if (!leaf || !shown(leaf.name) || !ours(el)) return;
      const x0 = e.clientX;
      const y0 = e.clientY;
      let dragging = false;
      let cancelled = false;
      let target = null;
      const end = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        window.removeEventListener("keydown", key, true);
        document.documentElement.classList.remove("portail-dragging");
        el.classList.remove("portail-dragged");
        hideZone();
      };
      const move = (ev) => {
        if (cancelled) return;
        if (!dragging) {
          if (Math.hypot(ev.clientX - x0, ev.clientY - y0) < CLICK_PX) return;
          dragging = true;
          document.documentElement.classList.add("portail-dragging");
          el.classList.add("portail-dragged");
          const selection = window.getSelection && window.getSelection();
          if (selection) selection.removeAllRanges();
        }
        ev.preventDefault();
        target = dropAt(ev.clientX, ev.clientY, leaf);
        if (target) showZone(target.rect, target.side);
        else hideZone();
      };
      const key = (ev) => {
        if (ev.key !== "Escape" || !dragging) return;
        ev.preventDefault();
        ev.stopPropagation();
        cancelled = true;
        target = null;
        end();
      };
      const up = () => {
        end();
        if (!dragging) return;   // a click: the handle's own
        // The click that follows the release belongs to no button: the press was a drag.
        const swallow = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
        window.addEventListener("click", swallow, { capture: true, once: true });
        setTimeout(() => window.removeEventListener("click", swallow, { capture: true }), 0);
        if (!target || cancelled) return;
        takeOut(leaf);
        putBeside(leaf, target.target || tree, target.side);
        draw();
        save(written(tree, true));
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("keydown", key, true);
    }

    document.addEventListener("pointerdown", dragPane);
    // A handle that is a link or a picture would start the browser's own drag of it.
    document.addEventListener("dragstart", (e) => {
      if (e.target.closest && e.target.closest("[data-pane-handle]")) e.preventDefault();
    });

    draw();
    return {
      add(name, el, where) {
        panes.set(name, el);
        names.set(el, name);
        if (!leafOf(name)) {
          const place = where || {};
          const beside = place.pane ? leafOf(place.pane) : null;
          putBeside({ kind: "pane", name, share: 1 }, beside || tree, place.side || "right");
        }
        draw();
      },
      refresh: draw,
      tree: () => (tree ? written(tree, true) : null),
    };
  };
})();
