"""
A safety net for the one thing the other tests cannot see.

Most of this suite replaces the store functions with fakes (tests/fakes.py), so the
real bodies never run. A name that is used but never imported is therefore invisible:
the module imports fine, the tests pass, and the NameError only appears in production
on the first real call. That is exactly what happened to leave/store.py once.

pyflakes reads the source instead of running it, so it sees those names.
"""
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHECKED_FOLDERS = ("app", "scripts", "tests")


def run_pyflakes():
    result = subprocess.run([sys.executable, "-m", "pyflakes", *CHECKED_FOLDERS],
                            cwd=PROJECT_ROOT, capture_output=True, text=True)
    if "No module named pyflakes" in result.stderr:
        pytest.skip("pyflakes is not installed (pip install pyflakes)")
    return [line for line in result.stdout.splitlines() if "__pycache__" not in line]


def test_no_undefined_names():
    """A used-but-never-imported name is a crash waiting for the first real call."""
    problems = [line for line in run_pyflakes() if "undefined name" in line]
    assert not problems, "undefined names:\n" + "\n".join(problems)


def test_no_unused_imports():
    """Usually harmless, but after a refactor it is the sign of a move left half done."""
    problems = [line for line in run_pyflakes() if "imported but unused" in line]
    assert not problems, "unused imports:\n" + "\n".join(problems)
