"""Template loading helpers for the local web UI."""

from __future__ import annotations

from pathlib import Path


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def load_template(name: str) -> str:
    """Load a bundled HTML template by filename."""
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")
