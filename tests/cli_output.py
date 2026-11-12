"""Reading what the CLI SAID, independent of where its formatter broke the lines.

The CLI prints through liquifai's ``rich`` console, which hard-wraps every message to the
console's width — 80 columns whenever stdout is not a terminal, as it is not under pytest. So a
message's line breaks fall wherever the arithmetic of width against content puts them, and a test
asserting a multi-word PHRASE can fail on a perfectly correct message whose wrap lands inside the
phrase.

Measured 2026-10-20 on a test of a refusal naming a served workspace::

    the message held            'is being \\nserved on http://127.0.0.1:8787'
    "being served" in it        -> False
    "being served" in flat(it)  -> True

The wrap moved there because the parallel fast run (``-n 4``) puts ``popen-gw0`` in the temp path,
lengthening a path that message prints twice — the test passed with ``-n 0`` and failed with the
workers. That is the whole point: the break position depends on content nobody controls (the temp
directory's name, the machine's, how many digits a port has), so WIDENING the console only moves
the problem. Measured the same day: ``COLUMNS=200`` makes that file pass, and still passes with a
176-character base temp — but it is arithmetic between width and content, not a guarantee, and it
would quietly stop holding the next time a message grew.

A test that means "the message says X" must therefore read the output with its whitespace
FLATTENED. That is what these helpers are for, and a new test that checks a phrase against CLI
output uses one of them rather than ``capsys.readouterr()`` directly.
"""

from typing import Any


def flat(text: str) -> str:
    """``text`` with every run of whitespace collapsed to ONE space: layout gone, words kept."""
    return " ".join(text.split())


def said(capsys: Any) -> str:
    """What the CLI wrote to stdout, flattened by :func:`flat`."""
    return flat(capsys.readouterr().out)


def said_all(capsys: Any) -> str:
    """Stdout AND stderr together, flattened — for a message that may go to either."""
    out, err = capsys.readouterr()
    return flat(out + err)
