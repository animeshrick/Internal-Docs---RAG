"""Shared setup for all tests. pytest loads this file automatically."""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_FOLDER = PROJECT_ROOT / "data" / "raw"

# Let the tests import the app package ("app.core.config", ...) whichever way pytest is started.
sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.ingest.parsing import parse_folder  # noqa: E402


@pytest.fixture(scope="session")
def parsed_documents():
    """Parse the 14 raw files once and share the result with every test."""
    # parse_folder logs its summary now instead of printing it, so there is nothing
    # to silence here: pytest shows captured logs only for failing tests.
    documents, _skipped_files = parse_folder(RAW_FOLDER)
    return documents
