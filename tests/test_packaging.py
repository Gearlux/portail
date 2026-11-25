"""What a built wheel must contain, and what PyPI must accept.

An editable install reads every file straight from the checkout, so a file missing from the WHEEL is invisible during
development and total in production: the package imports and serves no page. Each rule is on the declaration or on a
wheel built here from what the checkout carries.
"""

import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any, Dict, List, cast

import pytest

_REPO = Path(__file__).resolve().parent.parent
_PACKAGE = _REPO / "portail"
#: `[text](target)` with a relative target — absolute URLs excluded by rejecting `://`; an in-page `#anchor` is fine.
_RELATIVE_LINK = re.compile(r"\[[^\]]*\]\((?!\w+://|#)([^)\s]+)\)")
#: What the wheel must hold beyond the modules — an install without one of them does not work.
_MUST_SHIP = [
    "portail/py.typed",
    "portail/web/static/shell.js",
    "portail/web/static/shell.css",
    "portail/web/static/server-events.js",
    "portail/web/static/index.html",
]


def _pyproject() -> Dict[str, Any]:
    with (_REPO / "pyproject.toml").open("rb") as fh:
        return cast(Dict[str, Any], tomllib.load(fh))


def _project() -> Dict[str, Any]:
    return cast(Dict[str, Any], _pyproject()["project"])


def _every_requirement() -> List[str]:
    project = _project()
    requirements = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    return requirements


def test_no_requirement_is_a_direct_url() -> None:
    """PyPI refuses an upload whose metadata holds a `name @ url` requirement — extras included."""
    assert [line for line in _every_requirement() if "@" in line or "://" in line] == []


def test_the_landing_page_metadata_is_present() -> None:
    project = _project()
    assert project["readme"] == "README.md" and (_REPO / "README.md").is_file()
    assert project["license"] and project["license-files"] == ["LICENSE"] and (_REPO / "LICENSE").is_file()
    assert {"Homepage", "Repository", "Issues", "Changelog"} <= set(project["urls"])
    assert project["authors"] and project["classifiers"] and project["description"]


def test_the_development_status_classifier_matches_the_version() -> None:
    """A pre-release (`0.1.0a1`) is Alpha on its PyPI page; a classifier left behind says otherwise."""
    project = _project()
    status = [line for line in project["classifiers"] if line.startswith("Development Status ::")]
    expected = "3 - Alpha" if "a" in project["version"] else "4 - Beta" if "b" in project["version"] else None
    if expected is not None:
        assert status == [f"Development Status :: {expected}"]


def test_readme_links_stay_inside_what_pypi_can_reach() -> None:
    """The README is the PyPI page: a relative link works on GitHub and is dead there."""
    assert _RELATIVE_LINK.findall((_REPO / "README.md").read_text(encoding="utf-8")) == []


def test_the_changelog_is_shipped_in_the_sdist() -> None:
    """`MANIFEST.in` must carry `CHANGELOG.md` — nothing else pulls it in."""
    manifest = _REPO / "MANIFEST.in"
    assert manifest.is_file() and (_REPO / "CHANGELOG.md").is_file()
    included = [
        line.split(maxsplit=1)[1].strip() for line in manifest.read_text().splitlines() if line.startswith("include ")
    ]
    assert "CHANGELOG.md" in included


@pytest.mark.parametrize("word", ["tai" "dal", "ais" "land"])
def test_the_published_text_names_no_workspace_and_no_setup_tool(word: str) -> None:
    """The README (the PyPI page), the CHANGELOG, the pages under ``docs/`` and the package's description name neither
    the workspace this package is developed in nor its setup tool: a reader has neither."""
    names = ["README.md", "CHANGELOG.md", *sorted(f"docs/{page.name}" for page in (_REPO / "docs").glob("*.md"))]
    texts = {name: (_REPO / name).read_text(encoding="utf-8") for name in names}
    texts["pyproject.toml description"] = str(_project()["description"])
    found = [
        f"{name}:{lineno}: {line.strip()[:80]}"
        for name, text in texts.items()
        for lineno, line in enumerate(text.splitlines(), start=1)
        if word in line.lower()
    ]
    assert not found, f"{word!r} in the published text:\n" + "\n".join(found)


def test_the_changelog_has_a_section_for_this_version() -> None:
    assert f"## [{_project()['version']}]" in (_REPO / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_declared_package_data_files_exist() -> None:
    """A `package-data` pattern that matches nothing ships nothing, and says nothing."""
    patterns = _pyproject()["tool"]["setuptools"]["package-data"]["portail"]
    assert [pattern for pattern in patterns if not list(_PACKAGE.glob(pattern))] == []


def test_the_wheel_built_from_a_checkout_holds_what_an_install_needs(tmp_path: Path) -> None:
    """Built from what a checkout CARRIES (the tracked files, plus new ones not ignored — `git ls-files -co`)."""
    git = shutil.which("git")
    inside = (
        git
        and subprocess.run(
            [git, "rev-parse", "--is-inside-work-tree"], cwd=_REPO, capture_output=True, text=True
        ).stdout.strip()
    )
    if inside != "true":
        pytest.skip("not a git checkout: what a checkout carries is what git lists")
    assert git is not None
    listed = subprocess.run(
        [git, "ls-files", "-co", "--exclude-standard", "-z"], cwd=_REPO, capture_output=True, check=True
    ).stdout.decode()
    checkout = tmp_path / "checkout"
    for name in filter(None, listed.split("\0")):
        if (_REPO / name).is_file():
            (checkout / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_REPO / name, checkout / name)
    # No isolation: an isolated build would fetch the backend from the network (the [dev] extra declares it).
    command = [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(tmp_path / "wheel")]
    built = subprocess.run([*command, str(checkout)], capture_output=True, text=True)
    assert built.returncode == 0, built.stdout[-2000:] + built.stderr[-2000:]
    names = zipfile.ZipFile(next((tmp_path / "wheel").glob("*.whl"))).namelist()
    assert [member for member in _MUST_SHIP if member not in names] == []
    assert [name for name in names if name.startswith("tests/")] == [], "the tests are not part of the package"
