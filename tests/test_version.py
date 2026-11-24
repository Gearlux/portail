"""``portail.__version__`` is the installed distribution's version, read from its metadata.

The version is written ONCE, in ``pyproject.toml``; a typed-in ``__version__`` is a second copy that drifts.
"""

from importlib.metadata import version

import portail


def test_version_is_the_installed_distribution_version() -> None:
    assert portail.__version__ == version("portail")
