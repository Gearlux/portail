# Serving, stopping and restarting

What the lifecycle commands an app gets from portaide do, in the words of the person typing them. `<name>` is the
app's own command (`AppSpec.name`).

## Starting

- `<name>` serves an EMPTY workspace; `<name> <workspace.yaml>` opens a saved one; `<name> workspace <type>` opens a
  NEW workspace of a type (when the app has types).
- The command prints nothing alarming when the app already answers on the address: it says
  `<name> is already running — serving …`, the URL alone on its line (so a terminal makes it clickable), and how to
  stop or restart it.
- **A saved workspace keeps its port.** When ANOTHER workspace of the app answers there, the command asks
  `… launch <it> on port N? [Y/n]` (Enter = yes); without a terminal it refuses and names `--auto-port`. A port held by
  a program that is not the app is one `Error:` line naming it — `--auto-port` goes around it.
- **A new type workspace finds its own port** — whoever holds the default one — and asks nothing, unless `--port N`
  names the port (then it is not moved unless `--auto-port` is typed too) or `--auto-port false` turns it off.
- `--port N` serves on N; anything that is not a port number is one located error.
- `--background` / `-b` serves in a detached process and gives the terminal back once the server ANSWERS: the report
  names the address, the pid and the log file. A child that dies before it answers is an error carrying its own last
  output; one still coming up after 30 s is said as such. When the same workspace already answers, no child is
  started: the already-running report is printed.
- `--browser new` (the default of the serving commands) opens the default browser once a server THIS command started
  answers; `always` also opens the one already running; `never` opens none. A machine without a browser gets one line
  naming the address. A background child never opens a tab: the terminal does, once the child answers.

## Listing

`<name> list` prints one line per running server, lowest port first: its port, what it serves (its file, `the <type>
type workspace`, or `an empty workspace`) and its URL, last. Only servers that ANSWER as the app are listed; a stale
registry entry is dropped.

## Stopping

- `<name> stop` stops the one running server; with several it lists them and asks which (`a` = all, a number, `q` =
  none). Without a terminal it refuses and names `--all`.
- `<name> stop <file>`, `<name> stop <type>` and `<name> stop <port>` stop the server SERVING that — wherever it runs,
  a server moved by `--auto-port` included. A port names a server exactly as its workspace does; a file or type
  spelled like a number wins.
- `<name> stop --all` stops every running server without asking — and with none answering says so and stops nothing.
- A stop signals only the processes LISTENING on the ports of the servers it stops: SIGTERM, then SIGKILL after five
  seconds. A process that binds no port (an MCP server of a client, say) is never touched.
- **A stop never ends something that is not the app.** When nothing the registry knows serves what was named, the stop
  asks the workspace's own port: a server of the app answering as that workspace is stopped; one serving something
  else is refused, naming both and the command that stops the one running; a listener that does not answer as the app
  is refused — `port N is held by something that is not <name> (pid … ) — not stopping it`. A server too stuck to
  answer its status check is refused the same way: end it with `kill <pid>`.
- When the lookup of who listens cannot be made (no `ss`, a hung `lsof`), the stop says so — it never claims the port
  is free.

## Restarting

- `<name> restart` serves the workspace served LAST again (a file, a type, or an empty one) — the dev loop: a fresh
  process picks up changed code. `<name> restart <file|type|port>` names another; `--port N` moves it.
- `restart <port>` opens what the LIVE server on that port serves, keeps that port, and refuses before stopping
  anything when the file it serves is gone, or when a program that is not the app holds the port (the same words a
  stop gives).
- `restart` defaults to `--browser never`: the tab of the server it stopped is usually still open — and reloads.
- **A never-saved workspace is CONTINUED** (when the app keeps one, `Hooks.continued`): bare (when the last served
  workspace was a never-saved one), by its port, or by the type of the one running or served last, it comes back in
  its own folder with what it held. A server still running is stopped first — it keeps its work on its way out — and
  one that is not running comes back where it was served, unless a SAVED workspace serves that port now: then the
  restart refuses, naming both and the `--port` that continues it on a free port. After a Save the older never-saved
  workspaces are no longer "the one served last".
- `stop` of a never-saved workspace with work says where it is kept and which restart continues it; a fresh
  never-saved start while one with work is left behind says so, and starts.

## The page

- ⏻ stops the server the way `<name> stop` does and closes the tab (a browser that refuses to close it shows
  `<name> has exited.` with how to start it again). ↻ restarts it through `<name> restart <port> --background` and
  reloads the tab once the fresh server answers; when none comes back within a minute the page names the restart's
  log. Both ask first when a SAVED workspace has unsaved changes.
- Every open page hears when its server goes — a stop, Ctrl+C, the terminal closed, ⏻ or ↻ in another tab: a stop
  shows what ⏻ shows, a restart waits for the new server and reloads. A server that dies without a word (kill -9, a
  crash) shows `<name> is not answering` with a Retry after three seconds, and the bar goes when it answers again.
- All tabs of one server share ONE connection for this (a browser allows six per server).

## Where things are

- The registry and the remembered workspace: `~/.<name>/` (`servers/<port>.json`, `last-workspace`,
  `unsaved-workspaces.json`), or the app's `AppSpec.state_folder`.
- A background server's log: `$LOGGAIR_DIR/<name>.log`; the page's ↻: `$LOGGAIR_DIR/<name>-restart.log`, both named
  in full in what is printed.
