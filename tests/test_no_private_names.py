"""portaide names neither helios nor waivefront, nor an app built on it.

portaide serves every domain, where the signal domain (waivefront) and a private bridge (helios) mean nothing
(root ``AGENTS.md`` → "General Projects Name Neither Helios Nor Waivefront"). And its docs never name a project that
depends on it (aisland rules → "Docs Never Name Dependent Projects"): an app built on portaide is named by nothing
here — not in code, tests or docs — so the framework stays the framework.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List

PROJECT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile("helios|waivefront|annotaide|curaitor", re.IGNORECASE)
#: Files that must spell a forbidden word, and why.
ALLOWED: Dict[str, str] = {}


def _project_files() -> List[Path]:
    """Every file git tracks in portaide, and every new one it does not ignore."""
    listed = subprocess.run(
        ["git", "-C", str(PROJECT), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [PROJECT / name for name in listed if (PROJECT / name).is_file()]


def test_no_file_of_portaide_names_helios_waivefront_or_an_app_built_on_it() -> None:
    this_file = Path(__file__).resolve()
    hits = []
    for path in _project_files():
        if path.resolve() == this_file or path.relative_to(PROJECT).as_posix() in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(PROJECT)}:{number}: {line.strip()}")
    assert not hits, "portaide names helios, waivefront or an app built on it — say it in general words:\n" + "\n".join(
        hits
    )
