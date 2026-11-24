"""Fake servers and a fake machine for the lifecycle tests — no real process is listed or touched through them."""

import json
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from portail import serving
from portail.spec import Served

#: A process that binds no port — an MCP server of a client, say — whose life is its own: a stop never signals it.
OTHER_PROCESSES = {
    2400: "/usr/bin/python3.13 /Users/me/.venv/bin/demoapp-mcp",
    3372: "/usr/bin/python3.13 /Users/me/.venv/bin/demoapp-mcp",
}


def meta(path: str = "", template: str = "", state_dir: str = "", app: str = "demoapp") -> Dict[str, Any]:
    """What ``/api/meta`` of a server of the app answers."""
    return {"app": app, "title": "x", "workspace": {"path": path, "template": template, "state_dir": state_dir}}


def register(state: Path, port: int, path: str = "", template: str = "") -> None:
    """A server's registry entry, as the process serving it writes one."""
    directory = state / "servers"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{port}.json").write_text(
        json.dumps({"host": "127.0.0.1", "port": port, "path": path, "template": template, "pid": 1})
    )


def noting(into: List[Any], answer: Any) -> Any:
    """A stand-in that notes its first argument in ``into`` and answers ``answer``."""

    def note(first: Any, *rest: Any, **kwargs: Any) -> Any:
        into.append(first)
        return answer

    return note


def serves(monkeypatch: Any) -> List[Served]:
    """``Server.serve`` replaced: every workspace a start would serve, recorded instead."""
    served: List[Served] = []
    monkeypatch.setattr(serving.Server, "serve", lambda self: served.append(self.served))
    return served


class Fleet:
    """Fake running servers: the meta probe answers from ``alive``, ``stop_port`` pops the port."""

    def __init__(self, monkeypatch: Any, alive: Dict[int, Dict[str, str]]) -> None:
        self.alive = dict(alive)
        self.stopped: List[Tuple[str, int]] = []
        monkeypatch.setattr(serving, "meta_at", self._meta)
        monkeypatch.setattr(serving, "stop_port", self._stop)

    def _meta(self, host: str, port: int) -> Optional[Dict[str, Any]]:
        info = self.alive.get(port)
        return None if info is None else meta(**info)

    def _stop(self, name: str, port: int) -> str:
        self.stopped.append((name, port))
        self.alive.pop(port, None)
        return f"{name}: stopped (port {port})"

    @property
    def ports(self) -> List[int]:
        return [port for _name, port in self.stopped]


class Machine:
    """A fake process table: servers of the app LISTENING on ports, other processes on none.

    Everything a stop can reach goes through it: ``ss`` (absent), ``lsof`` and ``ps`` (``subprocess.run``) answer from
    it, ``os.kill`` signals it, and a port answers the meta probe while its listener lives. A command it does not know
    fails the test and a pid it does not know is never signalled. A server's listener pid is ``port * 10`` unless
    ``listeners`` says otherwise; a pid in ``ignores_term`` survives SIGTERM; a pid in ``exited`` is still listed by
    ``lsof`` but gone by the time it is signalled.
    """

    def __init__(
        self,
        monkeypatch: Any,
        servers: Dict[int, Dict[str, str]],
        listeners: Optional[Dict[int, List[int]]] = None,
        ignores_term: Iterable[int] = (),
        exited: Iterable[int] = (),
    ) -> None:
        self.servers = dict(servers)
        self.listeners = dict(listeners) if listeners is not None else {port: [port * 10] for port in servers}
        self.ignores_term, self.exited = set(ignores_term), set(exited)
        listed = {pid for pids in self.listeners.values() for pid in pids}
        self.alive: Set[int] = (listed - self.exited) | set(OTHER_PROCESSES)
        self.signalled: List[Tuple[int, int]] = []
        self.commands: List[str] = []
        monkeypatch.setattr(serving, "meta_at", self._meta)
        monkeypatch.setattr(serving.subprocess, "run", self._run)
        monkeypatch.setattr(serving.os, "kill", self._kill)

    def _meta(self, host: str, port: int) -> Optional[Dict[str, Any]]:
        info = self.servers.get(port)
        if info is None or not any(pid in self.alive for pid in self.listeners.get(port, [])):
            return None
        return meta(**info)

    def _run(self, argv: List[str], **kwargs: Any) -> SimpleNamespace:
        self.commands.append(argv[0])
        if argv[0] == "ss":  # this machine has no ss: the pid lookup falls back to lsof
            raise FileNotFoundError("ss")
        if argv[0] == "lsof":
            port = int(next(arg for arg in argv if arg.startswith("-iTCP:"))[len("-iTCP:") :])
            listening = [pid for pid in self.listeners.get(port, []) if pid in self.alive | self.exited]
            fields = "-Fpc" in argv
            return SimpleNamespace(
                stdout="".join(f"p{pid}\n" + ("cpython3.13\n" if fields else "") for pid in listening)
            )
        if argv[0] == "ps":
            servers = [f"{pid} /usr/bin/python3.13 /Users/me/.venv/bin/demoapp" for pid in self._listening()]
            others = [f"{pid} {args}" for pid, args in OTHER_PROCESSES.items() if pid in self.alive]
            return SimpleNamespace(stdout="\n".join(servers + others) + "\n")
        raise AssertionError(f"a stop ran {argv!r}")

    def _listening(self) -> List[int]:
        return [pid for pids in self.listeners.values() for pid in pids if pid in self.alive]

    def _kill(self, pid: int, sig: int) -> None:
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        if sig != 0:
            self.signalled.append((pid, sig))
            if not (sig == signal.SIGTERM and pid in self.ignores_term):
                self.alive.discard(pid)


def only_these_were_stopped(machine: Machine, capsys: Any, pids: List[int], out: str) -> None:
    """The stop signalled exactly ``pids`` (its listeners), left every other process running, never even listed
    processes to look for one, and printed exactly ``out``."""
    assert machine.signalled == [(pid, signal.SIGTERM) for pid in pids]
    assert set(OTHER_PROCESSES) <= machine.alive, "a process that binds no port is never signalled"
    assert "ps" not in machine.commands, "…nor looked for"
    assert capsys.readouterr().out == out
