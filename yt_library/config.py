"""Runtime configuration for YT Library Manager."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, time, timedelta, timezone
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
    "search_card_layout": "grid",
    "history_card_layout": "compact",
    "sort_preferences": {},
    "page_size": 100,
    "partial_completion_min_percent": 1,
    "history_fetch_daily": False,
    "history_fetch_time": "03:00",
    "admin_advanced": False,
    "use_proxy": False,
    "proxy": "",
    "dispatch_mode": "delay",
    "job_dispatch_delay_seconds": 5.0,
    "request_delay_min_seconds": 6.0,
    "request_delay_max_seconds": 10.0,
    "youtube_max_in_flight": 10,
    "archivarix_max_in_flight": 1,
    "archivarix_request_timeout_seconds": 15.0,
    "archivarix_stream_timeout_seconds": 30.0,
    "archivarix_retry_attempts": 3,
    "archivarix_retry_backoff_seconds": 2.0,
}

_LEGACY_YOUTUBE_REQUEST_INTERVAL_SECONDS = 5.0
_LEGACY_ARCHIVARIX_REQUEST_INTERVAL_SECONDS = 3.0
CARD_LAYOUTS = frozenset({"grid", "detailed", "compact"})
PAGE_SIZES = frozenset({50, 100, 250, 500})
SEARCH_SORTS = frozenset(
    {"relevance", "title", "newest", "oldest", "most_watched", "type"}
)
PLAYLIST_VIDEO_SORTS = frozenset(
    {"newest_added", "title", "oldest_added", "most_watched", "playlist_order"}
)
SEARCH_SORT_CONTEXTS = frozenset(
    {
        "search",
        "videos",
        "playlist-videos",
        "liked-videos",
        "all-playlists",
        "channels",
        "subscribed-channels",
        "terminated-channels",
        "playlist-group",
    }
)
SORT_PREFERENCE_VALUES = {
    **{context: SEARCH_SORTS for context in SEARCH_SORT_CONTEXTS},
    "playlist": PLAYLIST_VIDEO_SORTS,
}
DAILY_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


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


def configured_card_layout(
    config: dict[str, Any],
    key: str,
    default: str,
) -> str:
    value = str(config.get(key) or "").strip().lower()
    return value if value in CARD_LAYOUTS else default


def configured_search_card_layout(config: dict[str, Any]) -> str:
    return configured_card_layout(config, "search_card_layout", "grid")


def configured_history_card_layout(config: dict[str, Any]) -> str:
    return configured_card_layout(config, "history_card_layout", "compact")


def configured_page_size(config: dict[str, Any]) -> int:
    try:
        value = int(config.get("page_size", DEFAULT_CONFIG["page_size"]))
    except (TypeError, ValueError):
        return int(DEFAULT_CONFIG["page_size"])
    return value if value in PAGE_SIZES else int(DEFAULT_CONFIG["page_size"])


def configured_partial_completion_min_percent(config: dict[str, Any]) -> int:
    try:
        value = int(
            config.get(
                "partial_completion_min_percent",
                DEFAULT_CONFIG["partial_completion_min_percent"],
            )
        )
    except (TypeError, ValueError):
        return int(DEFAULT_CONFIG["partial_completion_min_percent"])
    return max(1, min(99, value))


def configured_sort_preferences(config: dict[str, Any]) -> dict[str, str]:
    raw_preferences = config.get("sort_preferences")
    if not isinstance(raw_preferences, dict):
        return {}
    preferences: dict[str, str] = {}
    for raw_context, raw_value in raw_preferences.items():
        context = str(raw_context).strip().lower()
        value = str(raw_value or "").strip().lower()
        if value in SORT_PREFERENCE_VALUES.get(context, frozenset()):
            preferences[context] = value
    return preferences


def configured_history_fetch_daily(config: dict[str, Any]) -> bool:
    value = config.get("history_fetch_daily", DEFAULT_CONFIG["history_fetch_daily"])
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def configured_admin_advanced(config: dict[str, Any]) -> bool:
    value = config.get("admin_advanced", DEFAULT_CONFIG["admin_advanced"])
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def valid_history_fetch_time(value: str) -> bool:
    return bool(DAILY_TIME_PATTERN.fullmatch((value or "").strip()))


def configured_history_fetch_time(config: dict[str, Any]) -> str:
    value = str(config.get("history_fetch_time") or "").strip()
    return value if valid_history_fetch_time(value) else str(DEFAULT_CONFIG["history_fetch_time"])


def next_history_fetch_at(
    config: dict[str, Any],
    now: datetime | None = None,
) -> datetime:
    current_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    zone = ZoneInfo(effective_display_timezone(config))
    local_now = current_utc.astimezone(zone)
    hour, minute = (int(part) for part in configured_history_fetch_time(config).split(":"))
    candidate = datetime.combine(
        local_now.date(),
        time(hour=hour, minute=minute),
        tzinfo=zone,
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def configured_dispatch_mode(config: dict[str, Any]) -> str:
    value = str(config.get("dispatch_mode") or "").strip().lower()
    if value in {"delay", "throttle"}:
        return value
    legacy_value = config.get("request_jitter_enabled", False)
    if isinstance(legacy_value, bool):
        return "throttle" if legacy_value else "delay"
    return (
        "throttle"
        if str(legacy_value).strip().lower() in {"1", "true", "yes", "on"}
        else "delay"
    )


def configured_job_dispatch_delay(config: dict[str, Any]) -> float:
    return max(
        0.0,
        float(
            config.get(
                "job_dispatch_delay_seconds",
                DEFAULT_CONFIG["job_dispatch_delay_seconds"],
            )
        ),
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


def configured_request_delay_range(config: dict[str, Any]) -> tuple[float, float]:
    minimum = max(
        0.0,
        float(
            config.get(
                "request_delay_min_seconds",
                DEFAULT_CONFIG["request_delay_min_seconds"],
            )
        ),
    )
    maximum = max(
        minimum,
        float(
            config.get(
                "request_delay_max_seconds",
                DEFAULT_CONFIG["request_delay_max_seconds"],
            )
        ),
    )
    return minimum, maximum


def configured_youtube_max_in_flight(config: dict[str, Any]) -> int:
    return max(1, min(100, int(config.get("youtube_max_in_flight", DEFAULT_CONFIG["youtube_max_in_flight"]))))


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
        if "dispatch_mode" not in loaded:
            config["dispatch_mode"] = configured_dispatch_mode(loaded)
        if "job_dispatch_delay_seconds" not in loaded:
            legacy_delays: list[float] = []
            for key, fallback in (
                (
                    "youtube_request_interval_seconds",
                    _LEGACY_YOUTUBE_REQUEST_INTERVAL_SECONDS,
                ),
                (
                    "archivarix_request_interval_seconds",
                    _LEGACY_ARCHIVARIX_REQUEST_INTERVAL_SECONDS,
                ),
            ):
                try:
                    value = float(loaded.get(key, fallback))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    legacy_delays.append(max(0.0, value))
            if legacy_delays:
                config["job_dispatch_delay_seconds"] = max(legacy_delays)
        if "request_delay_min_seconds" not in loaded:
            legacy_minimums: list[float] = []
            for key in (
                "youtube_request_delay_min_seconds",
                "archivarix_request_delay_min_seconds",
            ):
                try:
                    value = float(
                        loaded.get(key, DEFAULT_CONFIG["request_delay_min_seconds"])
                    )
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    legacy_minimums.append(max(0.0, value))
            if legacy_minimums:
                config["request_delay_min_seconds"] = max(legacy_minimums)
        if "request_delay_max_seconds" not in loaded:
            legacy_maximums: list[float] = []
            for key in (
                "youtube_request_delay_max_seconds",
                "archivarix_request_delay_max_seconds",
            ):
                try:
                    value = float(
                        loaded.get(key, DEFAULT_CONFIG["request_delay_max_seconds"])
                    )
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    legacy_maximums.append(max(0.0, value))
            if legacy_maximums:
                config["request_delay_max_seconds"] = max(legacy_maximums)
        if "use_proxy" not in loaded and str(loaded.get("proxy") or "").strip():
            config["use_proxy"] = True
    config["dispatch_mode"] = configured_dispatch_mode(config)
    config["job_dispatch_delay_seconds"] = configured_job_dispatch_delay(config)
    request_delay_min, request_delay_max = configured_request_delay_range(config)
    config["request_delay_min_seconds"] = request_delay_min
    config["request_delay_max_seconds"] = request_delay_max
    config["youtube_max_in_flight"] = configured_youtube_max_in_flight(config)
    config["archivarix_max_in_flight"] = configured_archivarix_max_in_flight(config)
    config["search_card_layout"] = configured_search_card_layout(config)
    config["history_card_layout"] = configured_history_card_layout(config)
    config["sort_preferences"] = configured_sort_preferences(config)
    config["page_size"] = configured_page_size(config)
    config["partial_completion_min_percent"] = (
        configured_partial_completion_min_percent(config)
    )
    config["history_fetch_daily"] = configured_history_fetch_daily(config)
    config["history_fetch_time"] = configured_history_fetch_time(config)
    config["admin_advanced"] = configured_admin_advanced(config)
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
