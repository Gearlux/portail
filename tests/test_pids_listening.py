"""``pids_listening``: who LISTENS on a port, found without depending on ``lsof`` alone.

Measured on a machine where ``lsof`` hangs (a stuck mount): ``timeout 20 lsof -nP -iTCP:8792 -sTCP:LISTEN -Fp`` never
returned while ``ss`` answered at once, and a stop printed ``nothing listening on port 8792`` for a server that was up.
``ss`` is asked first; ``lsof`` is the fallback; when no tool can answer, the stop says so instead of claiming the port
is free.
"""

import subprocess
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from portail import serving
from portail.errors import ServeError

_SS_LINE = 'LISTEN 0      128        127.0.0.1:8792       0.0.0.0:*    users:(("python",pid=2425975,fd=24))\n'
_SS_LINE_NO_PID = "LISTEN 0      128        127.0.0.1:8792       0.0.0.0:*\n"


def _tools(monkeypatch: pytest.MonkeyPatch, answers: Dict[str, Any]) -> List[str]:
    """Replace ``subprocess.run``: ``answers`` maps a tool to its stdout, an exception to raise, or absent."""
    asked: List[str] = []

    def run(argv: List[str], **kwargs: Any) -> SimpleNamespace:
        asked.append(argv[0])
        answer = answers.get(argv[0], FileNotFoundError(argv[0]))
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(stdout=answer, returncode=0)

    monkeypatch.setattr(serving.subprocess, "run", run)
    return asked


def test_ss_names_the_listener_without_asking_lsof(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _tools(monkeypatch, {"ss": _SS_LINE})
    assert serving.pids_listening(8792) == [2425975]
    assert asked == ["ss"]


def test_ss_seeing_no_listener_means_nothing_listens(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _tools(monkeypatch, {"ss": ""})
    assert serving.pids_listening(8792) == []
    assert asked == ["ss"]


def test_a_listener_ss_shows_without_a_pid_is_looked_up_by_lsof(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _tools(monkeypatch, {"ss": _SS_LINE_NO_PID, "lsof": "p77\n"})
    assert serving.pids_listening(8792) == [77]
    assert asked == ["ss", "lsof"]


def test_lsof_answers_when_ss_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _tools(monkeypatch, {"lsof": "p5\np9\np5\n"})
    assert serving.pids_listening(8792) == [5, 9]
    assert asked == ["ss", "lsof"]


def test_lsof_answers_when_ss_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    _tools(monkeypatch, {"ss": subprocess.TimeoutExpired("ss", 5), "lsof": "p5\n"})
    assert serving.pids_listening(8792) == [5]


def test_lsof_answers_when_ss_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _tools(monkeypatch, {"ss": subprocess.CalledProcessError(1, "ss"), "lsof": "p5\n"})
    assert serving.pids_listening(8792) == [5]


def test_no_tool_able_to_answer_is_said_not_read_as_a_free_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _tools(monkeypatch, {"lsof": subprocess.TimeoutExpired("lsof", 5)})
    with pytest.raises(
        ServeError, match=r"could not look up what listens on port 8792 \(ss: not installed; lsof: .*timed out"
    ):
        serving.pids_listening(8792)


def test_a_listener_without_a_pid_twice_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    _tools(monkeypatch, {"ss": _SS_LINE_NO_PID, "lsof": subprocess.TimeoutExpired("lsof", 5)})
    with pytest.raises(ServeError, match="ss: showed a listener without its pid; lsof: timed out"):
        serving.pids_listening(8792)


def test_stop_reports_the_failed_lookup_instead_of_nothing_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    _tools(monkeypatch, {"lsof": subprocess.TimeoutExpired("lsof", 5)})
    with pytest.raises(ServeError, match="could not look up what listens on port 8792"):
        serving.stop_port("demoapp", 8792)


class TestWhoHoldsAPort:
    def test_lsof_names_the_holder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _tools(monkeypatch, {"lsof": "p2279\ncCode Helper (Plugin)\np2280\ncother\n"})
        assert serving.port_holder(8792) == " (pid 2279 Code Helper (Plugin))"

    def test_nobody_named_is_said_as_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _tools(monkeypatch, {"lsof": ""})
        assert serving.port_holder(8792) == ""

    def test_a_failing_lsof_is_said_as_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _tools(monkeypatch, {})
        assert serving.port_holder(8792) == ""
