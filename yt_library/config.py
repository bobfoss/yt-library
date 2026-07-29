"""Runtime configuration for YT Library Manager."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .network import validated_socks5_proxy_url


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "yt_library.config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "database": "yt_library.sqlite3",
    "youtube_cookies": "yt_cookies.txt",
    "archivarix_cookies": "archivarix_cookies.txt",
    "thumbnail_dir": "thumbs",
    "archivarix_thumbnail_dir": "archivarix_thumbs",
    "video_thumbnail_dir": "video_thumbs",
    "takeout_dir": "takeout",
    "host": "127.0.0.1",
    "port": 8765,
    "display_timezone": "",
    "use_proxy": False,
    "proxy": "",
    "request_jitter_enabled": False,
    "youtube_request_interval_seconds": 5.0,
    "youtube_request_delay_min_seconds": 6.0,
    "youtube_request_delay_max_seconds": 10.0,
    "youtube_max_in_flight": 10,
    "archivarix_request_interval_seconds": 3.0,
    "archivarix_request_delay_min_seconds": 6.0,
    "archivarix_request_delay_max_seconds": 10.0,
    "archivarix_max_in_flight": 1,
    "archivarix_request_timeout_seconds": 15.0,
    "archivarix_stream_timeout_seconds": 30.0,
    "archivarix_retry_attempts": 3,
    "archivarix_retry_backoff_seconds": 2.0,
}


def configured_display_timezone(config: dict[str, Any]) -> str:
    value = str(config.get("display_timezone") or "").strip()
    if not value:
        return ""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return value


def effective_display_timezone(config: dict[str, Any]) -> str:
    return configured_display_timezone(config) or "UTC"


def configured_youtube_request_interval(config: dict[str, Any]) -> float:
    return max(
        0.0,
        float(config.get("youtube_request_interval_seconds", DEFAULT_CONFIG["youtube_request_interval_seconds"])),
    )


def configured_proxy_address(config: dict[str, Any]) -> str:
    return validated_socks5_proxy_url(
        str(config.get("proxy", DEFAULT_CONFIG["proxy"]) or "")
    )


def configured_use_proxy(config: dict[str, Any]) -> bool:
    value = config.get("use_proxy")
    if value is None:
        return bool(configured_proxy_address(config))
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def configured_proxy(config: dict[str, Any]) -> str:
    proxy_url = configured_proxy_address(config)
    return proxy_url if configured_use_proxy(config) else ""


def configured_request_jitter_enabled(config: dict[str, Any]) -> bool:
    value = config.get(
        "request_jitter_enabled",
        DEFAULT_CONFIG["request_jitter_enabled"],
    )
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _configured_request_delay_range(
    config: dict[str, Any],
    site: str,
) -> tuple[float, float]:
    minimum_key = f"{site}_request_delay_min_seconds"
    maximum_key = f"{site}_request_delay_max_seconds"
    minimum = max(
        0.0,
        float(
            config.get(
                minimum_key,
                DEFAULT_CONFIG[minimum_key],
            )
        ),
    )
    maximum = max(
        minimum,
        float(
            config.get(
                maximum_key,
                DEFAULT_CONFIG[maximum_key],
            )
        ),
    )
    return minimum, maximum


def configured_youtube_request_delay_range(config: dict[str, Any]) -> tuple[float, float]:
    return _configured_request_delay_range(config, "youtube")


def configured_archivarix_request_delay_range(config: dict[str, Any]) -> tuple[float, float]:
    return _configured_request_delay_range(config, "archivarix")


def configured_youtube_max_in_flight(config: dict[str, Any]) -> int:
    return max(1, min(100, int(config.get("youtube_max_in_flight", DEFAULT_CONFIG["youtube_max_in_flight"]))))


def configured_archivarix_request_interval(config: dict[str, Any]) -> float:
    return max(
        0.0,
        float(config.get("archivarix_request_interval_seconds", DEFAULT_CONFIG["archivarix_request_interval_seconds"])),
    )


def configured_archivarix_max_in_flight(config: dict[str, Any]) -> int:
    return max(1, min(20, int(config.get("archivarix_max_in_flight", DEFAULT_CONFIG["archivarix_max_in_flight"]))))


def configured_archivarix_request_timeout(config: dict[str, Any]) -> float:
    return max(
        1.0,
        min(
            120.0,
            float(
                config.get(
                    "archivarix_request_timeout_seconds",
                    DEFAULT_CONFIG["archivarix_request_timeout_seconds"],
                )
            ),
        ),
    )


def configured_archivarix_stream_timeout(config: dict[str, Any]) -> float:
    return max(
        1.0,
        min(
            300.0,
            float(
                config.get(
                    "archivarix_stream_timeout_seconds",
                    DEFAULT_CONFIG["archivarix_stream_timeout_seconds"],
                )
            ),
        ),
    )


def configured_archivarix_retry_attempts(config: dict[str, Any]) -> int:
    return max(
        1,
        min(
            10,
            int(config.get("archivarix_retry_attempts", DEFAULT_CONFIG["archivarix_retry_attempts"])),
        ),
    )


def configured_archivarix_retry_backoff(config: dict[str, Any]) -> float:
    return max(
        0.0,
        min(
            60.0,
            float(
                config.get(
                    "archivarix_retry_backoff_seconds",
                    DEFAULT_CONFIG["archivarix_retry_backoff_seconds"],
                )
            ),
        ),
    )


PATH_KEYS = {
    "database",
    "youtube_cookies",
    "archivarix_cookies",
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
        config.update(
            {
                key: value
                for key, value in loaded.items()
                if key in DEFAULT_CONFIG and value is not None
            }
        )
        if "use_proxy" not in loaded and str(loaded.get("proxy") or "").strip():
            config["use_proxy"] = True
    configured_proxy_address(config)
    config["_config_path"] = str(path)
    return config


def ensure_config_file(config: dict[str, Any]) -> Path:
    path = Path(str(config.get("_config_path") or DEFAULT_CONFIG_PATH))
    if path.exists():
        return path
    payload = {key: config[key] for key in DEFAULT_CONFIG}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    ensure_directory(config_path(config, "takeout_dir"))
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


def ensure_directory(path: Path | str) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def config_int(config: dict[str, Any], key: str) -> int:
    return int(config[key])
