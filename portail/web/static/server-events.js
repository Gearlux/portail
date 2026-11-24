// The server's word to its pages, ONE connection for every tab of this server (shell.js `Portail.listenToServer`).
//
// Run as a SharedWorker: a browser allows six HTTP/1.1 connections per server across ALL its tabs, so an
// EventSource per tab would hold one of them for good — six tabs and the page's own requests could not load at all.
// The worker keeps the one `api/events` stream and hands what it hears to every tab connected to it:
//   {type: "open", pid}      the stream answers (again) — the server with this process id is up
//   {type: "lost"}           the stream dropped without a word; the browser tries again every second
//   {type: "leaving", why, pid, kept_in, kept_until}   the server said it is going; the stream is closed
// A browser without SharedWorker — or whose shared workers cannot reach the network — runs this same script as a
// dedicated Worker, one per tab (shell.js `Portail.listenToServer`).
//
// Where the stream is: the `events` query parameter the page put on this script's address (resolved against the
// page), else one level up from this file (`…/portail/server-events.js` → `…/api/events`) — relative either way, so a
// path proxy serving the app under a prefix keeps working.
let source = null;
let last = null;  // what a tab connecting now is told first
const ports = new Set();
const EVENTS = new URL(self.location.href).searchParams.get("events") || "../api/events";

function tell(message) {
  last = message;
  for (const port of ports) port.postMessage(message);
}

function listen() {
  source = new EventSource(EVENTS);
  source.addEventListener("hello", (e) => tell({ type: "open", ...JSON.parse(e.data) }));
  source.addEventListener("leaving", (e) => {
    tell({ type: "leaving", ...JSON.parse(e.data) });
    source.close();
    source = null;  // a tab that connects after (a reload) opens a fresh stream on whatever serves then
  });
  source.onerror = () => { if (source) tell({ type: "lost" }); };
}

function join(port) {
  ports.add(port);
  port.onmessage = (e) => {
    if (e.data !== "bye") return;
    ports.delete(port);
    if (!ports.size && source) { source.close(); source = null; }  // no tab listens: the server gets its thread back
  };
  if (!source) { last = null; listen(); }
  else if (last) port.postMessage(last);
}

if (typeof SharedWorkerGlobalScope !== "undefined" && self instanceof SharedWorkerGlobalScope) {
  self.onconnect = (e) => { const port = e.ports[0]; join(port); port.start(); };
} else {
  join(self);
}
