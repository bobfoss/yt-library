"""SQLite schema resource loader."""

from __future__ import annotations

from pathlib import Path


SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def load_schema() -> str:
    """Load the bundled SQLite schema."""
    return SCHEMA_PATH.read_text(encoding="utf-8")
