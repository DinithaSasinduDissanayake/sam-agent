"""S1: the cross-platform test package is collected and isolated on every OS."""
import os
import sys

from tests.xplat import REPO


def test_repo_layout():
    assert os.path.isfile(os.path.join(REPO, "pyproject.toml"))


def test_sam_home_is_isolated():
    # conftest points SAM_HOME into the per-test tmp dir on every OS
    home = os.environ.get("SAM_HOME", "")
    assert home and "isolated-home" in home


def test_psutil_available_on_windows():
    if sys.platform == "win32":
        import psutil  # noqa: F401
