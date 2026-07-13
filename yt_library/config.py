"""Runtime configuration for YT Library Manager."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "yt_library.config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "database": "yt_library.sqlite3",
    "cookies": "YT cookies.txt",
    "archivarix_cookies": "archivarix.net cookies.txt",
    "pockettube_export": "youtube_playlist_manager_2026-07-02-17_13.json",
    "thumbnail_dir": "thumbs",
    "archivarix_thumbnail_dir": "archivarix_thumbs",
    "video_thumbnail_dir": "video_thumbs",
    "takeout_dir": "takeout",
    "host": "127.0.0.1",
    "port": 8765,
    "display_timezone": "UTC",
}

PATH_KEYS = {
    "database",
    "cookies",
    "archivarix_cookies",
    "pockettube_export",
    "thumbnail_dir",
    "archivarix_thumbnail_dir",
    "video_thumbnail_dir",
    "takeout_dir",
}


def load_config(config_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file must contain a JSON object: {path}")
        config.update({key: value for key, value in loaded.items() if value is not None})
    config["_config_path"] = str(path)
    return config


def ensure_config_file(config: dict[str, Any]) -> Path:
    path = Path(str(config.get("_config_path") or DEFAULT_CONFIG_PATH))
    if path.exists():
        return path
    payload = {key: config[key] for key in DEFAULT_CONFIG}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def save_config(config: dict[str, Any]) -> Path:
    path = Path(str(config.get("_config_path") or DEFAULT_CONFIG_PATH))
    payload = {key: config.get(key, DEFAULT_CONFIG[key]) for key in DEFAULT_CONFIG}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def config_path(config: dict[str, Any], key: str) -> Path:
    value = Path(str(config[key]))
    if value.is_absolute():
        return value
    base = Path(str(config.get("_config_path") or DEFAULT_CONFIG_PATH)).resolve().parent
    return base / value


def config_int(config: dict[str, Any], key: str) -> int:
    return int(config[key])
