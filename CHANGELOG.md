# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0a2] — 2026-11-27

### Added
- **The panes' layout** (`portail/layout.js`, `Portail.layout`): every pane of a page in one layout of rows and
  columns. A pane is dragged by its handle to the left, right, top or bottom of another pane, or along an edge of the
  whole layout; the border between two panes moves the room between them, and a double-click shares it evenly. A
  closed pane keeps its place, a folded one (a minimize button) keeps its place at the size of its head, and a focused
  border moves with the arrow keys. The page keeps the layout where it likes and hands it back on the next load;
  `portail.layout.checked_layout` is its shape rule on the server.

### Changed
- **portail requires the newest releases of its own dependencies** — confluid 0.5.0 (was `>=0.4.0`), liquifai
  0.4.0 (was `>=0.3.0`) and loggair 0.3.0 (was `>=0.2.0`); the suite is tested against them.

## [0.1.0a1]

The first release.

### Added
- **The seam**: an app says who it is (`AppSpec`), what it serves (a workspace implementing `Served` — a workspace
  file, or a folder with `AppSpec(workspace="folder")`) and what it does at the moments serving passes through
  (`Hooks`).
- **Serving and the lifecycle commands** on the app's own command line (`add_lifecycle_commands`): `open` (the
  default), `workspace <type>`, `stop`, `restart` and `list`, with `--port`, `--auto-port`, `--background` and
  `--browser`. A server is found by what it answers, never by its port alone; a stop signals only the processes
  listening on the ports it stops and never a program that is not the app. A restart continues a never-saved
  workspace when the app says how. A workspace can carry serving options of the app's own.
- **The page's shell** (`mount_shell`): `/api/meta`, the ⏻ and ↻ buttons, and every open page told when its server
  goes or restarts.
- **Jobs** (`portail.jobs`): background work in a thread of its own, with its count, a cooperative stop, one job
  per key, and the list a page polls.
- **The MCP server base** (`portail.mcp`, the `[mcp]` extra): an LLM-safe server whose tools talk to the running app,
  and the choice of which running server a tool reaches.
- **The filter language** a records list is searched with (`portail.query`), the app's flag words declared.
- **The structural protocols** of a records viewer (`portail.protocols`).
- **A workspace may name the page its tab opens on** — an optional `page` attribute (`?item=7`): `--browser` opens
  `http://127.0.0.1:8790/?item=7` in place of the address alone. A workspace without it opens what it always did.
- **The licence** is the PolyForm Noncommercial License 1.0.0 (`LICENSE`, `pyproject.toml`): free for personal, academic
  and other noncommercial use; commercial use needs a separate licence from the author.
