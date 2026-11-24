# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- **The MCP server base** (`portail.mcp`, the `[mcp]` extra): an LLM-safe server whose tools talk to the running app,
  and the choice of which running server a tool reaches.
- **The filter language** a records list is searched with (`portail.query`), the app's flag words declared.
- **The structural protocols** of a records viewer (`portail.protocols`).
