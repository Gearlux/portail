"""Guard: branch coverage is propagated to the child processes the suite spawns.

``[tool.coverage.run] branch = true`` is read by the pytest process ALONE. pytest-cov
tells a child process which mode to use through the ``COV_CORE_BRANCH`` environment
variable, and it exports that variable only when ``--cov-branch`` reached it as a
command-line option -- the config file's ``branch`` setting never gets there.

Tests that spawn a real python child therefore wrote STATEMENT data beside the parent's
BRANCH data, and coverage's final combine refuses to merge the two::

    804 passed in 84.10s
    INTERNALERROR> coverage.exceptions.DataError:
        Can't combine statement coverage data with branch data   (exit code 3)

An all-green suite that still fails CI. ``addopts`` carries the flag so every invocation
gets it -- the CI command line, a bare ``pytest``, and a single-file run alike.
"""

import tomllib
from pathlib import Path
from typing import Any, Dict, cast

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _config() -> Dict[str, Any]:
    with _PYPROJECT.open("rb") as handle:
        # tomllib hands back `dict[str, Any]`, so indexing it is Any: name the shape here
        # rather than let it leak into every caller.
        return cast(Dict[str, Any], tomllib.load(handle)["tool"])


def test_branch_coverage_is_requested_on_the_command_line_not_only_in_the_config() -> None:
    tool = _config()
    if not tool["coverage"]["run"].get("branch"):
        return  # statement-only measurement: parent and children already agree
    addopts = tool["pytest"]["ini_options"].get("addopts", "")
    assert "--cov-branch" in addopts, (
        f"{_PYPROJECT} sets [tool.coverage.run] branch = true, so every child process the "
        "suite spawns must measure branches too -- add --cov-branch to "
        "[tool.pytest.ini_options] addopts, or the run ends in "
        "'Can't combine statement coverage data with branch data'."
    )
