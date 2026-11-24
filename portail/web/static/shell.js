// The page's shell (portail.web.shell): the ⏻ and ↻ buttons, and what a page shows when its server goes.
//
// A host page loads this script (`<script src="portail/shell.js"></script>` — relative, so a path proxy keeps
// working) and calls `Portail.start({buttons: "#top"})`: the page then knows its server (`Portail.meta`, from
// `api/meta`), carries ⏻ (stop the server, close the tab) and ↻ (restart it with the code on disk now, reload the
// tab) in the element it names, and hears when the server goes — `<name> stop`, Ctrl+C, the terminal closed, ⏻ or ↻
// in another tab.
//
// What a host page may set before it starts:
//   Portail.beforeLeaving = async (what) => true   // "exit" | "restart": false keeps the server (its own question)
//   Portail.toast = (message, isError) => …        // how a refusal is said (default: a toast at the bottom)
"use strict";

(() => {
  const script = document.currentScript;
  // Every address is resolved against the folder THIS script is served from, one level up: `…/portail/shell.js` →
  // `…/` — the app's own root, under whatever prefix a path proxy serves it.
  const base = new URL("../", script ? script.src : document.baseURI);

  const Portail = {
    base,
    meta: null,
    leaving: false,          // this page knows its server is going — nothing it hears changes it any more
    serverListening: false,  // the stream has answered at least once
    NOT_ANSWERING_MS: 3000,
    RESTART_WAIT_MS: 60000,  // the restart's stop (up to 5 s) and its wait for the new server (up to 30 s), with room
    lostTimer: null,
    beforeLeaving: async () => true,
  };

  Portail.url = (path) => new URL(path, base).href;

  Portail.api = async (path, opts) => {
    const res = await fetch(Portail.url(path), opts);
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error || msg; } catch (e) { /* not json */ }
      throw new Error(`${res.status} ${msg}`);
    }
    return res.headers.get("content-type")?.includes("json") ? res.json() : res;
  };

  Portail.postJson = (path, body) => Portail.api(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });

  Portail.escape = (text) => String(text).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  Portail.name = () => (Portail.meta && Portail.meta.app) || document.title || "the app";

  Portail.toast = (message, isError = false) => {
    const toast = document.createElement("div");
    toast.className = isError ? "portail-toast error" : "portail-toast";
    toast.textContent = message;
    document.body.append(toast);
    setTimeout(() => toast.remove(), isError ? 8000 : 4000);
  };

  // Before ⏻ or ↻: what the stop loses. A SAVED workspace's unsaved changes (`dirty`), read fresh — a never-saved one
  // is kept by the stop when the app continues it, so nothing is asked for it; then the host page's own question.
  async function agreed(what) {
    const w = await Portail.api("api/workspace");
    if (w.path && w.dirty && !window.confirm(`${String(w.path).split(/[\\/]/).pop()} has unsaved changes — ${what} and lose them?`)) {
      return false;
    }
    return (await Portail.beforeLeaving(what)) !== false;
  }

  // ⏻: stop this server and close the tab. The server stops the way `<name> stop` stops it.
  Portail.exit = async () => {
    let said;
    try {
      if (!(await agreed("exit"))) return;
      said = await Portail.postJson("api/exit", {});
    } catch (e) { Portail.toast("exit failed: " + e.message, true); return; }
    Portail.leaving = true;  // the server's own `leaving`, which follows, changes nothing more here
    Portail.showExited(said);
  };

  // What every way out of the server leaves in a tab — ⏻, and any stop the server tells its pages about. The page is
  // replaced BEFORE window.close(): a browser closes only a tab a script opened or one with a single page in its
  // history (measured in Chromium), and a refused close must not leave a page that looks alive over a server that is
  // gone.
  Portail.showExited = (said) => {
    const name = Portail.escape(Portail.name());
    const again = said && said.kept_in
      ? `<code>${name} restart</code> continues this workspace; what you did in it is kept in <code>${Portail.escape(said.kept_in)}</code> ${Portail.escape(said.kept_until || "")}.`
      : `<code>${name} restart</code> starts it again.`;
    document.title = `${Portail.name()} — exited`;
    document.body.className = "exited";
    document.body.innerHTML = `<main id="exited"><h1>${name} has exited.</h1><p>You can close this tab — ${again}</p></main>`;
    window.close();
  };

  // ↻: restart this server with the code on disk now, and reload the tab. The server launches
  // `<name> restart <port> --background` — the command a person types — and a FRESH process serves the same workspace
  // on the same port. Asked first, as by ⏻.
  Portail.restart = async () => {
    let said;
    try {
      if (!(await agreed("restart"))) return;
      said = await Portail.postJson("api/restart", {});
    } catch (e) { Portail.toast("restart failed: " + e.message, true); return; }
    Portail.leaving = true;
    await Portail.awaitRestart(said);
  };

  // A restart from ↻ or from `<name> restart` (the server's `leaving` says `restart`): nothing of this page survives
  // it, so it says what is happening, asks api/meta until a server with ANOTHER process id than `said.pid` answers, and
  // reloads. `said.log` — where ↻'s restart writes what it prints — is named when no server comes back.
  Portail.awaitRestart = async (said) => {
    const name = Portail.name();
    const waited = Portail.RESTART_WAIT_MS;
    document.title = `${name} — restarting`;
    document.body.className = "exited";
    document.body.innerHTML = `<main id="restarting"><h1>${Portail.escape(name)} is restarting…</h1><p>This tab reloads once the new server answers.</p></main>`;
    const deadline = Date.now() + waited;
    while (Date.now() < deadline) {
      try {
        const meta = await (await fetch(Portail.url("api/meta"), { cache: "no-store" })).json();
        if (meta.pid && meta.pid !== said.pid) { location.reload(); return; }
      } catch (e) { /* the old server is gone and the new one does not answer yet */ }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    const main = document.getElementById("restarting");
    main.className = "failed";
    const command = `<code>${Portail.escape(name)} restart</code>`;
    const where = said.log
      ? `What the restart printed is in <code>${Portail.escape(said.log)}</code> — ${command} in a terminal tries again.`
      : `${command} in a terminal tries again.`;
    main.innerHTML = `<h1>${Portail.escape(name)} did not come back in ${Math.round(waited / 1000)} s</h1><p>${where}</p>`;
  };

  // The server's word to its pages: every tab holds the server's event stream through ONE shared worker
  // (`server-events.js` says why one). `exit` shows what ⏻ shows and closes the tab, `restart` waits for the new
  // server and reloads. A stream that drops WITHOUT a word (a kill -9, a crash, a blip) is not a stop: after
  // `NOT_ANSWERING_MS` the page says the server is not answering, with a Retry, and a stream that comes back takes it
  // away.
  Portail.listenToServer = (ownWorker = false) => {
    let channel;
    let shared = false;
    const events = encodeURIComponent(Portail.url("api/events"));
    const worker = Portail.url(`portail/server-events.js?events=${events}`);
    try {
      if (!ownWorker && window.SharedWorker) {
        channel = new SharedWorker(worker, { name: `${Portail.name()}-server-events` }).port;
        channel.start();
        shared = true;
      } else if (window.Worker) {
        channel = new Worker(worker);
      } else {
        return;  // a page that cannot listen: the next click says the server is gone
      }
    } catch (e) { console.warn(`${Portail.name()} cannot listen to its server:`, e); return; }
    let opened = false;
    let checked = false;
    channel.onmessage = async (e) => {
      const said = e.data;
      if (said && said.type === "open") opened = true;
      // A shared worker that never reached the server while this page does is one the browser keeps off the network
      // (measured in an embedded browser pane: every request from a SharedWorker failed while the page's own and a
      // dedicated worker's answered) — this tab then listens through its own.
      if (shared && !opened && !checked && said && said.type === "lost") {
        checked = true;
        if (await Portail.serverAnswers()) {
          channel.onmessage = null;
          channel.postMessage("bye");
          Portail.listenToServer(true);
          return;
        }
      }
      Portail.heardFromServer(said);
    };
    window.addEventListener("pagehide", () => channel.postMessage("bye"));
  };

  Portail.serverAnswers = async () => {
    try { return (await fetch(Portail.url("api/meta"), { cache: "no-store" })).ok; } catch (e) { return false; }
  };

  Portail.heardFromServer = (said) => {
    if (Portail.leaving || !said) return;
    if (said.type === "open") {
      Portail.serverListening = true;
      clearTimeout(Portail.lostTimer);
      Portail.lostTimer = null;
      // Another process answers than the one that served this page: it came back while the page was not told.
      if (Portail.meta && Portail.meta.pid && said.pid && said.pid !== Portail.meta.pid) { location.reload(); return; }
      Portail.showNotAnswering(false);
    } else if (said.type === "lost") {
      if (!Portail.lostTimer) {
        Portail.lostTimer = setTimeout(() => { Portail.lostTimer = null; Portail.showNotAnswering(true); }, Portail.NOT_ANSWERING_MS);
      }
    } else if (said.type === "leaving") {
      Portail.leaving = true;
      clearTimeout(Portail.lostTimer);
      if (said.why === "restart") Portail.awaitRestart({ pid: said.pid, log: "" });
      else Portail.showExited(said);
    }
  };

  // The bar over the page while its server does not answer — fixed, so it moves nothing under it.
  Portail.showNotAnswering = (shown) => {
    let bar = document.getElementById("not-answering");
    if (!bar) {
      if (!shown) return;
      bar = document.createElement("div");
      bar.id = "not-answering";
      bar.innerHTML = `<span class="said"></span> <button type="button">Retry</button>`;
      bar.querySelector("button").onclick = Portail.retryServer;
      document.body.append(bar);
    }
    if (shown) bar.querySelector(".said").textContent = `${Portail.name()} is not answering — it may have stopped, or the connection dropped.`;
    bar.classList.toggle("hidden", !shown);
  };

  Portail.retryServer = async () => {
    const said = document.querySelector("#not-answering .said");
    try {
      const meta = await (await fetch(Portail.url("api/meta"), { cache: "no-store" })).json();
      if (Portail.meta && meta.pid && meta.pid !== Portail.meta.pid) { location.reload(); return; }
      Portail.showNotAnswering(false);
    } catch (e) {
      const name = Portail.name();
      said.textContent = `${name} is still not answering (asked at ${new Date().toLocaleTimeString()}) — '${name}' in a terminal starts it.`;
    }
  };

  // ↻ and ⏻, in that order, at the end of the element the host page names.
  Portail.mountButtons = (container) => {
    const place = typeof container === "string" ? document.querySelector(container) : container;
    if (!place) return;
    const button = (id, label, title, onclick) => {
      const b = document.createElement("button");
      b.type = "button";
      b.id = id;
      b.className = "portail-button";
      b.textContent = label;
      b.title = title;
      b.onclick = onclick;
      return b;
    };
    const name = Portail.name();
    place.append(
      button("ws-restart", "↻", `restart ${name} with the code on disk now (${name} restart)`, Portail.restart),
      button("ws-exit", "⏻", `stop ${name} and close this tab (${name} stop)`, Portail.exit),
    );
  };

  // The page's start: its server's meta, the buttons, and the stream.
  Portail.start = async ({ buttons = null, listen = true } = {}) => {
    Portail.meta = await Portail.api("api/meta");
    if (buttons) Portail.mountButtons(buttons);
    if (listen) Portail.listenToServer();
    return Portail.meta;
  };

  window.Portail = Portail;
})();
