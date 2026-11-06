"""Shared setup for all tests. pytest loads this file automatically."""
import contextlib
import io
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_FOLDER = PROJECT_ROOT / "data" / "raw"

# Let the tests import the modules that live in the app/ folder.
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from app.parsing_v2 import parse_folder  # noqa: E402


@pytest.fixture(scope="session")
def parsed_documents():
    """Parse the 14 raw files once and share the result with every test."""
    # parse_folder prints a summary line; we hide it to keep the test output clean.
    with contextlib.redirect_stdout(io.StringIO()):
        documents, _skipped_files = parse_folder(RAW_FOLDER)
    return documents
