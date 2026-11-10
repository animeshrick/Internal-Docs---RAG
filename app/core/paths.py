"""
paths.py - the one place that knows where the project lives on disk.

Every module imports these instead of counting ".." from its own file, so moving
a module between folders can no longer break its file paths.
"""
from pathlib import Path

# app/core/paths.py -> app/core -> app -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"
RAW_DOCS_DIR = DATA_DIR / "raw"
PARSED_DIR = DATA_DIR / "parsed"
CHUNKS_FILE_PATH = PARSED_DIR / "chunks.jsonl"
EVAL_DIR = PROJECT_ROOT / "eval"
