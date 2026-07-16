#!/usr/bin/env python3
"""Import YouTube library data and browse it locally."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import http.cookiejar
import http.server
import io
import json
import mimetypes
import os
import posixpath
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schema import load_schema
from .config import effective_display_timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "yt_library.sqlite3"
DEFAULT_THUMB_DIR = ROOT / "thumbs"
DEFAULT_ARCHIVARIX_THUMB_DIR = ROOT / "archivarix_thumbs"
DEFAULT_VIDEO_THUMB_DIR = ROOT / "video_thumbs"
COOKIE_FILE = ROOT / "yt_cookies.txt"
ARCHIVARIX_COOKIE_FILE = ROOT / "archivarix_cookies.txt"
POCKETTUBE_EXPORT = ROOT / "youtube_playlist_manager_2026-07-02-17_13.json"
TAKEOUT_DIR = ROOT / "YouTube and YouTube Music"
HISTORY_BATCH_SIZE = 1000
HISTORY_BATCH_DELAY_SECONDS = 10.0
DEFAULT_DISPLAY_TIMEZONE = "UTC"

PLAYLIST_MATCH_TYPE_NOTES = {
    "ambiguous_hidden_candidate": "missing from current playable scan; hidden slot mapping is ambiguous",
    "ambiguous_hidden_slot": "current hidden slot has no exposed video ID",
    "inferred_hidden_slot": "matched hidden current slot to missing Takeout video by ordered equal counts",
}
PLAYLIST_MATCH_TYPE_LABELS = {
    "ambiguous_hidden_candidate": "Takeout candidate",
    "ambiguous_hidden_slot": "hidden slot",
    "inferred_hidden_slot": "restored from Takeout",
}

HISTORY_SOURCE_TYPE_LABELS = {
    "takeout_youtube": "Takeout + YouTube",
    "takeout": "Takeout",
    "youtube": "YouTube",
}
HISTORY_MATCH_TYPE_LABELS = {
    "video_id_date": "matched by video/date",
    "takeout_only": "Takeout only",
    "youtube_only": "YouTube only",
}
HISTORY_TIME_QUALITY_LABELS = {
    "exact": "exact time",
    "date_only": "date only",
    "unknown": "time unknown",
}
HISTORY_TIME_QUALITY_NOTES = {
    "unknown": "YouTube history entry had no watch date; observed_at is fetch time",
}

VIDEO_SOURCE_PRIORITY = {
    "": 0,
    "archivarix": 10,
    "takeout": 20,
    "youtube_history": 30,
    "playlist": 40,
    "youtube": 50,
    "metadata": 50,
}

LIKED_VIDEOS_PLAYLIST_ID = "LL"


SCHEMA = load_schema()
SCHEMA_VERSION = 5


@dataclass(frozen=True)
class GroupNode:
    key: str
    name: str
    parent_key: str | None
    position: int
    icon: str


class ArchivarixQuotaExceeded(RuntimeError):
    """Raised when Archivarix reports the account has exhausted its daily search quota."""


_DATABASE_BOOTSTRAP_LOCK = threading.Lock()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate_database(db_path: Path) -> None:
    """Initialize or upgrade the database schema."""
    conn = connect(db_path)
    try:
        with _DATABASE_BOOTSTRAP_LOCK:
            with conn:
                _migrate_database(conn)
    except Exception:
        conn.close()
        raise
    else:
        conn.close()


def _bootstrap_database(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, utc_now()),
    )


def _migrate_database(conn: sqlite3.Connection) -> None:
    current_version = _schema_version(conn)
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current_version} is newer than this application supports ({SCHEMA_VERSION})"
        )
    if current_version == SCHEMA_VERSION:
        return
    _bootstrap_database(conn)
    if current_version < 2:
        conn.execute("DROP TABLE IF EXISTS app_settings")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (2, utc_now()),
        )
    if current_version < 5:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(placeholder_recovery_worker_runs)")
        }
        if "request_started_at" not in columns:
            conn.execute(
                "ALTER TABLE placeholder_recovery_worker_runs ADD COLUMN request_started_at TEXT"
            )
        conn.execute(
            """
            UPDATE placeholder_recovery_worker_runs
            SET request_started_at = started_at
            WHERE request_started_at IS NULL
              AND recovery_status NOT IN ('', 'authentication_error')
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (5, utc_now()),
        )


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if not row:
        return 0
    value = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    return int(value or 0)


def normalize_playlist_visibility(value: str) -> str:
    normalized = re.sub(r"\s+", " ", (value or "").strip()).lower()
    if normalized.endswith(" playlist"):
        normalized = normalized[: -len(" playlist")].strip()
    return normalized if normalized in {"private", "public", "unlisted"} else ""


def split_playlist_owner_visibility(value: str) -> tuple[str, str]:
    visibility = normalize_playlist_visibility(value)
    return ("", visibility) if visibility else ((value or "").strip(), "")


def assert_playlist_owner_visibility(metadata: dict[str, Any]) -> None:
    owner_channel_id = (metadata.get("owner_channel_id") or "").strip()
    visibility = (metadata.get("visibility") or "").strip()
    assert not (owner_channel_id and visibility), (
        "Playlist metadata cannot contain both an owner channel and a visibility value."
    )


def playlist_match_type_note(match_type: str) -> str:
    return PLAYLIST_MATCH_TYPE_NOTES.get(match_type or "", "")


def playlist_match_type_label(match_type: str) -> str:
    return PLAYLIST_MATCH_TYPE_LABELS.get(match_type or "", "")


def history_source_type_label(source_type: str) -> str:
    return HISTORY_SOURCE_TYPE_LABELS.get(source_type or "", source_type or "")


def history_match_type_label(match_type: str) -> str:
    return HISTORY_MATCH_TYPE_LABELS.get(match_type or "", match_type or "")


def history_time_quality_label(time_quality: str) -> str:
    return HISTORY_TIME_QUALITY_LABELS.get(time_quality or "", time_quality or "")


def history_time_quality_note(time_quality: str) -> str:
    return HISTORY_TIME_QUALITY_NOTES.get(time_quality or "", "")


def video_availability_from_recovery_status(status: str) -> str:
    status = (status or "").strip()
    status_upper = status.upper()
    if status_upper == "LIVE":
        return "public"
    if status_upper == "NOT_FOUND" or status_upper.startswith("DELETED_"):
        return "unavailable"
    return ""


def is_playable_from_recovery_status(status: str) -> int:
    return 1 if (status or "").strip().upper() == "LIVE" else 0


def normalize_video_availability(
    video_id: str,
    availability: str = "",
    is_playable: bool | int | None = None,
    recovered_status: str = "",
) -> str:
    if not video_id:
        return "unknown"
    availability = (availability or "").strip()
    legacy_note_values = {
        "Takeout candidate; current hidden slot match is ambiguous",
        *PLAYLIST_MATCH_TYPE_NOTES.values(),
    }
    if availability in legacy_note_values:
        availability = ""
    lowered = availability.lower()
    if lowered == "unavailable video is hidden":
        return "unavailable"
    if lowered == "live":
        return "public"
    if availability:
        return lowered
    recovered_availability = video_availability_from_recovery_status(recovered_status)
    if recovered_availability:
        return recovered_availability
    if is_playable:
        return "public"
    if is_playable is False or is_playable == 0:
        return "unavailable"
    return "unknown"


def playlist_entry_is_unavailable(title: str, availability: str = "") -> bool:
    normalized_title = (title or "").strip().lower().strip("[]() ")
    if normalized_title in {"deleted video", "private video", "unavailable video"}:
        return True
    return (availability or "").strip().lower() in {
        "private",
        "needs_auth",
        "premium_only",
        "subscriber_only",
        "unavailable",
        "deleted",
    }


def watch_playability_value(metadata: dict[str, Any]) -> int | None:
    status = str(metadata.get("playability_status") or metadata.get("yt_status") or "").strip().upper()
    if not status:
        return None
    status = status.split(":", 1)[0].strip()
    if status == "OK":
        return 1
    if status in {"ERROR", "UNPLAYABLE", "LOGIN_REQUIRED", "LIVE_STREAM_OFFLINE"}:
        return 0
    return None


def storable_watch_playability_value(metadata: dict[str, Any]) -> int | None:
    playability = watch_playability_value(metadata)
    if playability != 0:
        return playability
    if (metadata.get("availability") or "").strip():
        return playability
    return None


def apply_watch_playability_to_playlist_rows(
    conn: sqlite3.Connection,
    video_id: str,
    metadata: dict[str, Any],
) -> int:
    video_id = (video_id or "").strip()
    playability = watch_playability_value(metadata)
    if not video_id or playability is None:
        return 0
    availability = "public" if playability else "unavailable"
    result = conn.execute(
        """
        UPDATE videos
        SET is_playable = ?, availability = ?, last_checked_at = ?,
            last_seen_available_at = CASE WHEN ? = 1 THEN ? ELSE last_seen_available_at END,
            updated_at = ?
        WHERE video_id = ?
          AND (is_playable IS NOT ? OR availability <> ?)
        """,
        (
            playability,
            availability,
            utc_now(),
            playability,
            utc_now(),
            utc_now(),
            video_id,
            playability,
            availability,
        ),
    )
    return result.rowcount


def playlist_zero_result_is_suspicious(
    parsed_count: int,
    ytdlp_error: str,
    previous_scan_count: int,
) -> bool:
    """Avoid replacing a known playlist with an empty web fallback after yt-dlp is denied."""
    return parsed_count == 0 and bool(ytdlp_error.strip()) and previous_scan_count > 0


def playlist_scan_is_incomplete(parsed_count: int, expected_count: int) -> bool:
    """Return whether a source result is shorter than the current expected playlist size."""
    return expected_count > 0 and parsed_count < expected_count


def playlist_scan_requires_exact_count(
    metadata: dict[str, Any],
    *,
    known_owner_channel_id: str = "",
    known_visibility: str = "",
) -> bool:
    """Owned or ambiguous playlist headers must match the displayed YouTube count."""
    owner_channel_id = str(metadata.get("owner_channel_id") or "").strip()
    visibility = str(metadata.get("visibility") or "").strip()
    if not owner_channel_id and not visibility:
        owner_channel_id = (known_owner_channel_id or "").strip()
        visibility = (known_visibility or "").strip()
    return not (owner_channel_id and not visibility)


def reconciled_video_availability(
    video_id: str,
    current_availability: str = "",
    recovered_status: str = "",
    is_playable: bool | int | None = None,
) -> str:
    return normalize_video_availability(video_id, current_availability, is_playable, recovered_status)


def youtube_channel_id_from_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    found = re.search(r"(?:youtube\.com|youtu\.be)/(?:channel/)?(UC[A-Za-z0-9_-]{20,})", value)
    if found:
        return found.group(1)
    found = re.search(r"(?:^|[/?&=])(UC[A-Za-z0-9_-]{20,})(?:$|[/?&#])", value)
    return found.group(1) if found else ""


def youtube_channel_url(channel_id: str) -> str:
    channel_id = (channel_id or "").strip()
    if not channel_id:
        return ""
    if channel_id.startswith(("http://", "https://")):
        return channel_id
    if channel_id.startswith("@"):
        return f"https://www.youtube.com/{channel_id}"
    if channel_id.startswith(("c/", "user/")):
        return f"https://www.youtube.com/{channel_id}"
    return f"https://www.youtube.com/channel/{channel_id}"


def youtube_video_url(video_id: str, playlist_id: str = "") -> str:
    video_id = (video_id or "").strip()
    if not video_id:
        return ""
    url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    if playlist_id:
        url += f"&list={urllib.parse.quote(playlist_id)}"
    return url


def youtube_playlist_url(playlist_id: str) -> str:
    playlist_id = (playlist_id or "").strip()
    return f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}" if playlist_id else ""


def archivarix_search_url(video_id: str) -> str:
    return f"https://tube.archivarix.net/?q={urllib.parse.quote(video_id)}" if video_id else ""


def archivarix_media_url(video_id: str) -> str:
    return (
        f"https://web.archive.org/web/2oe_/http://wayback-fakeurl.archive.org/yt/{video_id}"
        if video_id
        else ""
    )


def wayback_video_url(video_id: str, capture_at: str | None) -> str:
    if not video_id or not capture_at:
        return ""
    stamp = re.sub(r"[^0-9]", "", capture_at)[:14]
    return f"https://web.archive.org/web/{stamp}/{youtube_video_url(video_id)}" if len(stamp) == 14 else ""


def youtube_channel_ref_from_url(value: str) -> str:
    value = html.unescape((value or "").strip())
    channel_id = youtube_channel_id_from_url(value)
    if channel_id:
        return channel_id
    parsed = urllib.parse.urlparse(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return ""
    if parts[0].startswith("@"):
        return parts[0]
    if parts[0] in {"c", "user"} and len(parts) > 1:
        return f"{parts[0]}/{parts[1]}"
    return ""


def channel_title_for_id(conn: sqlite3.Connection, channel_id: str) -> str:
    if not channel_id:
        return ""
    row = conn.execute("SELECT title FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
    return row["title"] if row else ""


def merge_channel_value(existing: str, incoming: str) -> str:
    return incoming if incoming else existing


def upsert_channel(
    conn: sqlite3.Connection,
    channel_id: str,
    *,
    title: str = "",
    url: str = "",
    description: str = "",
    aliases: str = "",
    thumbnail_url: str = "",
    thumbnail_path: str = "",
    archivarix_channel_id: str = "",
    status: str = "",
    status_reason: str = "",
    fetch_status: str = "",
    fetch_error: str = "",
    fetched_at: str | None = None,
    source: str = "",
    updated_at: str | None = None,
) -> str:
    channel_id = (channel_id or "").strip()
    if not channel_id:
        channel_id = youtube_channel_id_from_url(url)
    if not channel_id:
        return ""
    now = updated_at or utc_now()
    existing = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE channels
            SET title = ?,
                description = ?,
                aliases = ?,
                thumbnail_url = ?,
                thumbnail_path = ?,
                archivarix_channel_id = ?,
                status = ?,
                status_reason = ?,
                fetch_status = ?,
                fetch_error = ?,
                fetched_at = ?,
                metadata_source = ?,
                updated_at = ?
            WHERE channel_id = ?
            """,
            (
                merge_channel_value(existing["title"], title),
                merge_channel_value(existing["description"], description),
                merge_channel_value(existing["aliases"], aliases),
                merge_channel_value(existing["thumbnail_url"], thumbnail_url),
                merge_channel_value(existing["thumbnail_path"], thumbnail_path),
                merge_channel_value(existing["archivarix_channel_id"], archivarix_channel_id),
                merge_channel_value(existing["status"], status),
                merge_channel_value(existing["status_reason"], status_reason),
                merge_channel_value(existing["fetch_status"], fetch_status),
                fetch_error if fetch_status else existing["fetch_error"],
                fetched_at or existing["fetched_at"],
                merge_channel_value(existing["metadata_source"], source),
                now,
                channel_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO channels(
              channel_id, title, description, aliases, thumbnail_url, thumbnail_path,
              archivarix_channel_id, status, status_reason, fetch_status, fetch_error,
              fetched_at, metadata_source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                title,
                description,
                aliases,
                thumbnail_url,
                thumbnail_path,
                archivarix_channel_id,
                status,
                status_reason,
                fetch_status,
                fetch_error,
                fetched_at,
                source,
                now,
            ),
        )
    return channel_id


def useful_video_title(value: str) -> bool:
    return (value or "").strip().lower().strip("[]() ") not in {
        "",
        "youtube",
        "- youtube",
        "deleted video",
        "private video",
        "unavailable video",
    }


def upsert_video(
    conn: sqlite3.Connection,
    video_id: str,
    *,
    title: str = "",
    description: str = "",
    channel_id: str = "",
    channel_title: str = "",
    channel_url: str = "",
    duration_text: str = "",
    view_count: str = "",
    upload_date: str = "",
    thumbnail_url: str = "",
    thumbnail_path: str = "",
    reaction: str = "",
    watch_progress_percent: int | str | None = None,
    watch_resume_seconds: int | str | None = None,
    is_playable: bool | int | None = None,
    availability: str = "",
    source: str = "",
    fetch_status: str = "",
    fetch_error: str = "",
    fetched_at: str | None = None,
    checked_at: str | None = None,
    updated_at: str | None = None,
) -> str:
    video_id = (video_id or "").strip()
    if not video_id:
        return ""
    now = updated_at or utc_now()
    channel_id = upsert_channel(
        conn,
        channel_id,
        title=channel_title,
        url=channel_url,
        source=source,
        updated_at=now,
    ) or None
    existing = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    incoming_priority = VIDEO_SOURCE_PRIORITY.get(source, 0)
    existing_priority = VIDEO_SOURCE_PRIORITY.get(existing["metadata_source"], 0) if existing else 0
    authoritative = not existing or incoming_priority >= existing_priority

    def current(name: str, incoming: str, *, useful: bool = True) -> str:
        incoming = (incoming or "").strip()
        if useful and not incoming:
            return existing[name] if existing else ""
        if authoritative and incoming:
            return incoming
        return (existing[name] if existing else "") or incoming

    canonical_title = current("title", title if useful_video_title(title) else "")
    canonical_channel = channel_id if authoritative and channel_id else ((existing["channel_id"] if existing else None) or channel_id)
    incoming_playability = None if is_playable is None else int(bool(is_playable))
    canonical_playability = incoming_playability if incoming_playability is not None else (existing["is_playable"] if existing else None)
    canonical_availability = normalize_video_availability(
        video_id,
        availability,
        canonical_playability,
    )
    if not availability and existing and incoming_playability is None:
        canonical_availability = existing["availability"]
    progress = bounded_int(watch_progress_percent) if watch_progress_percent is not None else (existing["watch_progress_percent"] if existing else 0)
    resume = max(0, int(watch_resume_seconds or 0)) if watch_resume_seconds is not None else (existing["watch_resume_seconds"] if existing else 0)
    last_seen = now if incoming_playability == 1 else (existing["last_seen_available_at"] if existing else None)
    metadata_source = source if authoritative and source else (existing["metadata_source"] if existing else source)
    values = (
        canonical_title,
        current("description", description),
        canonical_channel,
        current("duration_text", duration_text),
        current("view_count", str(view_count or "")),
        current("upload_date", upload_date),
        current("thumbnail_url", thumbnail_url),
        current("thumbnail_path", thumbnail_path),
        current("reaction", reaction),
        progress,
        resume,
        canonical_playability,
        canonical_availability,
        metadata_source,
        fetch_status or (existing["fetch_status"] if existing else ""),
        fetch_error if fetch_status else (existing["fetch_error"] if existing else ""),
        fetched_at or (existing["fetched_at"] if existing else None),
        last_seen,
        checked_at or (existing["last_checked_at"] if existing else None),
        now,
    )
    if existing:
        conn.execute(
            """
            UPDATE videos SET
              title=?, description=?, channel_id=?, duration_text=?, view_count=?, upload_date=?,
              thumbnail_url=?, thumbnail_path=?, reaction=?, watch_progress_percent=?,
              watch_resume_seconds=?, is_playable=?, availability=?, metadata_source=?,
              fetch_status=?, fetch_error=?, fetched_at=?, last_seen_available_at=?,
              last_checked_at=?, updated_at=?
            WHERE video_id=?
            """,
            (*values, video_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO videos(
              video_id, title, description, channel_id, duration_text, view_count, upload_date,
              thumbnail_url, thumbnail_path, reaction, watch_progress_percent,
              watch_resume_seconds, is_playable, availability, metadata_source,
              fetch_status, fetch_error, fetched_at, last_seen_available_at,
              last_checked_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (video_id, *values),
        )
    return video_id


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_days_ago(days: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=max(days, 0))
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_utc_timestamp(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def current_iso_timestamp() -> str:
    return utc_now()


def valid_timezone_name(value: str) -> bool:
    try:
        ZoneInfo((value or "").strip())
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def refresh_exact_history_dates(conn: sqlite3.Connection, timezone_name: str) -> None:
    rows = conn.execute(
        "SELECT event_id, watched_at FROM history_events WHERE time_precision = 'exact'"
    ).fetchall()
    conn.executemany(
        "UPDATE history_events SET watch_date = ? WHERE event_id = ?",
        [
            (local_date_for_utc_instant(row["watched_at"], timezone_name), row["event_id"])
            for row in rows
        ],
    )

def load_cookie_jar(cookie_file: Path) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
    if cookie_file.exists():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except http.cookiejar.LoadError:
            text = cookie_file.read_text(encoding="utf-8", errors="replace")
            header = text.find("# Netscape HTTP Cookie File")
            if header < 0:
                raise
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
                handle.write(text[header:])
                temp_name = handle.name
            try:
                jar = http.cookiejar.MozillaCookieJar(temp_name)
                jar.load(ignore_discard=True, ignore_expires=True)
            finally:
                try:
                    Path(temp_name).unlink()
                except OSError:
                    pass
    return jar


def archivarix_session_status(cookie_file: Path, now: float | None = None) -> tuple[bool, str]:
    """Return whether the local Archivarix login session is still usable."""
    now = time.time() if now is None else now
    try:
        jar = load_cookie_jar(cookie_file)
    except (OSError, http.cookiejar.LoadError):
        return False, "Archivarix cookie file could not be read"
    sessions = [
        cookie
        for cookie in jar
        if cookie.name == "__Secure-better-auth.session_token"
        and cookie.domain.lstrip(".").endswith("archivarix.net")
    ]
    if not sessions:
        return False, "Archivarix login session cookie is missing"
    session = max(sessions, key=lambda cookie: cookie.expires or float("inf"))
    if session.expires is not None and session.expires <= now:
        return False, "Archivarix login session cookie has expired"
    return True, ""


_YOUTUBE_AUTH_COOKIE_NAMES = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-3PSID",
}


def youtube_auth_cookies(cookie_file: Path) -> list[http.cookiejar.Cookie]:
    return [
        cookie
        for cookie in load_cookie_jar(cookie_file)
        if cookie.name in _YOUTUBE_AUTH_COOKIE_NAMES
        and cookie.domain.lstrip(".").endswith(("youtube.com", "google.com"))
    ]


def youtube_cookie_diagnostics(cookie_file: Path, now: float | None = None) -> str:
    now = time.time() if now is None else now
    try:
        sessions = youtube_auth_cookies(cookie_file)
    except (OSError, http.cookiejar.LoadError) as exc:
        return f"cookie_file=unreadable; error={type(exc).__name__}"
    unexpired = [cookie for cookie in sessions if cookie.expires is None or cookie.expires > now]
    non_expiring_auth_cookies = sum(cookie.expires is None for cookie in sessions)
    expirations = [int(cookie.expires) for cookie in unexpired if cookie.expires is not None]
    earliest_expiry = (
        datetime.fromtimestamp(min(expirations), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if expirations
        else "none"
    )
    return (
        f"auth_cookies={len(sessions)}; unexpired={len(unexpired)}; "
        f"non_expiring_auth_cookies={non_expiring_auth_cookies}; earliest_expiry={earliest_expiry}"
    )


def youtube_session_status(
    cookie_file: Path,
    now: float | None = None,
    verify_remote: bool = False,
) -> tuple[bool, str]:
    """Return whether local YouTube cookies are current and, optionally, accepted by YouTube."""
    now = time.time() if now is None else now
    try:
        jar = load_cookie_jar(cookie_file)
    except (OSError, http.cookiejar.LoadError):
        return False, "YouTube cookie file could not be read"
    sessions = [
        cookie
        for cookie in jar
        if cookie.name in _YOUTUBE_AUTH_COOKIE_NAMES
        and cookie.domain.lstrip(".").endswith(("youtube.com", "google.com"))
    ]
    if not sessions:
        return False, "YouTube login cookies are missing"
    if not any(cookie.expires is None or cookie.expires > now for cookie in sessions):
        return False, "YouTube login cookies have expired"
    if verify_remote:
        try:
            page = request_text(
                load_cookie_opener(cookie_file),
                "https://www.youtube.com/feed/history",
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, youtube_request_error_diagnostics(exc, "history authentication check")
        if "Watch history isn't viewable when signed out" in page:
            return False, (
                "YouTube login session is not accepted by YouTube; "
                + youtube_page_diagnostics(page, "history authentication check")
            )
    return True, ""


def youtube_page_requires_login(html_text: str) -> bool:
    markers = (
        "ServiceLogin",
        "accounts.google.com/ServiceLogin",
        "Watch history isn't viewable when signed out",
        "Keep track of what you watch",
    )
    return any(marker in (html_text or "") for marker in markers)


class YouTubeAuthenticationError(RuntimeError):
    def __init__(self, message: str, diagnostics: str = "") -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


def youtube_page_is_authenticated(html_text: str) -> bool:
    return bool(re.search(r'"LOGGED_IN"\s*:\s*true', html_text or ""))


def youtube_page_diagnostics(html_text: str, operation: str) -> str:
    page = html_text or ""
    logged_in_match = re.search(r'"LOGGED_IN"\s*:\s*(true|false)', page)
    logged_in = logged_in_match.group(1) if logged_in_match else "missing"
    marker_patterns = (
        ("service_login", r"ServiceLogin"),
        ("signed_out_history", r"Watch history isn't viewable when signed out"),
        ("consent", r"consent\.youtube\.com|consent\.google\.com"),
        ("bot_check", r"confirm you(?:'|’)re not a bot|protect our community"),
        ("unusual_traffic", r"unusual traffic|/sorry/"),
        ("captcha", r"recaptcha|g-recaptcha"),
    )
    markers = [name for name, pattern in marker_patterns if re.search(pattern, page, re.IGNORECASE)]
    player = extract_json_assignment(page, "ytInitialPlayerResponse")
    playability = player.get("playabilityStatus", {}) if isinstance(player, dict) else {}
    player_status = str(playability.get("status") or "").strip()
    player_reason = re.sub(r"\s+", " ", text_from_runs(playability.get("reason")).strip())
    player_reason = re.sub(r"https?://\S+", "<url>", player_reason)[:160]
    client_name_match = re.search(r'"INNERTUBE_CLIENT_NAME"\s*:\s*"([^"\\]+)"', page)
    client_version_match = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"\\]+)"', page)
    parts = [
        f"operation={operation}",
        f"response_chars={len(page)}",
        f"logged_in={logged_in}",
        f"markers={','.join(markers) if markers else 'none'}",
        f"player_status={player_status or 'missing'}",
    ]
    if player_reason:
        parts.append(f"player_reason={player_reason}")
    if client_name_match:
        parts.append(f"client={client_name_match.group(1)}")
    if client_version_match:
        parts.append(f"client_version={client_version_match.group(1)}")
    return "; ".join(parts)


def youtube_authentication_error(html_text: str, operation: str) -> YouTubeAuthenticationError:
    return YouTubeAuthenticationError(
        "YouTube login session is not accepted by YouTube",
        youtube_page_diagnostics(html_text, operation),
    )


def youtube_request_error_diagnostics(exc: BaseException, operation: str) -> str:
    parts = [f"YouTube request failed; operation={operation}", f"error={type(exc).__name__}"]
    if isinstance(exc, urllib.error.HTTPError):
        parts.append(f"status={exc.code}")
        if exc.reason:
            reason = re.sub(r"https?://\S+", "<url>", str(exc.reason))
            parts.append(f"reason={reason[:120]}")
        retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        if retry_after:
            parts.append(f"retry_after={retry_after[:80]}")
        if content_type:
            parts.append(f"content_type={content_type.split(';', 1)[0][:80]}")
    elif isinstance(exc, urllib.error.URLError):
        reason = re.sub(r"https?://\S+", "<url>", str(exc.reason))
        parts.append(f"reason={reason[:160]}")
    elif isinstance(exc, json.JSONDecodeError):
        parts.append(f"json_line={exc.lineno}")
        parts.append(f"json_column={exc.colno}")
    elif str(exc):
        message = re.sub(r"https?://\S+", "<url>", str(exc))
        parts.append(f"message={message[:160]}")
    return "; ".join(parts)


def load_cookie_opener(cookie_file: Path) -> urllib.request.OpenerDirector:
    jar = load_cookie_jar(cookie_file)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def request_bytes(
    opener: urllib.request.OpenerDirector,
    url: str,
    timeout: int = 30,
    referer: str | None = None,
) -> tuple[bytes, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as response:
        return response.read(), response.headers.get_content_type()


def request_text(opener: urllib.request.OpenerDirector, url: str) -> str:
    body, _ = request_bytes(opener, url)
    return body.decode("utf-8", "replace")


def group_sort_tree(export: dict[str, Any]) -> list[GroupNode]:
    meta = export.get("ysc_meta", {})
    sub_groups = export.get("ysc_settings", {}).get("sub_groups", {})
    nodes: list[GroupNode] = []
    child_names = {
        child_name
        for children in sub_groups.values()
        if isinstance(children, dict)
        for child_name in children.keys()
    }

    top_level_names = [
        name
        for name, value in export.items()
        if isinstance(value, list) and name not in child_names
    ]

    seen: set[str] = set()

    def add_node(name: str, parent_key: str | None, position: int) -> None:
        if name in seen:
            return
        seen.add(name)
        nodes.append(
            GroupNode(
                key=name,
                name=name,
                parent_key=parent_key,
                position=position,
                icon=meta.get(name, {}).get("img", ""),
            )
        )
        children = sub_groups.get(name, {})
        if isinstance(children, dict):
            for child_position, child_name in enumerate(children.keys()):
                add_node(child_name, name, child_position)

    for position, name in enumerate(top_level_names):
        add_node(name, None, position)

    return nodes


def extract_playlist_id(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith("PL") or value.startswith("OLAK"):
        return value
    parsed = urllib.parse.urlparse(value)
    params = urllib.parse.parse_qs(parsed.query)
    if "list" in params and params["list"]:
        return params["list"][0]
    return None


def load_pockettube(path: Path) -> tuple[dict[str, Any], list[GroupNode], dict[str, list[str]]]:
    with path.open("r", encoding="utf-8") as handle:
        export = json.load(handle)
    groups = group_sort_tree(export)
    group_keys_by_name = {node.name: node.key for node in groups}
    memberships: dict[str, list[str]] = {}
    for name, values in export.items():
        if not isinstance(values, list):
            continue
        group_key = group_keys_by_name.get(name, name)
        memberships[group_key] = [
            playlist_id
            for value in values
            if isinstance(value, str)
            for playlist_id in [extract_playlist_id(value)]
            if playlist_id
        ]
    return export, groups, memberships


def extract_json_assignment(html_text: str, name: str) -> dict[str, Any]:
    start = -1
    for pattern in (
        rf"var\s+{re.escape(name)}\s*=",
        rf"window\[['\"]{re.escape(name)}['\"]\]\s*=",
        rf"{re.escape(name)}\s*=",
    ):
        found = re.search(pattern, html_text)
        if found:
            start = found.end()
            break
    if start == -1:
        return {}

    index = html_text.find("{", start)
    if index == -1:
        return {}
    depth = 0
    in_string = False
    escape = False
    for pos in range(index, len(html_text)):
        char = html_text[pos]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html_text[index : pos + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def extract_ytcfg(html_text: str) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for key in (
        "INNERTUBE_API_KEY",
        "INNERTUBE_CLIENT_NAME",
        "INNERTUBE_CLIENT_VERSION",
        "VISITOR_DATA",
        "DELEGATED_SESSION_ID",
    ):
        found = re.search(rf'"{re.escape(key)}":\s*"([^"]*)"', html_text)
        if found:
            config[key] = html.unescape(found.group(1))
    return config


def request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    payload: dict[str, Any],
    referer: str,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.youtube.com",
        "Referer": referer,
    }
    req = urllib.request.Request(url, data=body, headers=headers)
    with opener.open(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def cookie_value(jar: http.cookiejar.CookieJar, names: tuple[str, ...]) -> str:
    for cookie in jar:
        if cookie.name in names and cookie.value:
            return cookie.value
    return ""


def sapisid_auth_header(jar: http.cookiejar.CookieJar, origin: str) -> str:
    sapisid = cookie_value(jar, ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"))
    if not sapisid:
        return ""
    timestamp = str(int(time.time()))
    digest = hashlib.sha1(f"{timestamp} {sapisid} {origin}".encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {timestamp}_{digest}"


def request_youtubei_json(
    opener: urllib.request.OpenerDirector,
    jar: http.cookiejar.CookieJar,
    api_key: str,
    payload: dict[str, Any],
    referer: str,
    client_version: str,
) -> dict[str, Any]:
    origin = "https://www.youtube.com"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": origin,
        "Referer": referer,
        "X-Origin": origin,
        "X-Youtube-Client-Name": "1",
        "X-Youtube-Client-Version": client_version,
    }
    auth = sapisid_auth_header(jar, origin)
    if auth:
        headers["Authorization"] = auth
    url = f"https://www.youtube.com/youtubei/v1/browse?key={urllib.parse.quote(api_key)}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with opener.open(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def text_from_runs(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("simpleText"), str):
        return value["simpleText"]
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(run.get("text", "") for run in runs if isinstance(run, dict))
    content = value.get("content")
    if isinstance(content, str):
        return content
    return ""


def pick_thumbnail(thumbnails: list[dict[str, Any]]) -> str:
    candidates = [
        item
        for item in thumbnails
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    ]
    if not candidates:
        return ""
    best = max(candidates, key=lambda item: int(item.get("width") or 0))
    return html.unescape(best["url"])


def absolute_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("//"):
        return f"https:{value}"
    return value


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("content"), str):
        return value["content"]
    return text_from_runs(value)


def channel_ref_from_endpoint(endpoint: Any) -> str:
    if not isinstance(endpoint, dict):
        return ""
    browse = endpoint.get("browseEndpoint") or {}
    browse_id = str(browse.get("browseId") or "").strip()
    if browse_id.startswith("UC"):
        return browse_id
    canonical_base_url = str(browse.get("canonicalBaseUrl") or "").strip()
    ref = youtube_channel_ref_from_url(canonical_base_url)
    if ref:
        return ref
    command = endpoint.get("commandMetadata", {}).get("webCommandMetadata", {})
    return youtube_channel_ref_from_url(str(command.get("url") or ""))


def rich_text_channel_ref(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for run in value.get("runs") or []:
        if not isinstance(run, dict):
            continue
        ref = channel_ref_from_endpoint(run.get("navigationEndpoint"))
        if ref:
            return ref
    for command_run in value.get("commandRuns") or []:
        if not isinstance(command_run, dict):
            continue
        on_tap = command_run.get("onTap") or {}
        ref = channel_ref_from_endpoint(on_tap.get("innertubeCommand"))
        if ref:
            return ref
    command_context = value.get("rendererContext", {}).get("commandContext", {})
    on_tap = command_context.get("onTap") or {}
    return channel_ref_from_endpoint(on_tap.get("innertubeCommand"))


def playlist_owner_from_metadata_part(part: dict[str, Any]) -> tuple[str, str]:
    text_value = part.get("text")
    owner_text = content_text(text_value).strip()
    if not owner_text.lower().startswith("by "):
        avatar_text = (
            part.get("avatarStack", {})
            .get("avatarStackViewModel", {})
            .get("text")
        )
        owner_text = content_text(avatar_text).strip()
        text_value = avatar_text
    if not owner_text.lower().startswith("by "):
        return "", ""
    return owner_text[3:].strip(), rich_text_channel_ref(text_value)


def image_sources_thumbnail_url(value: Any) -> str:
    best_url = ""
    best_width = -1
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        sources = node.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if not isinstance(url, str) or not url.startswith(("http", "//")):
                continue
            width = int(source.get("width") or 0)
            if width > best_width:
                best_url = html.unescape(url)
                best_width = width
    return absolute_url(best_url)


def playlist_owner_thumbnail_from_metadata_part(part: dict[str, Any]) -> str:
    avatar_stack = (
        part.get("avatarStack", {})
        .get("avatarStackViewModel", {})
    )
    thumbnail = image_sources_thumbnail_url(avatar_stack)
    if thumbnail:
        return thumbnail
    return image_sources_thumbnail_url(part)


def pick_lockup_thumbnail(lockup: dict[str, Any]) -> str:
    best_url = ""
    best_width = -1
    for node in walk(lockup):
        if not isinstance(node, dict):
            continue
        sources = node.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            width = int(source.get("width") or 0)
            if width > best_width:
                best_url = html.unescape(url)
                best_width = width
    return best_url


def lockup_metadata_rows(lockup: dict[str, Any]) -> list[list[str]]:
    metadata = (
        lockup.get("metadata", {})
        .get("lockupMetadataViewModel", {})
        .get("metadata", {})
        .get("contentMetadataViewModel", {})
    )
    rows = []
    for row in metadata.get("metadataRows", []) or []:
        if not isinstance(row, dict):
            continue
        parts = []
        for part in row.get("metadataParts", []) or []:
            if isinstance(part, dict):
                text = content_text(part.get("text")).strip()
                if text:
                    parts.append(text)
        if parts:
            rows.append(parts)
    return rows


def parse_youtube_playlist_video_count(value: str) -> int:
    found = re.search(r"([\d,]+)\s+videos?", value or "", re.I)
    return int(found.group(1).replace(",", "")) if found else 0


def parse_playlist_lockup(lockup: dict[str, Any]) -> dict[str, Any] | None:
    playlist_id = lockup.get("contentId")
    if not isinstance(playlist_id, str) or not playlist_id:
        return None
    if lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_PLAYLIST":
        return None

    title = content_text(
        lockup.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title")
    ).strip()
    rows = lockup_metadata_rows(lockup)
    visibility = ""
    video_count = 0
    updated_text = ""
    for parts in rows:
        joined = " • ".join(parts)
        if "Playlist" in parts and parts:
            visibility = normalize_playlist_visibility(parts[0])
        if not video_count:
            video_count = parse_youtube_playlist_video_count(joined)
        if joined.lower().startswith("updated"):
            updated_text = joined
    if not video_count:
        for node in walk(lockup):
            if not isinstance(node, dict):
                continue
            badge = node.get("thumbnailBadgeViewModel")
            if isinstance(badge, dict):
                text = badge.get("text")
                if isinstance(text, str):
                    video_count = parse_youtube_playlist_video_count(text)
                if video_count:
                    break

    return {
        "playlist_id": playlist_id,
        "title": title or playlist_id,
        "description": updated_text,
        "owner": "",
        "visibility": visibility,
        "video_count": video_count,
        "thumbnail_url": pick_lockup_thumbnail(lockup),
        "url": f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}",
    }


def extract_playlist_metadata(html_text: str, playlist_id: str) -> dict[str, Any]:
    initial_data = extract_json_assignment(html_text, "ytInitialData")
    metadata = {
        "playlist_id": playlist_id,
        "title": "",
        "description": "",
        "owner": "",
        "owner_channel_id": "",
        "owner_thumbnail_url": "",
        "visibility": "",
        "video_count": 0,
        "has_video_count": False,
        "thumbnail_url": "",
        "url": f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}",
    }

    for prop in ("og:title", "twitter:title"):
        found = re.search(
            rf'<meta\s+(?:property|name)="{re.escape(prop)}"\s+content="([^"]*)"',
            html_text,
        )
        if found and not metadata["title"]:
            metadata["title"] = html.unescape(found.group(1))
    found = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', html_text)
    if found:
        metadata["thumbnail_url"] = html.unescape(found.group(1))
    found = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html_text)
    if found:
        metadata["description"] = html.unescape(found.group(1))

    for node in walk(initial_data):
        renderer = node.get("playlistHeaderRenderer")
        if not isinstance(renderer, dict):
            renderer = node.get("pageHeaderRenderer")
        if not isinstance(renderer, dict):
            continue
        title = text_from_runs(renderer.get("title"))
        if title and not metadata["title"]:
            metadata["title"] = title
        description = text_from_runs(renderer.get("description"))
        if description and not metadata["description"]:
            metadata["description"] = description
        owner_rich_text = renderer.get("ownerText") or renderer.get("subtitle")
        owner_text = text_from_runs(owner_rich_text)
        owner, visibility = split_playlist_owner_visibility(owner_text)
        if owner and not metadata["owner"]:
            metadata["owner"] = owner
        owner_channel_id = rich_text_channel_ref(owner_rich_text)
        if owner_channel_id and not metadata["owner_channel_id"]:
            metadata["owner_channel_id"] = owner_channel_id
        if visibility and not metadata["visibility"]:
            metadata["visibility"] = visibility
        for key in ("numVideosText", "numVideosTextText", "videoCountText"):
            count_text = text_from_runs(renderer.get(key))
            if count_text and not metadata["has_video_count"]:
                parsed_count = parse_youtube_playlist_video_count(count_text)
                if re.search(r"[\d,]+\s+videos?", count_text, re.I):
                    metadata["video_count"] = parsed_count
                    metadata["has_video_count"] = True
        thumbnail = renderer.get("playlistHeaderBanner")
        if isinstance(thumbnail, dict):
            thumbs = thumbnail.get("heroPlaylistThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])
            if thumbs and not metadata["thumbnail_url"]:
                metadata["thumbnail_url"] = pick_thumbnail(thumbs)

        for header_node in walk(renderer):
            if not isinstance(header_node, dict):
                continue
            metadata_rows = header_node.get("metadataRows")
            if not isinstance(metadata_rows, list):
                continue
            for row in metadata_rows:
                if not isinstance(row, dict):
                    continue
                for part in row.get("metadataParts", []) or []:
                    if not isinstance(part, dict):
                        continue
                    owner, owner_channel_id = playlist_owner_from_metadata_part(part)
                    if owner and not metadata["owner"]:
                        metadata["owner"] = owner
                    if owner_channel_id and not metadata["owner_channel_id"]:
                        metadata["owner_channel_id"] = owner_channel_id
                    owner_thumbnail_url = playlist_owner_thumbnail_from_metadata_part(part)
                    if owner_thumbnail_url and not metadata["owner_thumbnail_url"]:
                        metadata["owner_thumbnail_url"] = owner_thumbnail_url
                parts = [
                    content_text(part.get("text")).strip()
                    for part in row.get("metadataParts", []) or []
                    if isinstance(part, dict) and content_text(part.get("text")).strip()
                ]
                for part in parts:
                    visibility = normalize_playlist_visibility(part)
                    if visibility and not metadata["visibility"]:
                        metadata["visibility"] = visibility
                # This is YouTube's displayed playlist count, not the local scan total.
                if not metadata["has_video_count"]:
                    for part in parts:
                        parsed_count = parse_youtube_playlist_video_count(part)
                        if re.search(r"[\d,]+\s+videos?", part, re.I):
                            metadata["video_count"] = parsed_count
                            metadata["has_video_count"] = True
                            break

    if not metadata["thumbnail_url"]:
        for node in walk(initial_data):
            thumbnail = node.get("thumbnail")
            if isinstance(thumbnail, dict):
                url = pick_thumbnail(thumbnail.get("thumbnails", []))
                if url:
                    metadata["thumbnail_url"] = url
                    break

    if not metadata["title"]:
        found = re.search(r"<title>(.*?)</title>", html_text, flags=re.DOTALL)
        if found:
            title = html.unescape(re.sub(r"\s+", " ", found.group(1))).strip()
            metadata["title"] = re.sub(r"\s+-\s+YouTube$", "", title)
    if not metadata["title"]:
        metadata["title"] = playlist_id
    return metadata


def thumbnail_extension(content_type: str, url: str) -> str:
    guessed = mimetypes.guess_extension(content_type) if content_type else None
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def local_asset_path(path: Path) -> str:
    resolved = path.resolve()
    for asset_dir in (
        DEFAULT_THUMB_DIR,
        DEFAULT_VIDEO_THUMB_DIR,
        DEFAULT_ARCHIVARIX_THUMB_DIR,
    ):
        try:
            relative = resolved.relative_to(asset_dir.resolve())
        except ValueError:
            continue
        return str(Path(asset_dir.name) / relative).replace("\\", "/")
    return str(resolved.relative_to(ROOT)).replace("\\", "/")


def cache_thumbnail(
    opener: urllib.request.OpenerDirector,
    playlist_id: str,
    thumbnail_url: str,
    thumb_dir: Path,
) -> str:
    if not thumbnail_url:
        return ""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    try:
        body, content_type = request_bytes(
            opener,
            thumbnail_url,
            timeout=30,
            referer=f"https://www.youtube.com/playlist?list={playlist_id}",
        )
    except Exception:
        return ""
    ext = thumbnail_extension(content_type, thumbnail_url)
    target = thumb_dir / f"{safe_name(playlist_id)}{ext}"
    target.write_bytes(body)
    return local_asset_path(target)


def cache_video_thumbnail(
    opener: urllib.request.OpenerDirector,
    video_id: str,
    thumbnail_url: str,
    thumb_dir: Path,
) -> str:
    if not video_id or not thumbnail_url:
        return ""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    try:
        body, content_type = request_bytes(
            opener,
            thumbnail_url,
            timeout=30,
            referer=f"https://www.youtube.com/watch?v={video_id}",
        )
    except Exception:
        return ""
    ext = thumbnail_extension(content_type, thumbnail_url)
    target = thumb_dir / f"{safe_name(video_id)}{ext}"
    target.write_bytes(body)
    return local_asset_path(target)


def cache_channel_thumbnail(
    opener: urllib.request.OpenerDirector,
    subject_id: str,
    thumbnail_url: str,
    thumb_dir: Path,
    referer_url: str = "",
    timeout: int = 30,
) -> str:
    if not subject_id or not thumbnail_url:
        return ""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    try:
        body, content_type = request_bytes(
            opener,
            thumbnail_url,
            timeout=timeout,
            referer=referer_url or f"https://www.youtube.com/watch?v={subject_id}",
        )
    except Exception:
        return ""
    ext = thumbnail_extension(content_type, thumbnail_url)
    target = thumb_dir / f"{safe_name(subject_id)}_channel{ext}"
    target.write_bytes(body)
    return local_asset_path(target)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def format_duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def archivarix_search_deleted(query: str, page_size: int = 50) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"q": query, "page": "1", "pageSize": str(page_size), "status": "deleted"}
    )
    url = f"https://tube.archivarix.net/api/fts?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": "application/json",
            "Referer": f"https://tube.archivarix.net/?q={urllib.parse.quote(query)}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    data = payload.get("data", {})
    videos = data.get("videos", [])
    return videos if isinstance(videos, list) else []


def archivarix_quota_message_from_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if "limit reached" in normalized or "searches per day" in normalized:
        return "Archivarix daily search limit reached"
    return ""


def archivarix_http_error_message(exc: urllib.error.HTTPError) -> str:
    if exc.code == 429:
        return "Archivarix daily search limit reached"
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    return archivarix_quota_message_from_text(body)


def archivarix_lookup_video(
    video_id: str,
    opener: urllib.request.OpenerDirector | None = None,
    channel_cache: dict[str, dict[str, Any]] | None = None,
    stop_event: threading.Event | None = None,
    request_timeout: int = 20,
    stream_timeout: int = 25,
) -> dict[str, Any] | None:
    if stop_event and stop_event.is_set():
        return None
    opener = opener or urllib.request.build_opener()
    channel_cache = channel_cache if channel_cache is not None else {}
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    request = urllib.request.Request(
        "https://tube.archivarix.net/api/search",
        data=json.dumps({"query": youtube_url}).encode("utf-8"),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://tube.archivarix.net/",
        },
        method="POST",
    )
    with opener.open(request, timeout=request_timeout) as response:
        response_text = response.read().decode("utf-8", "replace")
    quota_message = archivarix_quota_message_from_text(response_text)
    if quota_message:
        raise ArchivarixQuotaExceeded(quota_message)
    session = json.loads(response_text).get("data", {})
    endpoint = session.get("sseEndpointUrl")
    if not isinstance(endpoint, str) or not endpoint:
        return None
    stream_url = urllib.parse.urljoin("https://tube.archivarix.net", endpoint)
    stream_request = urllib.request.Request(
        stream_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": "text/event-stream",
            "Referer": f"https://tube.archivarix.net/?q={urllib.parse.quote(youtube_url)}",
        },
    )
    event = ""
    resolved_channel: dict[str, Any] = {}
    with opener.open(stream_request, timeout=stream_timeout) as response:
        for raw_line in response:
            if stop_event and stop_event.is_set():
                return None
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                event = ""
                continue
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload_text = line.partition(":")[2].strip()
            if event == "search:channel_resolved":
                payload = json.loads(payload_text)
                resolved_channel = archivarix_channel_recovery_fields(payload)
                continue
            if event == "search:channel_update":
                payload = json.loads(payload_text)
                resolved_channel.update(archivarix_channel_recovery_fields(payload))
                continue
            if event == "search:video":
                payload = json.loads(payload_text)
                internal_channel_id = str(payload.get("channelId") or "")
                cached_channel = channel_cache.get(internal_channel_id, {}) if internal_channel_id else {}
                channel_fields = dict(cached_channel)
                channel_fields.update({k: v for k, v in resolved_channel.items() if v})
                if internal_channel_id and channel_fields:
                    channel_cache[internal_channel_id] = channel_fields
                apply_archivarix_channel_fields(payload, channel_fields)
                if payload.get("videoId") == video_id:
                    return payload
            if event in {"search:complete", "search:error"}:
                return None
    return None


def archivarix_lookup_channel(
    channel_id: str,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict[str, Any]:
    if not channel_id:
        return {}
    opener = opener or urllib.request.build_opener()
    request = urllib.request.Request(
        "https://tube.archivarix.net/api/search",
        data=json.dumps({"query": channel_id}).encode("utf-8"),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://tube.archivarix.net/",
        },
        method="POST",
    )
    with opener.open(request, timeout=20) as response:
        session = json.loads(response.read().decode("utf-8", "replace")).get("data", {})
    endpoint = session.get("sseEndpointUrl")
    if not isinstance(endpoint, str) or not endpoint:
        return {}
    stream_url = urllib.parse.urljoin("https://tube.archivarix.net", endpoint)
    stream_request = urllib.request.Request(
        stream_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": "text/event-stream",
            "Referer": f"https://tube.archivarix.net/?q={urllib.parse.quote(channel_id)}",
        },
    )
    event = ""
    fields: dict[str, Any] = {}
    with opener.open(stream_request, timeout=25) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                event = ""
                continue
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload_text = line.partition(":")[2].strip()
            if event in {"search:channel_resolved", "search:channel_update"}:
                payload = json.loads(payload_text)
                fields.update({k: v for k, v in archivarix_channel_recovery_fields(payload).items() if v})
                status = str(payload.get("channelStatus") or "").strip()
                if status:
                    fields["channelStatus"] = status
                internal_id = str(payload.get("id") or payload.get("internalId") or "").strip()
                if internal_id:
                    fields["archivarixChannelId"] = internal_id
                if fields.get("channelTitle") and fields.get("channelThumbnailUrl"):
                    return fields
                continue
            if event == "search:video":
                payload = json.loads(payload_text)
                internal_id = str(payload.get("channelId") or "").strip()
                if internal_id:
                    fields["archivarixChannelId"] = internal_id
                return fields
            if event in {"search:complete", "search:error"}:
                return fields
    return fields


def archivarix_channel_recovery_fields(payload: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(payload.get("channelId") or "")
    if channel_id and not channel_id.startswith("UC"):
        channel_id = ""
    channel_url = str(payload.get("channelUrl") or "")
    if not channel_url and channel_id:
        channel_url = f"https://www.youtube.com/channel/{channel_id}"
    thumbnail_url = str(
        payload.get("thumbnailUrl")
        or payload.get("avatarLocalUrl")
        or payload.get("channelThumbnailUrl")
        or payload.get("channelAvatarLocalUrl")
        or ""
    )
    if thumbnail_url.startswith("/"):
        thumbnail_url = urllib.parse.urljoin("https://tube.archivarix.net", thumbnail_url)
    return {
        "channelTitle": str(payload.get("channelTitle") or payload.get("channel") or ""),
        "channelExternalId": channel_id,
        "channelUrl": channel_url,
        "channelThumbnailUrl": thumbnail_url,
        "archivarixChannelId": str(payload.get("archivarixChannelId") or payload.get("internalId") or ""),
        "channelDescription": str(payload.get("channelDescription") or payload.get("description") or ""),
        "channelAliases": ", ".join(str(item) for item in payload.get("channelAliases") or payload.get("aliases") or []),
    }


def apply_archivarix_channel_fields(video: dict[str, Any], channel_fields: dict[str, Any]) -> None:
    if not channel_fields:
        return
    if not video.get("channelTitle") and channel_fields.get("channelTitle"):
        video["channelTitle"] = channel_fields["channelTitle"]
    if not video.get("channelExternalId") and channel_fields.get("channelExternalId"):
        video["channelExternalId"] = channel_fields["channelExternalId"]
    if not video.get("channelUrl") and channel_fields.get("channelUrl"):
        video["channelUrl"] = channel_fields["channelUrl"]
    if not video.get("channelThumbnailUrl") and channel_fields.get("channelThumbnailUrl"):
        video["channelThumbnailUrl"] = channel_fields["channelThumbnailUrl"]


def cache_archivarix_thumbnail(
    video_id: str,
    thumbnail_url: str,
    thumb_dir: Path,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: int = 12,
    stop_event: threading.Event | None = None,
) -> str:
    if not video_id:
        return ""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    opener = opener or urllib.request.build_opener()
    sources = [
        f"https://tube.archivarix.net/media/thumbnails/{video_id[:2]}/{video_id[2:4]}/{video_id}.jpg",
        f"https://web.archive.org/web/0im_/https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    ]
    if thumbnail_url:
        sources.append(thumbnail_url)
    body = b""
    content_type = ""
    for source in sources:
        if stop_event and stop_event.is_set():
            return ""
        try:
            body, content_type = request_bytes(
                opener,
                source,
                timeout=timeout,
                referer=f"https://tube.archivarix.net/?q={urllib.parse.quote(video_id)}",
            )
            if body:
                break
        except Exception:
            continue
    if not body:
        return ""
    ext = thumbnail_extension(content_type, thumbnail_url)
    target = thumb_dir / f"{safe_name(video_id)}{ext}"
    target.write_bytes(body)
    return local_asset_path(target)


def continuation_token(data: dict[str, Any]) -> str:
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("continuationItemRenderer")
        if not isinstance(renderer, dict):
            continue
        endpoint = renderer.get("continuationEndpoint", {})
        command = endpoint.get("continuationCommand", {})
        token = command.get("token")
        if isinstance(token, str) and token:
            return token
    return ""


def playlist_continuation_token(data: dict[str, Any]) -> str:
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        view_model = node.get("continuationItemViewModel")
        if isinstance(view_model, dict):
            token = (
                view_model.get("continuationCommand", {})
                .get("innertubeCommand", {})
                .get("continuationCommand", {})
                .get("token")
            )
            if isinstance(token, str) and token:
                return token
        renderer = node.get("continuationItemRenderer")
        if not isinstance(renderer, dict):
            continue
        endpoint = renderer.get("continuationEndpoint", {})
        executor = endpoint.get("commandExecutorCommand", {})
        for command in executor.get("commands", []) if isinstance(executor, dict) else []:
            if not isinstance(command, dict):
                continue
            token = command.get("continuationCommand", {}).get("token")
            if isinstance(token, str) and token:
                return token
        token = endpoint.get("continuationCommand", {}).get("token")
        if isinstance(token, str) and token:
            return token
    return ""


def playlist_video_renderers(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node["playlistVideoRenderer"]
        for node in walk(data)
        if isinstance(node, dict) and isinstance(node.get("playlistVideoRenderer"), dict)
    ]


def playlist_panel_video_renderers(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node["playlistPanelVideoRenderer"]
        for node in walk(data)
        if isinstance(node, dict) and isinstance(node.get("playlistPanelVideoRenderer"), dict)
    ]


def video_lockup_renderers(data: dict[str, Any]) -> list[dict[str, Any]]:
    lockups = []
    for node in walk(data):
        if not isinstance(node, dict) or not isinstance(node.get("lockupViewModel"), dict):
            continue
        lockup = node["lockupViewModel"]
        if lockup.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
            lockups.append(lockup)
    return lockups


def shorts_lockup_renderers(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node["shortsLockupViewModel"]
        for node in walk(data)
        if isinstance(node, dict) and isinstance(node.get("shortsLockupViewModel"), dict)
    ]


def hidden_alert_count(data: dict[str, Any]) -> int:
    total = 0
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("alertWithButtonRenderer")
        if not isinstance(renderer, dict):
            continue
        text = text_from_runs(renderer.get("text"))
        found = re.search(r"(\d+)\s+unavailable\s+videos?\s+is\s+hidden", text, re.I)
        if not found:
            found = re.search(r"(\d+)\s+unavailable\s+videos?\s+are\s+hidden", text, re.I)
        if found:
            total += int(found.group(1))
    return total


def unavailable_reason(renderer: dict[str, Any], title: str) -> str:
    reason = text_from_runs(renderer.get("unplayableText"))
    if reason:
        return reason
    lower_title = title.strip().lower()
    normalized_title = lower_title.strip("[]() ")
    if normalized_title in {"deleted video", "private video"}:
        return title
    if "unavailable" in lower_title:
        return title
    badges = []
    for badge in renderer.get("badges", []) or []:
        if isinstance(badge, dict):
            label = text_from_runs(badge.get("metadataBadgeRenderer", {}).get("label"))
            if label:
                badges.append(label)
    return ", ".join(badges)


def is_unavailable_video_renderer(renderer: dict[str, Any], title: str, reason: str) -> bool:
    if renderer.get("isPlayable") is False:
        return True
    lower_title = title.strip().lower()
    normalized_title = lower_title.strip("[]() ")
    if normalized_title in {"deleted video", "private video"}:
        return True
    if "unavailable" in lower_title or "deleted" in lower_title or "private" in lower_title:
        return True
    lower_reason = reason.lower()
    return any(word in lower_reason for word in ("unavailable", "deleted", "private"))


def parse_video_renderer(
    playlist_id: str,
    renderer: dict[str, Any],
    fallback_position: int,
) -> dict[str, Any]:
    index_text = text_from_runs(renderer.get("index"))
    found_index = re.search(r"\d+", index_text)
    position = int(found_index.group(0)) if found_index else fallback_position
    video_id = renderer.get("videoId") if isinstance(renderer.get("videoId"), str) else ""
    title = text_from_runs(renderer.get("title")).strip() or "(untitled)"
    channel = text_from_runs(renderer.get("shortBylineText")).strip()
    duration = text_from_runs(renderer.get("lengthText")).strip()
    reason = unavailable_reason(renderer, title)
    unavailable = is_unavailable_video_renderer(renderer, title, reason)
    return {
        "playlist_id": playlist_id,
        "position": position,
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "duration_text": duration,
        "is_playable": 0 if unavailable else 1,
        "availability": reason,
        "url": f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}" if video_id else "",
    }


def parse_panel_video_renderer(
    playlist_id: str,
    renderer: dict[str, Any],
    fallback_position: int,
) -> dict[str, Any]:
    index_text = text_from_runs(renderer.get("indexText") or renderer.get("index"))
    found_index = re.search(r"\d+", index_text)
    position = int(found_index.group(0)) if found_index else fallback_position
    video_id = renderer.get("videoId") if isinstance(renderer.get("videoId"), str) else ""
    title = text_from_runs(renderer.get("title")).strip() or "(untitled)"
    channel = text_from_runs(renderer.get("shortBylineText") or renderer.get("longBylineText")).strip()
    duration = text_from_runs(renderer.get("lengthText")).strip()
    reason = unavailable_reason(renderer, title)
    unavailable = is_unavailable_video_renderer(renderer, title, reason)
    return {
        "playlist_id": playlist_id,
        "position": position,
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "duration_text": duration,
        "is_playable": 0 if unavailable else 1,
        "availability": reason,
        "url": f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}" if video_id else "",
    }


def parse_video_lockup(
    playlist_id: str,
    lockup: dict[str, Any],
    fallback_position: int,
) -> dict[str, Any]:
    video_id = lockup.get("contentId") if isinstance(lockup.get("contentId"), str) else ""
    title = content_text(
        lockup.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title")
    ).strip() or "(untitled)"
    channel = ""
    duration = ""
    rows = lockup_metadata_rows(lockup)
    if rows and rows[0]:
        channel = rows[0][0]
    for node in walk(lockup):
        if not isinstance(node, dict):
            continue
        badge = node.get("thumbnailBadgeViewModel")
        if isinstance(badge, dict):
            text = badge.get("text")
            if isinstance(text, str) and re.search(r"\d+:\d+", text):
                duration = text
                break
    watch_url = ""
    for node in walk(lockup):
        if not isinstance(node, dict):
            continue
        endpoint = node.get("watchEndpoint")
        if isinstance(endpoint, dict) and isinstance(endpoint.get("index"), int):
            fallback_position = int(endpoint["index"]) + 1
            break
    if video_id:
        watch_url = f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}"
    return {
        "playlist_id": playlist_id,
        "position": fallback_position,
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "duration_text": duration,
        "is_playable": 1,
        "availability": "",
        "url": watch_url,
    }


def history_date_from_label(label: str, today: date | None = None) -> str:
    today = today or date.today()
    cleaned = re.sub(r"\s+", " ", label).strip()
    if not cleaned:
        return ""
    lower = cleaned.lower()
    if lower == "today":
        return today.isoformat()
    if lower == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    if lower in weekdays:
        delta = (today.weekday() - weekdays[lower]) % 7
        if delta == 0:
            delta = 7
        return (today - timedelta(days=delta)).isoformat()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d", "%B %d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        year = parsed.year if "%Y" in fmt else today.year
        candidate = date(year, parsed.month, parsed.day)
        if "%Y" not in fmt and candidate > today:
            candidate = date(year - 1, parsed.month, parsed.day)
        return candidate.isoformat()
    return ""


TZ_OFFSETS = {
    "UTC": "+00:00",
    "GMT": "+00:00",
    "PST": "-08:00",
    "PDT": "-07:00",
    "MST": "-07:00",
    "MDT": "-06:00",
    "CST": "-06:00",
    "CDT": "-05:00",
    "EST": "-05:00",
    "EDT": "-04:00",
}


def local_date_for_utc_instant(value: str, timezone_name: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return value[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(zone).date().isoformat()


def takeout_watch_datetime(watched_at: str) -> str:
    cleaned = re.sub(r"\s+", " ", watched_at).strip()
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$", cleaned)
    if iso_match:
        offset = "+00:00" if iso_match.group(3) == "Z" else iso_match.group(3)
        return normalize_utc_timestamp(f"{iso_match.group(1)}T{iso_match.group(2)}{offset}")
    match = re.match(r"^(.*)\s+([A-Z]{2,5})$", cleaned)
    if not match:
        return ""
    datetime_text, tz_name = match.groups()
    offset = TZ_OFFSETS.get(tz_name)
    if not offset:
        return ""
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%B %d, %Y, %I:%M:%S %p"):
        try:
            parsed = datetime.strptime(datetime_text, fmt)
            return normalize_utc_timestamp(parsed.strftime("%Y-%m-%dT%H:%M:%S") + offset)
        except ValueError:
            continue
    return ""


def takeout_watch_date(watched_at: str) -> str:
    iso_value = takeout_watch_datetime(watched_at)
    if iso_value:
        return iso_value[:10]
    cleaned = re.sub(r"\s+", " ", watched_at).strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def bounded_int(value: Any, minimum: int = 0, maximum: int = 100) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(minimum, min(maximum, number))


def compact_json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def reaction_from_button_node(node: dict[str, Any]) -> str:
    blob = compact_json_text(node).lower()
    icon_types = {
        str(item.get("iconType") or "").lower()
        for item in walk(node)
        if isinstance(item, dict) and item.get("iconType")
    }
    if "dislike" in icon_types or re.search(r"\bdislike(?:d)?\b", blob):
        return "D"
    if "like" in icon_types or re.search(r"\blike(?:d)?\b", blob):
        return "L"
    return ""


def extract_reaction_from_initial_data(initial_data: dict[str, Any]) -> str:
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        like_status = node.get("likeStatusEntity")
        if isinstance(like_status, dict):
            status = str(like_status.get("likeStatus") or "").strip().upper()
            if status == "LIKE":
                return "L"
            if status == "DISLIKE":
                return "D"
        if node.get("isToggled") is True or node.get("isToggledButton") is True:
            reaction = reaction_from_button_node(node)
            if reaction:
                return reaction
        toggle_model = node.get("toggleButtonViewModel")
        if isinstance(toggle_model, dict) and toggle_model.get("isToggled") is True:
            reaction = reaction_from_button_node(node)
            if reaction:
                return reaction
    return ""


def extract_watch_status_from_card(card: dict[str, Any], video_id: str) -> tuple[int, int]:
    progress = 0
    resume_seconds = 0
    for node in walk(card):
        if not isinstance(node, dict):
            continue
        resume = node.get("thumbnailOverlayResumePlaybackRenderer")
        if isinstance(resume, dict) and not progress:
            progress = bounded_int(resume.get("percentDurationWatched"))
        progress_model = node.get("thumbnailOverlayProgressBarViewModel")
        if isinstance(progress_model, dict) and not progress:
            progress = bounded_int(progress_model.get("startPercent"))
        endpoint = node.get("watchEndpoint")
        if isinstance(endpoint, dict) and endpoint.get("videoId") == video_id and not resume_seconds:
            start = endpoint.get("startTimeSeconds")
            if isinstance(start, int) and start > 0:
                resume_seconds = start
    return progress, resume_seconds


def find_video_card_watch_status(initial_data: dict[str, Any], video_id: str) -> tuple[int, int]:
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("videoRenderer")
        if isinstance(renderer, dict) and renderer.get("videoId") == video_id:
            progress, resume_seconds = extract_watch_status_from_card(renderer, video_id)
            if progress or resume_seconds:
                return progress, resume_seconds
        lockup = node.get("lockupViewModel")
        if isinstance(lockup, dict) and lockup.get("contentId") == video_id:
            progress, resume_seconds = extract_watch_status_from_card(lockup, video_id)
            if progress or resume_seconds:
                return progress, resume_seconds
    return 0, 0


def parse_history_lockup(lockup: dict[str, Any], normalized_date: str) -> dict[str, Any] | None:
    video_id = lockup.get("contentId") if isinstance(lockup.get("contentId"), str) else ""
    if not video_id:
        for node in walk(lockup):
            if not isinstance(node, dict):
                continue
            endpoint = node.get("watchEndpoint") or node.get("reelWatchEndpoint")
            if isinstance(endpoint, dict) and isinstance(endpoint.get("videoId"), str):
                video_id = endpoint["videoId"]
                break
    if not video_id:
        return None
    title = content_text(
        lockup.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title")
    ).strip()
    rows = lockup_metadata_rows(lockup)
    channel = rows[0][0] if rows and rows[0] else ""
    channel_url = ""
    url = ""
    watch_progress_percent, watch_resume_seconds = extract_watch_status_from_card(lockup, video_id)
    for node in walk(lockup):
        if not isinstance(node, dict):
            continue
        endpoint = node.get("watchEndpoint")
        if isinstance(endpoint, dict) and endpoint.get("videoId") == video_id:
            start = endpoint.get("startTimeSeconds")
            url = f"https://www.youtube.com/watch?v={video_id}"
            if isinstance(start, int) and start > 0:
                url = f"{url}&t={start}s"
                if not watch_resume_seconds:
                    watch_resume_seconds = start
            break
        endpoint = node.get("reelWatchEndpoint")
        if isinstance(endpoint, dict) and endpoint.get("videoId") == video_id:
            url = f"https://www.youtube.com/shorts/{video_id}"
            break
    if not url:
        url = f"https://www.youtube.com/watch?v={video_id}"
    return {
        "video_id": video_id,
        "title": title or video_id,
        "url": url,
        "channel": channel,
        "channel_url": channel_url,
        "watch_date": normalized_date,
        "watch_progress_percent": watch_progress_percent,
        "watch_resume_seconds": watch_resume_seconds,
    }


def history_sections(data: dict[str, Any], today: date | None = None) -> list[tuple[str, str, list[dict[str, Any]]]]:
    sections: list[tuple[str, str, list[dict[str, Any]]]] = []
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("itemSectionRenderer")
        if not isinstance(renderer, dict):
            continue
        title = (
            renderer.get("header", {})
            .get("itemSectionHeaderRenderer", {})
            .get("title", {})
        )
        label = text_from_runs(title).strip()
        if not label:
            continue
        normalized = history_date_from_label(label, today=today)
        rows: list[dict[str, Any]] = []
        for content in renderer.get("contents") or []:
            if not isinstance(content, dict):
                continue
            lockup = content.get("lockupViewModel")
            if isinstance(lockup, dict):
                row = parse_history_lockup(lockup, normalized)
                if row:
                    rows.append(row)
        if rows:
            sections.append((label, normalized, rows))
    return sections


def fetch_youtube_history_web(
    cookie_file: Path,
    limit: int = 100,
    start: int = 1,
    timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> list[dict[str, Any]]:
    jar = load_cookie_jar(cookie_file)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    referer = "https://www.youtube.com/feed/history"
    page = request_text(opener, referer)
    if "Watch history isn't viewable when signed out" in page or "Keep track of what you watch" in page:
        raise RuntimeError("YouTube history page is not signed in with the provided cookie file.")
    initial_data = extract_json_assignment(page, "ytInitialData")
    config = extract_ytcfg(page)
    api_key = config.get("INNERTUBE_API_KEY", "")
    client_version = config.get("INNERTUBE_CLIENT_VERSION", "")
    pages = [initial_data]
    token = continuation_token(initial_data)
    seen_tokens: set[str] = set()
    max_needed = max(1, start) + max(1, limit) - 1
    all_rows: list[dict[str, Any]] = []
    try:
        today = datetime.now(ZoneInfo(timezone_name)).date()
    except ZoneInfoNotFoundError:
        today = datetime.now(timezone.utc).date()

    while pages:
        page_data = pages.pop(0)
        for _label, _normalized, section_rows in history_sections(page_data, today=today):
            all_rows.extend(section_rows)
        if len(all_rows) >= max_needed:
            break
        if not token or token in seen_tokens or not api_key or not client_version:
            break
        seen_tokens.add(token)
        payload = {
            "context": youtube_web_context(config),
            "continuation": token,
        }
        next_page = request_youtubei_json(opener, jar, api_key, payload, referer, client_version)
        pages.append(next_page)
        token = continuation_token(next_page)

    offset = max(1, start) - 1
    return all_rows[offset : offset + max(1, limit)]


def parse_shorts_lockup(
    playlist_id: str,
    renderer: dict[str, Any],
    fallback_position: int,
) -> dict[str, Any]:
    endpoint = (
        renderer.get("onTap", {})
        .get("innertubeCommand", {})
        .get("reelWatchEndpoint", {})
    )
    video_id = endpoint.get("videoId") if isinstance(endpoint.get("videoId"), str) else ""
    text = str(renderer.get("accessibilityText") or "").strip()
    title = text
    if "," in title:
        title = title.rsplit(",", 1)[0].strip()
    if not title:
        title = video_id or "(untitled)"
    return {
        "playlist_id": playlist_id,
        "position": fallback_position,
        "video_id": video_id,
        "title": title,
        "channel": "",
        "duration_text": "Short",
        "is_playable": 1,
        "availability": "",
        "url": f"https://www.youtube.com/shorts/{video_id}" if video_id else "",
    }


def extract_channel_thumbnail_url(initial_data: dict[str, Any]) -> str:
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        avatar = node.get("avatarViewModel")
        if isinstance(avatar, dict):
            image = avatar.get("image")
            if isinstance(image, dict):
                url = pick_thumbnail(image.get("sources", []))
                if url:
                    return url
        for key in ("videoOwnerRenderer", "channelThumbnailWithLinkRenderer"):
            renderer = node.get(key)
            if not isinstance(renderer, dict):
                continue
            thumbnail = renderer.get("thumbnail")
            if isinstance(thumbnail, dict):
                url = pick_thumbnail(thumbnail.get("thumbnails", []))
                if url:
                    return url
    return ""


def youtube_path_url(value: str) -> str:
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        if value.startswith("http://www.youtube.com/") or value.startswith("http://youtube.com/"):
            return "https://" + value[len("http://") :]
        return value
    if value.startswith("/"):
        return f"https://www.youtube.com{value}"
    return ""


def endpoint_channel_url(endpoint: dict[str, Any]) -> str:
    metadata = endpoint.get("commandMetadata", {}).get("webCommandMetadata", {})
    if isinstance(metadata, dict):
        url = youtube_path_url(str(metadata.get("url") or ""))
        if url:
            return url
    browse = endpoint.get("browseEndpoint")
    if isinstance(browse, dict):
        canonical = youtube_path_url(str(browse.get("canonicalBaseUrl") or ""))
        if canonical:
            return canonical
        browse_id = str(browse.get("browseId") or "")
        if browse_id:
            return f"https://www.youtube.com/channel/{urllib.parse.quote(browse_id)}"
    return ""


def extract_channel_handle_aliases(initial_data: dict[str, Any]) -> str:
    aliases: list[str] = []
    seen: set[str] = set()
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        candidates: list[str] = []
        metadata = node.get("webCommandMetadata")
        if isinstance(metadata, dict):
            candidates.append(str(metadata.get("url") or ""))
        browse = node.get("browseEndpoint")
        if isinstance(browse, dict):
            candidates.append(str(browse.get("canonicalBaseUrl") or ""))
        for value in candidates:
            found = re.search(r"(?:^|/)@([A-Za-z0-9._-]+)(?:$|[/?#])", value)
            if not found:
                continue
            alias = f"@{found.group(1)}"
            key = alias.casefold()
            if key not in seen:
                seen.add(key)
                aliases.append(alias)
    return ", ".join(aliases)


def extract_channel_url(initial_data: dict[str, Any]) -> str:
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        for key in ("videoOwnerRenderer", "channelThumbnailWithLinkRenderer"):
            renderer = node.get(key)
            if not isinstance(renderer, dict):
                continue
            endpoint = renderer.get("navigationEndpoint")
            if isinstance(endpoint, dict):
                url = endpoint_channel_url(endpoint)
                if url:
                    return url
    return ""


def extract_channel_id(initial_data: dict[str, Any], channel_url: str = "") -> str:
    channel_id = youtube_channel_id_from_url(channel_url)
    if channel_id:
        return channel_id
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        browse = node.get("browseEndpoint")
        if isinstance(browse, dict):
            browse_id = str(browse.get("browseId") or "")
            if browse_id.startswith("UC"):
                return browse_id
    return ""


def extract_channel_page_metadata(html_text: str, channel_id: str) -> dict[str, str]:
    initial_data = extract_json_assignment(html_text, "ytInitialData")
    title = ""
    channel_url = youtube_channel_url(channel_id)
    thumbnail_url = ""
    found_channel_id = channel_id
    description = ""
    status = ""
    status_reason = ""
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("channelMetadataRenderer")
        if not isinstance(renderer, dict):
            continue
        title = str(renderer.get("title") or title or "").strip()
        description = str(renderer.get("description") or description or "").strip()
        found_channel_id = str(renderer.get("externalId") or found_channel_id or "").strip()
        channel_url = youtube_path_url(str(renderer.get("channelUrl") or "")) or channel_url
        avatar = renderer.get("avatar")
        if isinstance(avatar, dict):
            thumbnail_url = pick_thumbnail(avatar.get("thumbnails", [])) or thumbnail_url
        break
    lower_text = html.unescape(re.sub(r"\s+", " ", html_text)).lower()
    if "this account has been terminated" in lower_text:
        status = "terminated"
        found = re.search(
            r"(This account has been terminated[^<\"{}]+)",
            html_text,
            flags=re.IGNORECASE,
        )
        if found:
            status_reason = html.unescape(re.sub(r"\s+", " ", found.group(1))).strip()
        if not status_reason:
            status_reason = "This account has been terminated."
    elif "this channel was removed because it violated our community guidelines" in lower_text:
        status = "terminated"
        status_reason = "This channel was removed because it violated YouTube Community Guidelines."
    if not thumbnail_url:
        thumbnail_url = extract_channel_thumbnail_url(initial_data)
    if not title:
        found = re.search(r"<title>(.*?)</title>", html_text, flags=re.DOTALL)
        if found:
            title = html.unescape(re.sub(r"\s+", " ", found.group(1))).strip()
            title = re.sub(r"\s+-\s+YouTube$", "", title)
    return {
        "channel_id": found_channel_id or channel_id,
        "channel": title,
        "channel_url": channel_url,
        "channel_description": description,
        "channel_aliases": extract_channel_handle_aliases(initial_data),
        "channel_thumbnail_url": absolute_url(thumbnail_url),
        "channel_status": status,
        "channel_status_reason": status_reason,
        "archivarix_channel_id": "",
    }


def normalized_channel_match(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip()).lower()
    return value.removeprefix("@")


def channel_renderer_metadata(renderer: dict[str, Any]) -> dict[str, str]:
    endpoint = renderer.get("navigationEndpoint")
    channel_url = endpoint_channel_url(endpoint) if isinstance(endpoint, dict) else ""
    thumbnail = renderer.get("thumbnail")
    thumbnail_url = ""
    if isinstance(thumbnail, dict):
        thumbnail_url = pick_thumbnail(thumbnail.get("thumbnails", []))
    return {
        "channel_id": str(renderer.get("channelId") or "").strip(),
        "channel": text_from_runs(renderer.get("title")).strip(),
        "channel_url": channel_url,
        "channel_description": text_from_runs(renderer.get("description")).strip(),
        "channel_aliases": extract_channel_handle_aliases(renderer),
        "channel_thumbnail_url": absolute_url(thumbnail_url),
        "channel_status": "",
        "channel_status_reason": "",
        "archivarix_channel_id": "",
    }


def extract_channel_search_metadata(
    html_text: str,
    channel_id: str,
    fallback_query: str = "",
) -> dict[str, str]:
    initial_data = extract_json_assignment(html_text, "ytInitialData")
    fallback_key = normalized_channel_match(fallback_query)
    title_matches: list[dict[str, str]] = []
    handle_matches: list[dict[str, str]] = []
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("channelRenderer")
        if not isinstance(renderer, dict):
            continue
        metadata = channel_renderer_metadata(renderer)
        found_channel_id = metadata.get("channel_id", "")
        if channel_id and found_channel_id == channel_id:
            return metadata
        if fallback_key:
            title_key = normalized_channel_match(metadata.get("channel", ""))
            url_key = normalized_channel_match(metadata.get("channel_url", "").rstrip("/").rsplit("/", 1)[-1])
            if title_key == fallback_key:
                title_matches.append(metadata)
            if url_key == fallback_key:
                handle_matches.append(metadata)
    if len(handle_matches) == 1:
        return handle_matches[0]
    if len(title_matches) == 1:
        return title_matches[0]
    return {
        "channel_id": channel_id,
        "channel": "",
        "channel_url": youtube_channel_url(channel_id),
        "channel_description": "",
        "channel_aliases": "",
        "channel_thumbnail_url": "",
        "channel_status": "",
        "channel_status_reason": "",
        "archivarix_channel_id": "",
    }


def merge_channel_metadata(primary: dict[str, str], fallback: dict[str, str]) -> dict[str, str]:
    return {
        "channel_id": primary.get("channel_id", "") or fallback.get("channel_id", ""),
        "channel": primary.get("channel", "") or fallback.get("channel", ""),
        "channel_url": primary.get("channel_url", "") or fallback.get("channel_url", ""),
        "channel_description": primary.get("channel_description", "") or fallback.get("channel_description", ""),
        "channel_aliases": primary.get("channel_aliases", "") or fallback.get("channel_aliases", ""),
        "channel_thumbnail_url": primary.get("channel_thumbnail_url", "") or fallback.get("channel_thumbnail_url", ""),
        "channel_status": primary.get("channel_status", "") or fallback.get("channel_status", ""),
        "channel_status_reason": primary.get("channel_status_reason", "") or fallback.get("channel_status_reason", ""),
        "archivarix_channel_id": primary.get("archivarix_channel_id", "") or fallback.get("archivarix_channel_id", ""),
    }


def archivarix_channel_metadata(fields: dict[str, Any], channel_id: str) -> dict[str, str]:
    status = str(fields.get("channelStatus") or "").strip()
    status_reason = "Deleted/terminated channel reported by Archivarix." if status == "deleted" else ""
    return {
        "channel_id": str(fields.get("channelExternalId") or channel_id or "").strip(),
        "channel": str(fields.get("channelTitle") or "").strip(),
        "channel_url": str(fields.get("channelUrl") or youtube_channel_url(channel_id) or "").strip(),
        "channel_description": str(fields.get("channelDescription") or "").strip(),
        "channel_aliases": str(fields.get("channelAliases") or "").strip(),
        "channel_thumbnail_url": absolute_url(str(fields.get("channelThumbnailUrl") or "")),
        "channel_status": status,
        "channel_status_reason": status_reason,
        "archivarix_channel_id": str(fields.get("archivarixChannelId") or "").strip(),
    }


def fetch_channel_metadata(
    opener: urllib.request.OpenerDirector,
    channel_id: str,
    thumb_dir: Path,
    fallback_query: str = "",
    require_authenticated: bool = False,
) -> dict[str, str]:
    channel_url = youtube_channel_url(channel_id)
    page = request_text(opener, channel_url)
    if require_authenticated and not youtube_page_is_authenticated(page):
        raise youtube_authentication_error(page, "channel page")
    metadata = extract_channel_page_metadata(page, channel_id)
    if not (metadata.get("channel") and metadata.get("channel_thumbnail_url")):
        search_terms = [
            term.strip()
            for term in (fallback_query, channel_id)
            if term and term.strip()
        ]
        seen_terms: set[str] = set()
        for term in search_terms:
            term_key = term.lower()
            if term_key in seen_terms:
                continue
            seen_terms.add(term_key)
            search_url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": term})
            search_page = request_text(opener, search_url)
            if require_authenticated and not youtube_page_is_authenticated(search_page):
                raise youtube_authentication_error(search_page, "channel search")
            search_metadata = extract_channel_search_metadata(search_page, channel_id, fallback_query or term)
            metadata = merge_channel_metadata(metadata, search_metadata)
            if metadata.get("channel") and metadata.get("channel_thumbnail_url"):
                break
    if (
        metadata.get("channel_status") == "terminated"
        or not (metadata.get("channel") and metadata.get("channel_thumbnail_url"))
    ):
        try:
            archivarix_opener = load_cookie_opener(ARCHIVARIX_COOKIE_FILE)
            archivarix_fields = archivarix_lookup_channel(channel_id, archivarix_opener)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            archivarix_fields = {}
        if archivarix_fields:
            metadata = merge_channel_metadata(
                metadata,
                archivarix_channel_metadata(archivarix_fields, channel_id),
            )
    metadata["channel_thumbnail_path"] = cache_channel_thumbnail(
        opener,
        metadata.get("channel_id", "") or channel_id,
        metadata.get("channel_thumbnail_url", ""),
        thumb_dir,
        referer_url=channel_url,
    )
    return metadata


def extract_watch_metadata(html_text: str, video_id: str) -> dict[str, str]:
    player = extract_json_assignment(html_text, "ytInitialPlayerResponse")
    initial_data = extract_json_assignment(html_text, "ytInitialData")
    details = player.get("videoDetails", {}) if isinstance(player, dict) else {}
    playability = player.get("playabilityStatus", {}) if isinstance(player, dict) else {}
    microformat = (
        player.get("microformat", {}).get("playerMicroformatRenderer", {})
        if isinstance(player, dict)
        else {}
    )
    title = str(details.get("title") or "").strip()
    if not title:
        found = re.search(r"<title>(.*?)</title>", html_text, flags=re.DOTALL)
        if found:
            title = html.unescape(re.sub(r"\s+", " ", found.group(1))).strip()
            title = re.sub(r"\s+-\s+YouTube$", "", title)
    thumbnails = []
    if isinstance(details.get("thumbnail"), dict):
        thumbnails = details.get("thumbnail", {}).get("thumbnails", []) or []
    thumbnail_url = pick_thumbnail(thumbnails)
    if not thumbnail_url:
        thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    channel_thumbnail_url = extract_channel_thumbnail_url(initial_data)
    channel_url = youtube_path_url(str(microformat.get("ownerProfileUrl") or "")) or extract_channel_url(initial_data)
    channel_id = extract_channel_id(initial_data, channel_url)
    watch_progress_percent, watch_resume_seconds = find_video_card_watch_status(initial_data, video_id)
    reaction = extract_reaction_from_initial_data(initial_data)
    status = str(playability.get("status") or "").strip()
    reason = text_from_runs(playability.get("reason")).strip()
    playability_status = status
    if reason and status and reason not in status:
        status = f"{status}: {reason}"
    channel = str(details.get("author") or "").strip()
    if status.upper().startswith("ERROR") and title in {"", "YouTube", "- YouTube"}:
        channel_id = ""
        channel = ""
        channel_url = ""
        channel_thumbnail_url = ""
    return {
        "video_id": video_id,
        "title": title,
        "description": str(details.get("shortDescription") or "").strip(),
        "channel_id": channel_id,
        "channel": channel,
        "channel_url": channel_url,
        "duration_text": format_duration(details.get("lengthSeconds")),
        "view_count": str(details.get("viewCount") or ""),
        "upload_date": str(microformat.get("uploadDate") or microformat.get("publishDate") or ""),
        "thumbnail_url": thumbnail_url,
        "channel_thumbnail_url": channel_thumbnail_url,
        "reaction": reaction,
        "watch_progress_percent": str(watch_progress_percent),
        "watch_resume_seconds": str(watch_resume_seconds),
        "playability_status": playability_status,
        "yt_status": status or ("OK" if title else ""),
    }


def fetch_video_card_watch_status(
    opener: urllib.request.OpenerDirector,
    video_id: str,
) -> tuple[int, int]:
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": video_id})
    page = request_text(opener, url)
    initial_data = extract_json_assignment(page, "ytInitialData")
    return find_video_card_watch_status(initial_data, video_id)


def fetch_watch_metadata(
    opener: urllib.request.OpenerDirector,
    video_id: str,
    thumb_dir: Path,
    require_authenticated: bool = False,
) -> dict[str, str]:
    watch_url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    page = request_text(opener, watch_url)
    if require_authenticated and not youtube_page_is_authenticated(page):
        raise youtube_authentication_error(page, "watch page")
    metadata = extract_watch_metadata(page, video_id)
    if not bounded_int(metadata.get("watch_progress_percent")) and not int(metadata.get("watch_resume_seconds") or 0):
        try:
            progress, resume_seconds = fetch_video_card_watch_status(opener, video_id)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            progress, resume_seconds = 0, 0
        if progress:
            metadata["watch_progress_percent"] = str(progress)
        if resume_seconds:
            metadata["watch_resume_seconds"] = str(resume_seconds)
    metadata["thumbnail_path"] = cache_video_thumbnail(
        opener,
        video_id,
        metadata["thumbnail_url"],
        thumb_dir,
    )
    metadata["channel_thumbnail_path"] = cache_channel_thumbnail(
        opener,
        video_id,
        metadata.get("channel_thumbnail_url", ""),
        thumb_dir,
    )
    return metadata


def extract_channel_handle(value: str) -> str:
    value = html.unescape((value or "").strip())
    parsed = urllib.parse.urlparse(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value
    found = re.search(r"(?:^|/)@([A-Za-z0-9._-]+)(?:$|[/?#])", path)
    return f"@{found.group(1)}" if found else ""


def resolve_channel_id(
    opener: urllib.request.OpenerDirector,
    value: str,
) -> str:
    value = html.unescape((value or "").strip())
    channel_id = youtube_channel_id_from_url(value)
    if channel_id:
        return channel_id
    if re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", value):
        return value
    handle = extract_channel_handle(value)
    if not handle:
        return ""
    url = f"https://www.youtube.com/{handle}"
    page = request_text(opener, url)
    metadata = extract_channel_page_metadata(page, "")
    return metadata.get("channel_id", "")


def resolve_metadata_target(
    opener: urllib.request.OpenerDirector,
    value: str,
) -> tuple[str, str]:
    value = html.unescape((value or "").strip())
    if not value:
        return "", ""
    channel_id = resolve_channel_id(opener, value)
    if channel_id:
        return "channel", channel_id
    video_id = extract_video_id(value)
    if not video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        video_id = value
    if video_id:
        return "video", video_id
    return "", ""


def store_video_metadata(
    conn: sqlite3.Connection,
    metadata: dict[str, str],
    status: str,
    error: str = "",
    updated_at: str | None = None,
) -> str:
    now = updated_at or utc_now()
    channel_id = upsert_channel(
        conn,
        metadata.get("channel_id", ""),
        title=metadata.get("channel", ""),
        url=metadata.get("channel_url", ""),
        description=metadata.get("channel_description", ""),
        aliases=metadata.get("channel_aliases", ""),
        thumbnail_url=metadata.get("channel_thumbnail_url", ""),
        thumbnail_path=metadata.get("channel_thumbnail_path", ""),
        archivarix_channel_id=metadata.get("archivarix_channel_id", ""),
        status=metadata.get("channel_status", ""),
        status_reason=metadata.get("channel_status_reason", ""),
        source="metadata",
        updated_at=now,
    )
    playability = storable_watch_playability_value(metadata)
    recovered_availability = video_availability_from_recovery_status(metadata.get("yt_status", ""))
    availability = (
        normalize_video_availability(
            metadata.get("video_id", ""),
            metadata.get("availability", ""),
            playability,
            metadata.get("yt_status", ""),
        )
        if playability is not None or (metadata.get("availability") or "").strip() or recovered_availability
        else ""
    )
    upsert_video(
        conn,
        metadata.get("video_id", ""),
        title=metadata.get("title", ""),
        description=metadata.get("description", ""),
        channel_id=channel_id,
        duration_text=metadata.get("duration_text", ""),
        view_count=metadata.get("view_count", ""),
        upload_date=metadata.get("upload_date", ""),
        thumbnail_url=metadata.get("thumbnail_url", ""),
        thumbnail_path=metadata.get("thumbnail_path", ""),
        reaction=metadata.get("reaction", ""),
        watch_progress_percent=metadata.get("watch_progress_percent"),
        watch_resume_seconds=metadata.get("watch_resume_seconds"),
        is_playable=playability,
        availability=availability,
        source="metadata",
        fetch_status=status,
        fetch_error=error,
        fetched_at=now,
        checked_at=now,
        updated_at=now,
    )
    return channel_id


def useful_video_metadata(metadata: dict[str, str]) -> bool:
    title = (metadata.get("title") or "").strip()
    yt_status = (metadata.get("yt_status") or "").strip().upper()
    has_recovered_fields = bool(
        metadata.get("channel_id")
        or metadata.get("channel")
        or metadata.get("thumbnail_path")
    )
    if title in {"", "YouTube", "- YouTube"}:
        return has_recovered_fields
    if yt_status.startswith("ERROR") and not has_recovered_fields:
        return False
    return True


def metadata_from_archivarix_video(video_id: str, video: dict[str, Any], thumbnail_url: str, thumbnail_path: str) -> dict[str, str]:
    return {
        "video_id": video_id,
        "title": str(video.get("title") or "").strip(),
        "description": str(video.get("description") or "").strip(),
        "channel_id": str(video.get("channelExternalId") or "").strip(),
        "channel": str(video.get("channelTitle") or "").strip(),
        "channel_url": str(video.get("channelUrl") or "").strip(),
        "channel_description": str(video.get("channelDescription") or "").strip(),
        "channel_aliases": str(video.get("channelAliases") or "").strip(),
        "channel_thumbnail_url": str(video.get("channelThumbnailUrl") or "").strip(),
        "channel_thumbnail_path": str(video.get("channelThumbnailPath") or "").strip(),
        "archivarix_channel_id": str(video.get("channelId") or "").strip(),
        "channel_status": str(video.get("channelStatus") or "").strip(),
        "channel_status_reason": str(video.get("channelStatusReason") or "").strip(),
        "duration_text": format_duration(video.get("duration")),
        "view_count": str(video.get("viewCount") or ""),
        "upload_date": str(video.get("uploadDate") or ""),
        "thumbnail_url": thumbnail_url or str(video.get("thumbnailArchiveUrl") or video.get("thumbnailUrl") or ""),
        "thumbnail_path": thumbnail_path,
        "reaction": "",
        "watch_progress_percent": "0",
        "watch_resume_seconds": "0",
        "yt_status": str(video.get("status") or ""),
    }


def enrich_archivarix_video_channel(
    video: dict[str, Any],
    channel_id: str,
    archivarix_opener: urllib.request.OpenerDirector,
) -> None:
    if not channel_id:
        return
    try:
        fields = archivarix_lookup_channel(channel_id, archivarix_opener)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        fields = {}
    if not fields:
        return
    channel_metadata = archivarix_channel_metadata(fields, channel_id)
    if channel_metadata.get("channel"):
        video["channelTitle"] = channel_metadata["channel"]
    if channel_metadata.get("channel_id"):
        video["channelExternalId"] = channel_metadata["channel_id"]
    if channel_metadata.get("channel_url"):
        video["channelUrl"] = channel_metadata["channel_url"]
    if channel_metadata.get("channel_description"):
        video["channelDescription"] = channel_metadata["channel_description"]
    if channel_metadata.get("channel_aliases"):
        video["channelAliases"] = channel_metadata["channel_aliases"]
    if channel_metadata.get("channel_thumbnail_url"):
        video["channelThumbnailUrl"] = channel_metadata["channel_thumbnail_url"]
    if channel_metadata.get("archivarix_channel_id"):
        video["channelId"] = channel_metadata["archivarix_channel_id"]
    if channel_metadata.get("channel_status"):
        video["channelStatus"] = channel_metadata["channel_status"]
    if channel_metadata.get("channel_status_reason"):
        video["channelStatusReason"] = channel_metadata["channel_status_reason"]


def store_channel_metadata(
    conn: sqlite3.Connection,
    metadata: dict[str, str],
    status: str,
    error: str = "",
    updated_at: str | None = None,
) -> str:
    now = updated_at or utc_now()
    return upsert_channel(
        conn,
        metadata.get("channel_id", ""),
        title=metadata.get("channel", ""),
        url=metadata.get("channel_url", ""),
        description=metadata.get("channel_description", ""),
        aliases=metadata.get("channel_aliases", ""),
        thumbnail_url=metadata.get("channel_thumbnail_url", ""),
        thumbnail_path=metadata.get("channel_thumbnail_path", ""),
        archivarix_channel_id=metadata.get("archivarix_channel_id", ""),
        status=metadata.get("channel_status", ""),
        status_reason=metadata.get("channel_status_reason", ""),
        fetch_status=status,
        fetch_error=error,
        fetched_at=now,
        source="metadata",
        updated_at=now,
    )


def video_metadata_channel_id(metadata: dict[str, str]) -> str:
    return (metadata.get("channel_id") or youtube_channel_id_from_url(metadata.get("channel_url", ""))).strip()


def fetch_new_channel_metadata_if_needed(
    conn: sqlite3.Connection,
    opener: urllib.request.OpenerDirector,
    thumb_dir: Path,
    video_metadata: dict[str, str],
    require_authenticated: bool = False,
) -> tuple[dict[str, str], str, str]:
    channel_id = video_metadata_channel_id(video_metadata)
    if not channel_id:
        return {}, "", ""
    if conn.execute("SELECT 1 FROM channels WHERE channel_id = ?", (channel_id,)).fetchone():
        return {}, "", ""
    try:
        metadata = fetch_channel_metadata(
            opener,
            channel_id,
            thumb_dir,
            fallback_query=video_metadata.get("channel", ""),
            require_authenticated=require_authenticated,
        )
        status = (
            "ok"
            if metadata.get("channel")
            or metadata.get("channel_url")
            or metadata.get("channel_thumbnail_path")
            else "no_metadata"
        )
        return metadata, status, ""
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return (
            {
                "channel_id": channel_id,
                "channel": video_metadata.get("channel", ""),
                "channel_url": video_metadata.get("channel_url", ""),
                "channel_thumbnail_url": video_metadata.get("channel_thumbnail_url", ""),
                "channel_thumbnail_path": video_metadata.get("channel_thumbnail_path", ""),
            },
            "error",
            str(exc),
        )


def fetch_provided_metadata(
    conn: sqlite3.Connection,
    opener: urllib.request.OpenerDirector,
    thumb_dir: Path,
    target: str,
) -> dict[str, str]:
    source, subject_id = resolve_metadata_target(opener, target)
    if not source or not subject_id:
        raise ValueError("Enter a YouTube watch URL, video ID, channel URL, channel ID, or @handle.")
    now = utc_now()
    if source == "channel":
        metadata = fetch_channel_metadata(opener, subject_id, thumb_dir, fallback_query=target)
        status = "ok" if (
            metadata.get("channel")
            or metadata.get("channel_url")
            or metadata.get("channel_thumbnail_path")
        ) else "no_metadata"
        channel_id = store_channel_metadata(conn, metadata, status, updated_at=now)
        return {
            "source": source,
            "subject_id": channel_id or subject_id,
            "status": status,
            "title": metadata.get("channel", "") or channel_id or subject_id,
        }
    metadata = fetch_watch_metadata(opener, subject_id, thumb_dir)
    status = "ok" if useful_video_metadata(metadata) else "no_metadata"
    if status != "ok":
        try:
            archivarix_opener = load_cookie_opener(ARCHIVARIX_COOKIE_FILE)
            video, thumbnail_url, thumbnail_path, arch_status, arch_error = recover_archivarix_video(
                subject_id,
                thumb_dir,
                archivarix_opener,
                refresh_metadata=True,
                channel_cache={},
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            video = None
            thumbnail_url = ""
            thumbnail_path = ""
            arch_status = "error"
            arch_error = "Archivarix fallback failed"
        if video:
            channel_id = str(video.get("channelExternalId") or "")
            enrich_archivarix_video_channel(video, channel_id, archivarix_opener)
            if video.get("channelThumbnailUrl") and not video.get("channelThumbnailPath"):
                video["channelThumbnailPath"] = cache_channel_thumbnail(
                    archivarix_opener,
                    channel_id or subject_id,
                    str(video.get("channelThumbnailUrl") or ""),
                    thumb_dir,
                )
            metadata = metadata_from_archivarix_video(subject_id, video, thumbnail_url, thumbnail_path)
            status = "ok" if useful_video_metadata(metadata) else "no_metadata"
            save_video_recovery(
                conn,
                subject_id,
                video,
                arch_status,
                arch_error,
            )
    channel_metadata: dict[str, str] = {}
    channel_status = ""
    channel_error = ""
    if status == "ok":
        channel_metadata, channel_status, channel_error = fetch_new_channel_metadata_if_needed(
            conn,
            opener,
            thumb_dir,
            metadata,
        )
    if channel_status:
        store_channel_metadata(conn, channel_metadata, channel_status, channel_error, updated_at=now)
    store_video_metadata(conn, metadata, status, updated_at=now)
    return {
        "source": source,
        "subject_id": subject_id,
        "status": status,
        "title": metadata.get("title", "") or subject_id,
    }


def scan_playlist_videos_ytdlp(
    playlist_id: str,
    cookie_file: Path,
) -> list[dict[str, Any]]:
    videos, _metadata = scan_playlist_ytdlp(playlist_id, cookie_file)
    return videos


def playlist_metadata_from_ytdlp_info(info: dict[str, Any], playlist_id: str) -> dict[str, Any]:
    title = str(info.get("title") or info.get("playlist_title") or "").strip()
    description = str(info.get("description") or "").strip()
    owner, inferred_visibility = split_playlist_owner_visibility(
        str(info.get("uploader") or info.get("channel") or "")
    )
    visibility = normalize_playlist_visibility(
        str(info.get("visibility") or info.get("privacy") or info.get("availability") or "")
    ) or inferred_visibility
    if owner:
        visibility = ""
    elif visibility:
        owner = ""
    playlist_count = info.get("playlist_count") or info.get("n_entries")
    video_count = int(playlist_count or 0)
    thumbnail_url = str(info.get("thumbnail") or "").strip()
    webpage_url = str(info.get("webpage_url") or info.get("original_url") or "").strip()
    if not webpage_url:
        webpage_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
    owner_channel_id = (
        str(info.get("channel_id") or info.get("uploader_id") or "").strip()
        or youtube_channel_ref_from_url(str(info.get("channel_url") or info.get("uploader_url") or ""))
    )
    return {
        "playlist_id": playlist_id,
        "title": title or playlist_id,
        "description": description,
        "owner": owner,
        "owner_channel_id": owner_channel_id,
        "owner_thumbnail_url": "",
        "visibility": visibility,
        "video_count": video_count,
        "thumbnail_url": thumbnail_url,
        "url": webpage_url,
    }


def scan_playlist_ytdlp(
    playlist_id: str,
    cookie_file: Path,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed") from exc

    url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
    messages: list[str] = []

    class YtdlpLogger:
        def debug(self, message: str) -> None:
            return

        def warning(self, message: str) -> None:
            messages.append(message)

        def error(self, message: str) -> None:
            messages.append(message)

    options = {
        "cookiefile": str(cookie_file) if cookie_file.exists() else None,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "logger": YtdlpLogger(),
        "no_warnings": True,
        "quiet": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        text = "\n".join(messages).strip()
        raise RuntimeError(text or "yt-dlp returned no playlist data")
    metadata = playlist_metadata_from_ytdlp_info(info, playlist_id)
    entries = [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
    videos: list[dict[str, Any]] = []
    for position, entry in enumerate(entries, start=1):
        video_id = str(entry.get("id") or entry.get("url") or "")
        if not video_id:
            continue
        title = str(entry.get("title") or video_id).strip()
        channel = str(entry.get("channel") or entry.get("uploader") or "").strip()
        channel_id = str(entry.get("channel_id") or entry.get("uploader_id") or "").strip()
        channel_url = str(entry.get("channel_url") or entry.get("uploader_url") or "").strip()
        if not channel_id:
            channel_id = youtube_channel_id_from_url(channel_url)
        duration = format_duration(entry.get("duration"))
        webpage_url = str(entry.get("webpage_url") or "")
        if not webpage_url:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}"
        availability = str(entry.get("availability") or "").strip()
        hidden = playlist_entry_is_unavailable(title, availability)
        if hidden and not availability:
            availability = "unavailable"
        availability = normalize_video_availability(video_id, availability, not hidden)
        videos.append(
            {
                "playlist_id": playlist_id,
                "position": position,
                "video_id": video_id,
                "title": title,
                "channel_id": channel_id,
                "channel": channel,
                "duration_text": duration,
                "is_playable": 0 if hidden else 1,
                "availability": availability,
                "url": webpage_url,
            }
        )
    return videos, metadata


def fetch_youtube_history_ytdlp(cookie_file: Path, limit: int = 100, start: int = 1) -> list[dict[str, Any]]:
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed. Run: pip install -r requirements.txt") from exc

    class YtdlpLogger:
        def debug(self, msg: str) -> None:
            pass

        def info(self, msg: str) -> None:
            pass

        def warning(self, msg: str) -> None:
            pass

        def error(self, msg: str) -> None:
            pass

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playliststart": max(1, start),
        "playlistend": max(1, start) + max(1, limit) - 1,
        "cookiefile": str(cookie_file) if cookie_file.exists() else None,
        "logger": YtdlpLogger(),
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(":ythistory", download=False)
    entries = (info or {}).get("entries") or []
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id") or extract_video_id(entry.get("url") or "")
        if not video_id:
            continue
        url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
        if url.startswith("http"):
            watch_url = url
        else:
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
        rows.append(
            {
                "video_id": video_id,
                "title": entry.get("title") or video_id,
                "url": watch_url,
                "channel_id": entry.get("channel_id") or entry.get("uploader_id") or youtube_channel_id_from_url(entry.get("channel_url") or entry.get("uploader_url") or ""),
                "channel": entry.get("channel") or entry.get("uploader") or "",
                "channel_url": entry.get("channel_url") or entry.get("uploader_url") or "",
            }
        )
    return rows


def scan_playlist_videos(
    opener: urllib.request.OpenerDirector,
    playlist_id: str,
    cookie_file: Path | None = None,
) -> list[dict[str, Any]]:
    playlist_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
    page = request_text(opener, playlist_url)
    initial_data = extract_json_assignment(page, "ytInitialData")
    config = extract_ytcfg(page)
    pages = [initial_data]
    token = playlist_continuation_token(initial_data)

    api_key = config.get("INNERTUBE_API_KEY", "")
    client_name = config.get("INNERTUBE_CLIENT_NAME", "WEB")
    client_version = config.get("INNERTUBE_CLIENT_VERSION", "")
    visitor_data = config.get("VISITOR_DATA", "")
    cookie_jar = load_cookie_jar(cookie_file) if cookie_file else None
    seen_tokens: set[str] = set()
    while token and token not in seen_tokens and api_key and client_version:
        seen_tokens.add(token)
        payload = {
            "context": {
                "client": {
                    "clientName": client_name,
                    "clientVersion": client_version,
                    "visitorData": urllib.parse.unquote(visitor_data),
                }
            },
            "continuation": token,
        }
        if cookie_jar:
            data = request_youtubei_json(
                opener,
                cookie_jar,
                api_key,
                payload,
                playlist_url,
                client_version,
            )
        else:
            data = request_json(
                opener,
                f"https://www.youtube.com/youtubei/v1/browse?key={urllib.parse.quote(api_key)}",
                payload,
                playlist_url,
            )
        pages.append(data)
        token = playlist_continuation_token(data)

    videos: list[dict[str, Any]] = []
    seen_positions: set[int] = set()
    seen_video_ids: set[str] = set()

    def add_video(video: dict[str, Any]) -> None:
        video_id = video.get("video_id") or ""
        if video_id and video_id in seen_video_ids:
            return
        while video["position"] in seen_positions:
            video["position"] += 1
        seen_positions.add(video["position"])
        if video_id:
            seen_video_ids.add(video_id)
        videos.append(video)

    hidden_alerts = 0
    for page_data in pages:
        hidden_alerts += hidden_alert_count(page_data)
        for renderer in playlist_video_renderers(page_data):
            fallback = len(videos) + 1
            video = parse_video_renderer(playlist_id, renderer, fallback)
            add_video(video)
        for lockup in video_lockup_renderers(page_data):
            fallback = len(videos) + 1
            add_video(parse_video_lockup(playlist_id, lockup, fallback))
        for renderer in shorts_lockup_renderers(page_data):
            fallback = len(videos) + 1
            add_video(parse_shorts_lockup(playlist_id, renderer, fallback))

    first_video_id = next((video["video_id"] for video in videos if video.get("video_id")), "")
    if first_video_id:
        try:
            watch_url = (
                f"https://www.youtube.com/watch?v={urllib.parse.quote(first_video_id)}"
                f"&list={urllib.parse.quote(playlist_id)}"
            )
            watch_page = request_text(opener, watch_url)
            watch_data = extract_json_assignment(watch_page, "ytInitialData")
            panel_videos: list[dict[str, Any]] = []
            panel_positions: set[int] = set()
            panel_ids: set[str] = set()
            for renderer in playlist_panel_video_renderers(watch_data):
                video = parse_panel_video_renderer(
                    playlist_id,
                    renderer,
                    len(panel_videos) + 1,
                )
                video_id = video.get("video_id") or ""
                if video_id and video_id in panel_ids:
                    continue
                while video["position"] in panel_positions:
                    video["position"] += 1
                panel_positions.add(video["position"])
                if video_id:
                    panel_ids.add(video_id)
                panel_videos.append(video)
            if len(panel_videos) > len(videos):
                videos = panel_videos
                seen_positions = panel_positions
                seen_video_ids = panel_ids
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            pass

    explicit_hidden = sum(1 for video in videos if not video["is_playable"])
    for offset in range(max(0, hidden_alerts - explicit_hidden)):
        position = max(seen_positions, default=0) + 1
        seen_positions.add(position)
        videos.append(
            {
                "playlist_id": playlist_id,
                "position": position,
                "video_id": "",
                "title": "Unavailable video hidden by YouTube",
                "channel": "",
                "duration_text": "",
                "is_playable": 0,
                "availability": "Unavailable video is hidden",
                "url": "",
            }
        )
    return sorted(videos, key=lambda item: item["position"])


def import_playlists(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    thumb_dir = Path(args.thumbs)
    _, groups, memberships = load_pockettube(Path(args.pockettube))
    all_playlist_ids = []
    seen: set[str] = set()
    for playlist_ids in memberships.values():
        for playlist_id in playlist_ids:
            if playlist_id not in seen:
                all_playlist_ids.append(playlist_id)
                seen.add(playlist_id)

    opener = load_cookie_opener(Path(args.cookies))
    conn = connect(db_path)
    with conn:
        conn.execute("DELETE FROM group_playlists")
        conn.execute("DELETE FROM groups")
        for node in groups:
            conn.execute(
                """
                INSERT INTO groups(group_key, name, parent_key, position, icon)
                VALUES (?, ?, ?, ?, ?)
                """,
                (node.key, node.name, node.parent_key, node.position, node.icon),
            )

    print(f"Importing {len(all_playlist_ids)} playlists from {len(groups)} PocketTube groups...")
    for index, playlist_id in enumerate(all_playlist_ids, start=1):
        url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
        status = "ok"
        error = ""
        try:
            page = request_text(opener, url)
            metadata = extract_playlist_metadata(page, playlist_id)
            thumbnail_path = cache_thumbnail(
                opener, playlist_id, metadata["thumbnail_url"], thumb_dir
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            metadata = {
                "playlist_id": playlist_id,
                "title": playlist_id,
                "description": "",
                "owner": "",
                "owner_channel_id": "",
                "owner_thumbnail_url": "",
                "visibility": "",
                "video_count": 0,
                "thumbnail_url": "",
                "url": url,
            }
            thumbnail_path = ""
            status = "error"
            error = str(exc)
        with conn:
            owner_channel_id = metadata.get("owner_channel_id", "")
            owner_thumbnail_path = ""
            if owner_channel_id and metadata.get("owner_thumbnail_url"):
                owner_thumbnail_path = cache_channel_thumbnail(
                    opener,
                    owner_channel_id,
                    metadata.get("owner_thumbnail_url", ""),
                    DEFAULT_VIDEO_THUMB_DIR,
                    referer_url=url,
                )
            if owner_channel_id:
                owner_channel_id = upsert_channel(
                    conn,
                    owner_channel_id,
                    title=metadata.get("owner", ""),
                    thumbnail_url=metadata.get("owner_thumbnail_url", ""),
                    thumbnail_path=owner_thumbnail_path,
                    source="playlist_owner",
                    updated_at=utc_now(),
                )
            conn.execute(
                """
                INSERT INTO playlists(
                  playlist_id, title, description, owner_channel_id, visibility, video_count,
                  thumbnail_url, thumbnail_path, fetch_status, fetch_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                  title=excluded.title,
                  description=excluded.description,
                  owner_channel_id=CASE
                    WHEN NULLIF(excluded.visibility, '') IS NOT NULL THEN NULL
                    ELSE COALESCE(excluded.owner_channel_id, playlists.owner_channel_id)
                  END,
                  visibility=CASE
                    WHEN excluded.owner_channel_id IS NOT NULL THEN ''
                    ELSE COALESCE(NULLIF(excluded.visibility, ''), playlists.visibility)
                  END,
                  video_count=excluded.video_count,
                  thumbnail_url=excluded.thumbnail_url,
                  thumbnail_path=excluded.thumbnail_path,
                  fetch_status=excluded.fetch_status,
                  fetch_error=excluded.fetch_error,
                  updated_at=excluded.updated_at
                """,
                (
                    playlist_id,
                    metadata["title"],
                    metadata["description"],
                    owner_channel_id or None,
                    metadata["visibility"],
                    metadata["video_count"],
                    metadata["thumbnail_url"],
                    thumbnail_path,
                    status,
                    error,
                    utc_now(),
                ),
            )
        print(f"[{index:03d}/{len(all_playlist_ids):03d}] {status} {metadata['title']}")

    with conn:
        for group_key, playlist_ids in memberships.items():
            for position, playlist_id in enumerate(playlist_ids):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO group_playlists(group_key, playlist_id, position)
                    VALUES (?, ?, ?)
                    """,
                    (group_key, playlist_id, position),
                )
    print(f"Wrote {db_path}")


def youtube_web_context(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "client": {
            "clientName": config.get("INNERTUBE_CLIENT_NAME", "WEB"),
            "clientVersion": config.get("INNERTUBE_CLIENT_VERSION", ""),
            "visitorData": urllib.parse.unquote(config.get("VISITOR_DATA", "")),
        }
    }


def fetch_current_youtube_playlists(
    cookie_file: Path,
    browse_id: str = "FEplaylist_aggregation",
) -> tuple[urllib.request.OpenerDirector, list[dict[str, str]]]:
    jar = load_cookie_jar(cookie_file)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    referer = "https://www.youtube.com/feed/playlists"
    page = request_text(opener, referer)
    config = extract_ytcfg(page)
    api_key = config.get("INNERTUBE_API_KEY", "")
    client_version = config.get("INNERTUBE_CLIENT_VERSION", "")
    if not api_key or not client_version:
        raise RuntimeError("Could not find YouTube web API configuration in the playlists page.")

    payload = {
        "context": youtube_web_context(config),
        "browseId": browse_id,
    }
    pages = [
        request_youtubei_json(opener, jar, api_key, payload, referer, client_version)
    ]
    token = continuation_token(pages[0])
    seen_tokens: set[str] = set()
    while token and token not in seen_tokens:
        seen_tokens.add(token)
        pages.append(
            request_youtubei_json(
                opener,
                jar,
                api_key,
                {"context": youtube_web_context(config), "continuation": token},
                referer,
                client_version,
            )
        )
        token = continuation_token(pages[-1])

    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for page_data in pages:
        for node in walk(page_data):
            if not isinstance(node, dict):
                continue
            lockup = node.get("lockupViewModel")
            if not isinstance(lockup, dict):
                continue
            record = parse_playlist_lockup(lockup)
            if not record:
                continue
            playlist_id = record["playlist_id"]
            if playlist_id in seen_ids:
                continue
            seen_ids.add(playlist_id)
            records.append(record)
    return opener, records


def is_system_playlist(playlist_id: str) -> bool:
    return playlist_id in {"LL", "LM", "WL"} or playlist_id.startswith("RD")


def discover_current_playlists(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    thumb_dir = Path(args.thumbs)
    opener, records = fetch_current_youtube_playlists(Path(args.cookies), args.browse_id)
    if not args.include_system:
        records = [record for record in records if not is_system_playlist(record["playlist_id"])]

    conn = connect(db_path)
    existing_groups = {
        row["playlist_id"]
        for row in conn.execute("SELECT DISTINCT playlist_id FROM group_playlists")
    }
    inserted_ungrouped: list[str] = []
    with conn:
        top_position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM groups WHERE parent_key IS NULL"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO groups(group_key, name, parent_key, position, icon)
            VALUES (?, ?, NULL, ?, '')
            ON CONFLICT(group_key) DO UPDATE SET
              name=excluded.name,
              parent_key=NULL,
              position=groups.position
            """,
            (args.group_key, args.group_name, top_position),
        )
        group_position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM group_playlists WHERE group_key = ?",
            (args.group_key,),
        ).fetchone()[0]

    print(f"Discovered {len(records)} current YouTube playlists.")
    for index, record in enumerate(records, start=1):
        playlist_id = record["playlist_id"]
        existing = conn.execute(
            "SELECT thumbnail_path FROM playlists WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        thumbnail_path = cache_thumbnail(
            opener,
            playlist_id,
            record["thumbnail_url"],
            thumb_dir,
        )
        if not thumbnail_path and existing:
            thumbnail_path = existing["thumbnail_path"]
        with conn:
            owner_channel_id = record.get("owner_channel_id", "")
            owner_thumbnail_path = ""
            if owner_channel_id and record.get("owner_thumbnail_url"):
                owner_thumbnail_path = cache_channel_thumbnail(
                    opener,
                    owner_channel_id,
                    record.get("owner_thumbnail_url", ""),
                    DEFAULT_VIDEO_THUMB_DIR,
                    referer_url=record["url"],
                )
            if owner_channel_id:
                owner_channel_id = upsert_channel(
                    conn,
                    owner_channel_id,
                    title=record.get("owner", ""),
                    thumbnail_url=record.get("owner_thumbnail_url", ""),
                    thumbnail_path=owner_thumbnail_path,
                    source="playlist_owner",
                    updated_at=utc_now(),
                )
            conn.execute(
                """
                INSERT INTO playlists(
                  playlist_id, title, description, owner_channel_id, visibility, video_count,
                  thumbnail_url, thumbnail_path, fetch_status, fetch_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', '', ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                  title=excluded.title,
                  description=excluded.description,
                  owner_channel_id=CASE
                    WHEN NULLIF(excluded.visibility, '') IS NOT NULL THEN NULL
                    ELSE COALESCE(excluded.owner_channel_id, playlists.owner_channel_id)
                  END,
                  visibility=CASE
                    WHEN excluded.owner_channel_id IS NOT NULL THEN ''
                    ELSE COALESCE(NULLIF(excluded.visibility, ''), playlists.visibility)
                  END,
                  video_count=excluded.video_count,
                  thumbnail_url=excluded.thumbnail_url,
                  thumbnail_path=excluded.thumbnail_path,
                  fetch_status='ok',
                  fetch_error='',
                  updated_at=excluded.updated_at
                """,
                (
                    playlist_id,
                    record["title"],
                    record["description"],
                    owner_channel_id or None,
                    record["visibility"],
                    record["video_count"],
                    record["thumbnail_url"],
                    thumbnail_path,
                    utc_now(),
                ),
            )
            if playlist_id not in existing_groups:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO group_playlists(group_key, playlist_id, position)
                    VALUES (?, ?, ?)
                    """,
                    (args.group_key, playlist_id, group_position),
                )
                existing_groups.add(playlist_id)
                inserted_ungrouped.append(playlist_id)
                group_position += 1
        print(f"[{index:03d}/{len(records):03d}] {record['title']}")

    print(
        f"Updated {len(records)} playlists; added {len(inserted_ungrouped)} "
        f"to {args.group_name}."
    )
    print(f"Wrote {db_path}")


def log_worker_event(
    conn: sqlite3.Connection,
    run_id: str,
    level: str,
    message: str,
    video_id: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO metadata_worker_log(run_id, created_at, level, video_id, message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, utc_now(), level, video_id, message),
    )


def log_playlist_scan_event(
    conn: sqlite3.Connection,
    run_id: str,
    level: str,
    message: str,
    playlist_id: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO playlist_scan_worker_log(run_id, created_at, level, playlist_id, message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, utc_now(), level, playlist_id, message),
    )


def log_live_history_event(
    conn: sqlite3.Connection,
    run_id: str,
    level: str,
    message: str,
    video_id: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO live_history_worker_log(run_id, created_at, level, video_id, message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, utc_now(), level, video_id, message),
    )


def log_placeholder_recovery_event(
    conn: sqlite3.Connection,
    run_id: str,
    level: str,
    message: str,
    video_id: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO placeholder_recovery_worker_log(run_id, created_at, level, video_id, message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, utc_now(), level, video_id, message),
    )


def playlist_placeholder_recovery_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
    playlist_id: str = "",
) -> list[sqlite3.Row]:
    where = [
        "pi.video_id IS NOT NULL",
        "(pi.membership_state = 'retained_unavailable' OR v.is_playable = 0)",
    ]
    if not force:
        where.append("(r.video_id IS NULL OR r.search_status = 'error')")
    params: list[Any] = []
    if playlist_id:
        where.append("pi.playlist_id = ?")
        params.append(playlist_id)
    sql = f"""
        SELECT pi.video_id,
               MIN(pi.position) AS display_position,
               MIN(p.title) AS playlist_title,
               MIN(v.title) AS title,
               COUNT(DISTINCT pi.playlist_id) AS playlist_count,
               COALESCE(r.search_status, '') AS previous_status
        FROM playlist_items pi
        JOIN playlists p ON p.playlist_id = pi.playlist_id
        JOIN videos v ON v.video_id = pi.video_id
        LEFT JOIN video_recovery r ON r.video_id = pi.video_id
        WHERE {" AND ".join(where)}
        GROUP BY pi.video_id, COALESCE(r.search_status, '')
        ORDER BY MIN(p.title) COLLATE NOCASE, MIN(pi.position), pi.video_id
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def playlist_placeholder_recovery_count(
    conn: sqlite3.Connection,
    force: bool = False,
    playlist_id: str = "",
) -> int:
    return len(playlist_placeholder_recovery_rows(conn, limit=0, force=force, playlist_id=playlist_id))


def placeholder_queue_subject_key(video_id: str) -> str:
    return f"placeholder:{(video_id or '').strip()}"


def enqueue_placeholder_recovery_targets(
    conn: sqlite3.Connection,
    playlist_id: str,
) -> dict[str, int]:
    rows = playlist_placeholder_recovery_rows(conn, force=False, playlist_id=playlist_id)
    inserted = 0
    existing = 0
    now = utc_now()
    # Recovery should not interrupt the scan that discovered it or the work
    # already queued ahead of it. Keep this batch at the current queue tail.
    tail_priority = int(
        conn.execute("SELECT COALESCE(MAX(priority), 0) + 1 AS priority FROM worker_queue").fetchone()["priority"]
        or 1
    )
    for row in rows:
        video_id = row["video_id"] or ""
        if not video_id:
            continue
        was_inserted = enqueue_placeholder_recovery_item(
            conn,
            video_id=video_id,
            playlist_id=playlist_id,
            current_title=row["title"] or video_id,
            playlist_count=int(row["playlist_count"] or 0),
            priority=tail_priority,
            updated_at=now,
        )
        if not was_inserted:
            existing += 1
        else:
            inserted += 1
    return {"inserted": inserted, "existing": existing}


def enqueue_placeholder_recovery_item(
    conn: sqlite3.Connection,
    *,
    video_id: str,
    playlist_id: str = "",
    current_title: str = "",
    source_key: str = "",
    playlist_count: int = 0,
    priority: int = 100,
    updated_at: str = "",
) -> bool:
    video_id = (video_id or "").strip()
    if not video_id:
        return False
    subject_key = placeholder_queue_subject_key(video_id)
    queued = conn.execute(
        "SELECT 1 FROM worker_queue WHERE subject_key = ?",
        (subject_key,),
    ).fetchone()
    now = updated_at or utc_now()
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, channel_id, playlist_id,
          channel_title, current_title, source_key, playlist_count, priority, manual, created_at, updated_at
        )
        VALUES (?, 'placeholder', 'recover', ?, '', ?, '', ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          playlist_id=COALESCE(NULLIF(excluded.playlist_id, ''), worker_queue.playlist_id),
          current_title=COALESCE(NULLIF(excluded.current_title, ''), worker_queue.current_title),
          source_key=COALESCE(NULLIF(excluded.source_key, ''), worker_queue.source_key),
          playlist_count=MAX(worker_queue.playlist_count, excluded.playlist_count),
          priority=MIN(worker_queue.priority, excluded.priority),
          updated_at=excluded.updated_at
        """,
        (
            subject_key,
            video_id,
            playlist_id or "",
            current_title or video_id,
            source_key or "",
            max(0, int(playlist_count or 0)),
            int(priority),
            now,
            now,
        ),
    )
    return not bool(queued)


def placeholder_worker_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    queue_id: int = 0,
) -> list[sqlite3.Row]:
    sql = """
        SELECT queue_id, video_id, playlist_id, current_title, source_key, priority
        FROM worker_queue
        WHERE worker_type = 'placeholder'
        ORDER BY priority, queue_id
    """
    params: list[Any] = []
    if queue_id:
        sql = sql.replace(
            "WHERE worker_type = 'placeholder'",
            "WHERE worker_type = 'placeholder' AND queue_id = ?",
        )
        params.append(queue_id)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def archive_capture_timestamp(url: str) -> str | None:
    match = re.search(r"/web/(\d{14})", url or "")
    if not match:
        return None
    parsed = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def save_video_recovery(
    conn: sqlite3.Connection,
    video_id: str,
    video: dict[str, Any] | None,
    search_status: str,
    search_error: str,
    thumbnail_url: str = "",
    thumbnail_path: str = "",
) -> None:
    now = utc_now()
    recovered_status = (video or {}).get("status") or ""
    if search_status == "not_found":
        recovered_status = "NOT_FOUND"
    archivarix_channel_id = str((video or {}).get("channelId") or "")
    channel_id = upsert_channel(
        conn,
        str((video or {}).get("channelExternalId") or ""),
        title=str((video or {}).get("channelTitle") or ""),
        url=str((video or {}).get("channelUrl") or ""),
        description=str((video or {}).get("channelDescription") or ""),
        aliases=str((video or {}).get("channelAliases") or ""),
        thumbnail_url=str((video or {}).get("channelThumbnailUrl") or ""),
        thumbnail_path=str((video or {}).get("channelThumbnailPath") or ""),
        archivarix_channel_id=archivarix_channel_id if not archivarix_channel_id.startswith("UC") else "",
        source="archivarix",
    )
    playability = is_playable_from_recovery_status(recovered_status) if recovered_status else None
    upsert_video(
        conn,
        video_id,
        title=str((video or {}).get("title") or ""),
        description=str((video or {}).get("description") or ""),
        channel_id=channel_id,
        duration_text=format_duration((video or {}).get("duration")),
        view_count=str((video or {}).get("viewCount") or ""),
        upload_date=str((video or {}).get("uploadDate") or ""),
        thumbnail_url=thumbnail_url,
        thumbnail_path=thumbnail_path,
        is_playable=playability,
        availability=video_availability_from_recovery_status(recovered_status),
        source="archivarix",
        checked_at=now,
        updated_at=now,
    )
    conn.execute(
        """
        INSERT INTO video_recovery(
          video_id, archivarix_status, archivarix_channel_id, archive_capture_at,
          media_available, searched_at, search_status, search_error, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
          archivarix_status=excluded.archivarix_status,
          archivarix_channel_id=excluded.archivarix_channel_id,
          archive_capture_at=COALESCE(excluded.archive_capture_at, video_recovery.archive_capture_at),
          media_available=COALESCE(excluded.media_available, video_recovery.media_available),
          searched_at=excluded.searched_at,
          search_status=excluded.search_status,
          search_error=excluded.search_error,
          updated_at=excluded.updated_at
        """,
        (
            video_id,
            recovered_status,
            archivarix_channel_id if not archivarix_channel_id.startswith("UC") else "",
            archive_capture_timestamp(str((video or {}).get("archiveUrl") or "")),
            1 if (video or {}).get("videoFileUrl") else None,
            now,
            search_status,
            search_error,
            now,
        ),
    )


def recover_archivarix_video(
    video_id: str,
    thumb_dir: Path,
    archivarix_opener: urllib.request.OpenerDirector,
    refresh_metadata: bool = False,
    no_api: bool = False,
    delay: float = 0,
    channel_cache: dict[str, dict[str, Any]] | None = None,
    stop_event: threading.Event | None = None,
    request_timeout: int = 20,
    stream_timeout: int = 25,
    thumbnail_timeout: int = 12,
    channel_thumbnail_timeout: int = 30,
) -> tuple[dict[str, Any] | None, str, str, str, str]:
    if stop_event and stop_event.is_set():
        return None, "", "", "stopped", "Stop requested"
    status = "not_found"
    error = ""
    video: dict[str, Any] | None = None
    thumbnail_url = ""
    thumbnail_path = cache_archivarix_thumbnail(
        video_id,
        "",
        thumb_dir,
        archivarix_opener,
        timeout=thumbnail_timeout,
        stop_event=stop_event,
    )
    if thumbnail_path and not refresh_metadata:
        status = "thumbnail_only"
    elif not no_api:
        try:
            if delay:
                if stop_event:
                    if stop_event.wait(delay):
                        return None, "", thumbnail_path, "stopped", "Stop requested"
                else:
                    time.sleep(delay)
            video = archivarix_lookup_video(
                video_id,
                archivarix_opener,
                channel_cache=channel_cache,
                stop_event=stop_event,
                request_timeout=request_timeout,
                stream_timeout=stream_timeout,
            )
        except ArchivarixQuotaExceeded as exc:
            status = "rate_limited"
            error = str(exc)
        except urllib.error.HTTPError as exc:
            quota_message = archivarix_http_error_message(exc)
            if quota_message:
                status = "rate_limited"
                error = quota_message
            else:
                status = "error"
                error = str(exc)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            status = "error"
            error = str(exc)
    if video:
        if stop_event and stop_event.is_set():
            return None, "", thumbnail_path, "stopped", "Stop requested"
        status = "found"
        thumbnail_url = video.get("thumbnailArchiveUrl") or video.get("thumbnailUrl") or ""
        thumbnail_path = thumbnail_path or cache_archivarix_thumbnail(
            video_id,
            thumbnail_url,
            thumb_dir,
            archivarix_opener,
            timeout=thumbnail_timeout,
            stop_event=stop_event,
        )
        channel_thumbnail_url = video.get("channelThumbnailUrl") or ""
        if channel_thumbnail_url:
            video["channelThumbnailPath"] = cache_channel_thumbnail(
                archivarix_opener,
                video_id,
                channel_thumbnail_url,
                thumb_dir,
                timeout=channel_thumbnail_timeout,
            )
    return video, thumbnail_url, thumbnail_path, status, error


def youtube_occurrence_sequence(
    conn: sqlite3.Connection,
    start: int,
    limit: int,
) -> list[str]:
    return [
        row["video_id"]
        for row in conn.execute(
            """
            SELECT video_id
            FROM history_events
            WHERE youtube_ordinal >= ?
              AND youtube_ordinal < ?
            ORDER BY youtube_ordinal
            """,
            (start, start + limit),
        )
    ]


def find_feed_overlap(fetched: list[str], existing: list[str]) -> int | None:
    max_offset = min(len(fetched), len(existing))
    for offset in range(max_offset + 1):
        candidate = fetched[offset:]
        if candidate and candidate == existing[: len(candidate)]:
            return offset
    return None


def save_youtube_history_events(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    start: int,
) -> tuple[int, int, str]:
    now = utc_now()
    observed_at = now
    inserted = 0
    existing = 0
    last_video_id = ""
    for index, row in enumerate(rows, start=start):
        video_id = row.get("video_id") or ""
        if not video_id:
            continue
        previous = conn.execute(
            "SELECT * FROM history_events WHERE youtube_ordinal = ?",
            (index,),
        ).fetchone()
        if previous and previous["video_id"] == video_id:
            existing += 1
        else:
            inserted += 1
        last_video_id = video_id
        channel_url = row.get("channel_url") or ""
        channel_id = upsert_channel(
            conn,
            row.get("channel_id") or youtube_channel_id_from_url(channel_url),
            title=row.get("channel") or "",
            url=channel_url,
            source="youtube_history",
            updated_at=now,
        )
        upsert_video(
            conn,
            video_id,
            title=row.get("title") or "",
            channel_id=channel_id,
            channel_title=row.get("channel") or "",
            channel_url=channel_url,
            watch_progress_percent=row.get("watch_progress_percent"),
            watch_resume_seconds=row.get("watch_resume_seconds"),
            source="youtube_history",
            updated_at=now,
        )
        if previous and previous["video_id"] != video_id:
            if previous["takeout_history_key"]:
                conn.execute(
                    """
                    UPDATE history_events
                    SET youtube_ordinal=NULL, source_type='takeout', match_type='takeout_only', updated_at=?
                    WHERE event_id=?
                    """,
                    (now, previous["event_id"]),
                )
            else:
                conn.execute("DELETE FROM history_events WHERE event_id = ?", (previous["event_id"],))
        conn.execute(
            """
            INSERT INTO history_events(
              event_id, video_id, watched_at, watch_date, time_precision,
              source_type, match_type, youtube_ordinal,
              watch_progress_percent, watch_resume_seconds,
              observed_at, imported_at, updated_at
            )
            VALUES (?, ?, NULL, ?, ?, 'youtube', 'youtube_only', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
              video_id=excluded.video_id,
              watch_date=excluded.watch_date,
              time_precision=excluded.time_precision,
              youtube_ordinal=excluded.youtube_ordinal,
              watch_progress_percent=excluded.watch_progress_percent,
              watch_resume_seconds=excluded.watch_resume_seconds,
              observed_at=excluded.observed_at,
              updated_at=excluded.updated_at
            """,
            (
                f"youtube:{index}",
                video_id,
                row.get("watch_date") or "",
                "date_only" if row.get("watch_date") else "unknown",
                index,
                bounded_int(row.get("watch_progress_percent")),
                max(0, int(row.get("watch_resume_seconds") or 0)),
                observed_at,
                now,
                now,
            ),
        )
    return inserted, existing, last_video_id


def history_row_hash(row: dict[str, str]) -> str:
    payload = "\x1f".join(
        (row.get("video_id", ""), takeout_watch_datetime(row.get("watched_at", "")))
    )
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()


def rebuild_history_reconciliation(
    conn: sqlite3.Connection,
    timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> dict[str, int]:
    now = utc_now()
    youtube_rows = conn.execute(
        """
        SELECT *
        FROM history_events
        WHERE youtube_ordinal IS NOT NULL AND takeout_history_key IS NULL
        ORDER BY youtube_ordinal
        """
    ).fetchall()
    takeout_rows = conn.execute(
        """
        SELECT *
        FROM history_events
        WHERE takeout_history_key IS NOT NULL AND youtube_ordinal IS NULL
        ORDER BY watched_at DESC, takeout_history_key, takeout_row_key
        """
    ).fetchall()

    takeout_by_video_date: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in takeout_rows:
        match_date = local_date_for_utc_instant(row["watched_at"], timezone_name)
        takeout_by_video_date.setdefault((row["video_id"], match_date), []).append(row)

    matched_takeout: set[str] = set()
    matched = 0
    for youtube in youtube_rows:
        key = (youtube["video_id"], youtube["watch_date"])
        if not key[0] or not key[1]:
            continue
        candidates = takeout_by_video_date.get(key, [])
        for takeout in candidates:
            takeout_key = takeout["event_id"]
            if takeout_key in matched_takeout:
                continue
            matched_takeout.add(takeout_key)
            conn.execute(
                """
                UPDATE history_events
                SET source_type='takeout_youtube', match_type='video_id_date',
                    youtube_ordinal=?, watch_progress_percent=?, watch_resume_seconds=?,
                    observed_at=?, updated_at=?
                WHERE event_id=?
                """,
                (
                    youtube["youtube_ordinal"],
                    youtube["watch_progress_percent"],
                    youtube["watch_resume_seconds"],
                    youtube["observed_at"],
                    now,
                    takeout["event_id"],
                ),
            )
            conn.execute("DELETE FROM history_events WHERE event_id = ?", (youtube["event_id"],))
            matched += 1
            break
    inserted = conn.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]
    return {
        "rows": inserted,
        "matched": matched,
        "takeout": len(takeout_rows),
        "youtube": len(youtube_rows),
    }


def save_playlist_scan(
    conn: sqlite3.Connection,
    playlist_id: str,
    videos: list[dict[str, Any]],
    status: str,
    error: str,
    playlist_metadata: dict[str, Any] | None = None,
) -> tuple[int, int]:
    deduped_videos: list[dict[str, Any]] = []
    seen_video_ids: set[str] = set()
    next_position = 1
    for video in videos:
        video_id = video.get("video_id") or ""
        if video_id:
            if video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
        cleaned = dict(video)
        cleaned["position"] = next_position
        deduped_videos.append(cleaned)
        next_position += 1
    videos = deduped_videos
    unavailable_count = sum(1 for video in videos if not video["is_playable"])
    now = utc_now()
    retained = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM playlist_items
            WHERE playlist_id = ?
              AND (membership_state = 'retained_unavailable' OR source_quality = 'takeout')
            ORDER BY position
            """,
            (playlist_id,),
        )
    ]
    conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
    for video in videos:
        channel_id = upsert_channel(
            conn,
            video.get("channel_id") or "",
            title=video.get("channel") or "",
            source="playlist",
            updated_at=now,
        )
        video_id = (video.get("video_id") or "").strip()
        if video_id:
            upsert_video(
                conn,
                video_id,
                title=video.get("title") or "",
                channel_id=channel_id,
                channel_title=video.get("channel") or "",
                duration_text=video.get("duration_text") or "",
                is_playable=video.get("is_playable"),
                availability=video.get("availability") or "",
                source="playlist",
                checked_at=now,
                updated_at=now,
            )
        conn.execute(
            """
            INSERT INTO playlist_items(
              playlist_id, position, video_id, membership_state, unavailable_kind,
              source_quality, match_type, match_confidence, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'youtube', ?, ?, NULL, ?)
            """,
            (
                playlist_id,
                video["position"],
                video_id or None,
                "current" if video_id else "unresolved_unavailable",
                (video.get("availability") or "unavailable") if not video_id else "",
                "" if video_id else "ambiguous_hidden_slot",
                "video_id" if video_id else "hidden_slot_only",
                now,
            ),
        )
    current_ids = {str(video.get("video_id") or "") for video in videos if video.get("video_id")}
    retained_position = (len(videos) + 1) * 1000
    for item in retained:
        if item.get("video_id") in current_ids:
            continue
        conn.execute(
            """
            INSERT INTO playlist_items(
              playlist_id, position, video_id, membership_state, unavailable_kind,
              source_quality, match_type, match_confidence, added_at, updated_at
            ) VALUES (?, ?, ?, 'retained_unavailable', ?, ?, ?, ?, ?, ?)
            """,
            (
                playlist_id,
                retained_position,
                item.get("video_id"),
                item.get("unavailable_kind") or "unavailable",
                item.get("source_quality") or "takeout",
                item.get("match_type") or "ambiguous_hidden_candidate",
                item.get("match_confidence") or "takeout_missing",
                item.get("added_at"),
                now,
            ),
        )
        retained_position += 1
    conn.execute(
        """
        INSERT INTO playlist_scans(
          playlist_id, scanned_at, video_count, unavailable_count, scan_status, scan_error
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(playlist_id) DO UPDATE SET
          scanned_at=excluded.scanned_at,
          video_count=excluded.video_count,
          unavailable_count=excluded.unavailable_count,
          scan_status=excluded.scan_status,
          scan_error=excluded.scan_error
        """,
        (playlist_id, now, len(videos), unavailable_count, status, error),
    )
    if playlist_metadata:
        metadata = {
            key: str(playlist_metadata.get(key) or "").strip()
            for key in (
                "title",
                "description",
                "owner_channel_id",
                "owner_thumbnail_url",
                "owner_thumbnail_path",
                "visibility",
                "thumbnail_url",
                "thumbnail_path",
            )
        }
        if metadata["owner_channel_id"]:
            metadata["owner_channel_id"] = upsert_channel(
                conn,
                metadata["owner_channel_id"],
                title=str(playlist_metadata.get("owner") or "").strip(),
                thumbnail_url=metadata["owner_thumbnail_url"],
                thumbnail_path=metadata["owner_thumbnail_path"],
                source="playlist_owner",
                updated_at=now,
            )
        metadata["video_count"] = max(0, int(playlist_metadata.get("video_count") or 0))
        assert_playlist_owner_visibility(metadata)
        conn.execute(
            """
            INSERT INTO playlists(
              playlist_id, title, description, owner_channel_id, visibility, video_count,
              thumbnail_url, thumbnail_path, fetch_status, fetch_error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(playlist_id) DO UPDATE SET
              title=COALESCE(NULLIF(excluded.title, ''), playlists.title),
              description=COALESCE(NULLIF(excluded.description, ''), playlists.description),
              owner_channel_id=CASE
                WHEN NULLIF(excluded.visibility, '') IS NOT NULL THEN NULL
                ELSE COALESCE(excluded.owner_channel_id, playlists.owner_channel_id)
              END,
              visibility=CASE
                WHEN excluded.owner_channel_id IS NOT NULL THEN ''
                ELSE COALESCE(NULLIF(excluded.visibility, ''), playlists.visibility)
              END,
              video_count=CASE
                WHEN excluded.video_count > 0 THEN excluded.video_count
                ELSE playlists.video_count
              END,
              thumbnail_url=COALESCE(NULLIF(excluded.thumbnail_url, ''), playlists.thumbnail_url),
              thumbnail_path=COALESCE(NULLIF(excluded.thumbnail_path, ''), playlists.thumbnail_path),
              fetch_status=excluded.fetch_status,
              fetch_error=excluded.fetch_error,
              updated_at=excluded.updated_at
            """,
            (
                playlist_id,
                metadata["title"],
                metadata["description"],
                metadata["owner_channel_id"] or None,
                metadata["visibility"],
                metadata["video_count"],
                metadata["thumbnail_url"],
                metadata["thumbnail_path"],
                status,
                error,
                now,
            ),
        )
    return len(videos), unavailable_count


def save_liked_video_reactions(
    conn: sqlite3.Connection,
    videos: list[dict[str, Any]],
    *,
    replace: bool = True,
) -> tuple[int, int]:
    deduped: list[dict[str, Any]] = []
    seen_video_ids: set[str] = set()
    unavailable_count = 0
    for video in videos:
        video_id = str(video.get("video_id") or "").strip()
        if not video_id or video_id in seen_video_ids:
            if not video_id:
                unavailable_count += 1
            continue
        seen_video_ids.add(video_id)
        deduped.append(video)
        if not video.get("is_playable", True):
            unavailable_count += 1

    now = utc_now()
    if replace:
        conn.execute(
            "UPDATE videos SET reaction = '', updated_at = ? WHERE reaction = 'L'",
            (now,),
        )
    for video in deduped:
        video_id = str(video.get("video_id") or "").strip()
        channel_id = upsert_channel(
            conn,
            str(video.get("channel_id") or ""),
            title=str(video.get("channel") or ""),
            source="playlist",
            updated_at=now,
        )
        upsert_video(
            conn,
            video_id,
            title=str(video.get("title") or ""),
            channel_id=channel_id,
            channel_title=str(video.get("channel") or ""),
            duration_text=str(video.get("duration_text") or ""),
            is_playable=video.get("is_playable"),
            availability=str(video.get("availability") or ""),
            source="playlist",
            checked_at=now,
            updated_at=now,
        )
        conn.execute(
            "UPDATE videos SET reaction = 'L', updated_at = ? WHERE video_id = ?",
            (now, video_id),
        )
    return len(deduped), unavailable_count


def save_playlist_scan_error(
    conn: sqlite3.Connection,
    playlist_id: str,
    error: str,
) -> tuple[int, int]:
    now = utc_now()
    previous = conn.execute(
        """
        SELECT video_count, unavailable_count
        FROM playlist_scans
        WHERE playlist_id = ?
        """,
        (playlist_id,),
    ).fetchone()
    video_count = int(previous["video_count"] or 0) if previous else 0
    unavailable_count = int(previous["unavailable_count"] or 0) if previous else 0
    conn.execute(
        """
        INSERT INTO playlist_scans(
          playlist_id, scanned_at, video_count, unavailable_count, scan_status, scan_error
        )
        VALUES (?, ?, ?, ?, 'error', ?)
        ON CONFLICT(playlist_id) DO UPDATE SET
          scanned_at=excluded.scanned_at,
          video_count=playlist_scans.video_count,
          unavailable_count=playlist_scans.unavailable_count,
          scan_status=excluded.scan_status,
          scan_error=excluded.scan_error
        """,
        (playlist_id, now, video_count, unavailable_count, error),
    )
    return video_count, unavailable_count


def rebuild_playlist_reconciliation(
    conn: sqlite3.Connection,
    playlist_id: str | None = None,
) -> dict[str, int]:
    where = "WHERE playlist_id = ?" if playlist_id else ""
    params: tuple[Any, ...] = (playlist_id,) if playlist_id else ()
    rows = conn.execute(
        f"SELECT membership_state, COUNT(*) AS count FROM playlist_items {where} GROUP BY membership_state",
        params,
    ).fetchall()
    counts = {row["membership_state"]: int(row["count"] or 0) for row in rows}
    playlist_count = conn.execute(
        f"SELECT COUNT(DISTINCT playlist_id) FROM playlist_items {where}",
        params,
    ).fetchone()[0]
    return {
        "playlists": int(playlist_count or 0),
        "rows": sum(counts.values()),
        "inferred": 0,
        "ambiguous": counts.get("retained_unavailable", 0) + counts.get("unresolved_unavailable", 0),
    }


def playlist_scan_candidate_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
    stale_days: int = 7,
) -> list[sqlite3.Row]:
    stale_before = utc_days_ago(stale_days)
    where = ["p.playlist_id <> ''"]
    params: list[Any] = []
    if not force:
        where.append(
            """
            (
              ps.playlist_id IS NULL
              OR ps.scan_status <> 'ok'
              OR (p.video_count > 0 AND p.video_count <> COALESCE(ps.video_count, -1))
              OR (ps.scanned_at IS NOT NULL AND ps.scanned_at < ?)
            )
            """
        )
        params.append(stale_before)
    sql = f"""
        SELECT p.playlist_id,
               p.title,
               p.video_count AS playlist_video_count,
               COALESCE(ps.scanned_at, '') AS scanned_at,
               COALESCE(ps.scan_status, '') AS scan_status,
               COALESCE(ps.video_count, 0) AS video_count,
               COALESCE(ps.unavailable_count, 0) AS unavailable_count
        FROM playlists p
        LEFT JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
        WHERE {" AND ".join(where)}
        ORDER BY
          CASE WHEN ps.playlist_id IS NULL THEN 0 ELSE 1 END,
          COALESCE(ps.scanned_at, ''),
          p.title COLLATE NOCASE
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def worker_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
) -> list[sqlite3.Row]:
    sql = """
        SELECT w.queue_id,
               w.subject_key,
               w.worker_type,
               w.task_type,
               w.video_id,
               w.channel_id,
               w.playlist_id,
               w.channel_title,
               w.current_title,
               w.source_key,
               w.playlist_count,
               w.priority,
               w.manual,
               w.created_at,
               w.updated_at,
               p.title AS playlist_title,
               p.video_count AS playlist_video_count,
               COALESCE(ps.scan_status, '') AS scan_status,
               COALESCE(ps.video_count, 0) AS video_count,
               COALESCE(ps.unavailable_count, 0) AS unavailable_count
        FROM worker_queue w
        LEFT JOIN playlists p ON p.playlist_id = w.playlist_id
        LEFT JOIN playlist_scans ps ON ps.playlist_id = w.playlist_id
        ORDER BY w.priority, w.queue_id
    """
    params: list[Any] = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def worker_queue_rows_by_id(
    conn: sqlite3.Connection,
    queue_ids: Sequence[int],
) -> list[sqlite3.Row]:
    ids = sorted({int(queue_id) for queue_id in queue_ids if int(queue_id) > 0})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        f"""
        SELECT w.queue_id,
               w.subject_key,
               w.worker_type,
               w.task_type,
               w.video_id,
               w.channel_id,
               w.playlist_id,
               w.channel_title,
               w.current_title,
               w.source_key,
               w.playlist_count,
               w.priority,
               w.manual,
               w.created_at,
               w.updated_at,
               p.title AS playlist_title,
               p.video_count AS playlist_video_count,
               COALESCE(ps.scan_status, '') AS scan_status,
               COALESCE(ps.video_count, 0) AS video_count,
               COALESCE(ps.unavailable_count, 0) AS unavailable_count
        FROM worker_queue w
        LEFT JOIN playlists p ON p.playlist_id = w.playlist_id
        LEFT JOIN playlist_scans ps ON ps.playlist_id = w.playlist_id
        WHERE w.queue_id IN ({placeholders})
        ORDER BY w.priority, w.queue_id
        """,
        ids,
    ).fetchall()


def worker_queue_event_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(event_id), 0) AS event_id FROM worker_queue_events").fetchone()
    return int(row["event_id"] or 0)


def worker_queue_events_after(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    limit: int = 500,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT event_id, queue_id, operation, created_at
        FROM worker_queue_events
        WHERE event_id > ?
        ORDER BY event_id
        LIMIT ?
        """,
        (max(0, int(event_id)), max(1, min(5000, int(limit)))),
    ).fetchall()


_WORKER_LOG_TABLES = {
    "metadataLogs": "metadata_worker_log",
    "playlistScanLogs": "playlist_scan_worker_log",
    "liveHistoryLogs": "live_history_worker_log",
    "placeholderRecoveryLogs": "placeholder_recovery_worker_log",
}


def worker_log_cursors(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        name: int(
            conn.execute(f"SELECT COALESCE(MAX(id), 0) AS id FROM {table}").fetchone()["id"] or 0
        )
        for name, table in _WORKER_LOG_TABLES.items()
    }


def worker_log_snapshot(conn: sqlite3.Connection, *, limit: int = 80) -> dict[str, list[sqlite3.Row]]:
    row_limit = max(1, min(500, int(limit)))
    return {
        name: conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (row_limit,)).fetchall()
        for name, table in _WORKER_LOG_TABLES.items()
    }


def worker_logs_after(
    conn: sqlite3.Connection,
    cursors: dict[str, int],
    *,
    limit: int = 500,
) -> dict[str, list[sqlite3.Row]]:
    row_limit = max(1, min(5000, int(limit)))
    return {
        name: conn.execute(
            f"SELECT * FROM {table} WHERE id > ? ORDER BY id LIMIT ?",
            (max(0, int(cursors.get(name, 0))), row_limit),
        ).fetchall()
        for name, table in _WORKER_LOG_TABLES.items()
    }


def worker_queue_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM worker_queue").fetchone()
    return int(row["count"] or 0)


def worker_queue_type_count(conn: sqlite3.Connection, worker_type: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM worker_queue WHERE worker_type = ?",
        ((worker_type or "").strip(),),
    ).fetchone()
    return int(row["count"] or 0)


def external_service_block(conn: sqlite3.Connection, service: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM external_service_blocks WHERE service = ?",
        (service,),
    ).fetchone()
    if not row:
        return {
            "service": service,
            "blocked": False,
            "reason_code": "",
            "message": "",
            "blocked_at": "",
            "retry_after": "",
            "run_id": "",
            "queue_id": 0,
            "retry_eligible": False,
            "manual_retry_required": False,
        }
    return {
        **dict(row),
        "blocked": True,
        "retry_eligible": True,
        "manual_retry_required": not bool(row["retry_after"]),
    }


def set_external_service_block(
    conn: sqlite3.Connection,
    service: str,
    reason_code: str,
    message: str,
    *,
    run_id: str = "",
    queue_id: int = 0,
    retry_after: str = "",
) -> dict[str, Any]:
    conn.execute(
        """
        INSERT INTO external_service_blocks(
          service, reason_code, message, blocked_at, retry_after, run_id, queue_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(service) DO UPDATE SET
          reason_code = excluded.reason_code,
          message = excluded.message,
          blocked_at = excluded.blocked_at,
          retry_after = excluded.retry_after,
          run_id = excluded.run_id,
          queue_id = excluded.queue_id
        """,
        (service, reason_code, message, utc_now(), retry_after, run_id, queue_id),
    )
    return external_service_block(conn, service)


def clear_external_service_block(conn: sqlite3.Connection, service: str) -> bool:
    cursor = conn.execute("DELETE FROM external_service_blocks WHERE service = ?", (service,))
    return cursor.rowcount > 0


def clear_worker_queue_type(conn: sqlite3.Connection, worker_type: str) -> int:
    worker_type = (worker_type or "").strip()
    count = worker_queue_type_count(conn, worker_type)
    conn.execute("DELETE FROM worker_queue WHERE worker_type = ?", (worker_type,))
    return count


def clear_worker_queue(conn: sqlite3.Connection) -> int:
    count = worker_queue_count(conn)
    conn.execute("DELETE FROM worker_queue")
    return count


def remove_worker_queue_entry(conn: sqlite3.Connection, queue_id: int) -> bool:
    cursor = conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (queue_id,))
    return cursor.rowcount > 0


def enqueue_history_task(conn: sqlite3.Connection, mode: str, *, priority: int = 100, manual: bool = True) -> str:
    mode = (mode or "recent").strip() or "recent"
    now = utc_now()
    subject_key = f"history:{mode}"
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, channel_id, playlist_id,
          channel_title, current_title, source_key, playlist_count, priority, manual, created_at, updated_at
        )
        VALUES (?, 'history', ?, '', '', '', '', ?, '', 0, ?, ?, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          worker_type='history',
          task_type=excluded.task_type,
          current_title=excluded.current_title,
          priority=MIN(worker_queue.priority, excluded.priority),
          manual=MAX(worker_queue.manual, excluded.manual),
          updated_at=excluded.updated_at
        """,
        (
            subject_key,
            mode,
            "Verify history" if mode == "verify" else "Fetch history",
            int(priority),
            1 if manual else 0,
            now,
            now,
        ),
    )
    return subject_key


def enqueue_playlist_scan_item(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    title: str = "",
    source_key: str = "",
    priority: int = 100,
    manual: bool = False,
) -> str:
    now = utc_now()
    playlist_id = (playlist_id or "").strip()
    if not playlist_id:
        raise ValueError("Playlist queue item needs a playlist ID")
    if playlist_id == LIKED_VIDEOS_PLAYLIST_ID:
        title = "Liked videos"
    if not title:
        row = conn.execute(
            "SELECT title FROM playlists WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        if row:
            title = title or row["title"] or ""
    subject_key = playlist_queue_subject_key(playlist_id)
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, channel_id, playlist_id,
          channel_title, current_title, source_key, playlist_count, priority, manual, created_at, updated_at
        )
        VALUES (?, 'playlist', 'scan', '', '', ?, '', ?, ?, 0, ?, ?, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          worker_type='playlist',
          task_type='scan',
          playlist_id=excluded.playlist_id,
          current_title=COALESCE(NULLIF(excluded.current_title, ''), worker_queue.current_title),
          source_key=COALESCE(NULLIF(excluded.source_key, ''), worker_queue.source_key),
          priority=MIN(worker_queue.priority, excluded.priority),
          manual=MAX(worker_queue.manual, excluded.manual),
          updated_at=excluded.updated_at
        """,
        (
            subject_key,
            playlist_id,
            title or playlist_id,
            source_key or (playlist_id if manual else ""),
            int(priority),
            1 if manual else 0,
            now,
            now,
        ),
    )
    return subject_key


def playlist_scan_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
    stale_days: int = 7,
) -> list[sqlite3.Row]:
    del force, stale_days
    sql = """
        SELECT w.queue_id,
               w.subject_key,
               w.playlist_id,
               COALESCE(NULLIF(w.current_title, ''), p.title, w.playlist_id) AS title,
               p.video_count AS playlist_video_count,
               COALESCE(ps.scanned_at, '') AS scanned_at,
               COALESCE(ps.scan_status, '') AS scan_status,
               COALESCE(ps.video_count, 0) AS video_count,
               COALESCE(ps.unavailable_count, 0) AS unavailable_count,
               w.priority,
               w.manual,
               w.created_at,
               w.updated_at
        FROM worker_queue w
        LEFT JOIN playlists p ON p.playlist_id = w.playlist_id
        LEFT JOIN playlist_scans ps ON ps.playlist_id = w.playlist_id
        WHERE w.worker_type = 'playlist'
        ORDER BY w.priority, w.queue_id
    """
    params: list[Any] = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def playlist_scan_queue_count(conn: sqlite3.Connection) -> int:
    return worker_queue_type_count(conn, "playlist")


def clear_playlist_scan_queue(conn: sqlite3.Connection) -> int:
    return clear_worker_queue_type(conn, "playlist")


def rebuild_playlist_scan_queue(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    stale_days: int = 7,
) -> dict[str, int]:
    rows = playlist_scan_candidate_rows(conn, force=force, stale_days=stale_days)
    cleared = clear_playlist_scan_queue(conn)
    enqueue_playlist_scan_item(
        conn,
        LIKED_VIDEOS_PLAYLIST_ID,
        title="Liked videos",
        priority=0,
        manual=False,
    )
    inserted = 1
    for index, row in enumerate(rows, start=1):
        if row["playlist_id"] == LIKED_VIDEOS_PLAYLIST_ID:
            continue
        enqueue_playlist_scan_item(
            conn,
            row["playlist_id"] or "",
            title=row["title"] or "",
            priority=index,
            manual=False,
        )
        inserted += 1
    return {"cleared": cleared, "inserted": inserted, "queued": playlist_scan_queue_count(conn)}


def metadata_queue_candidate_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
    stale_days: int = 30,
) -> list[sqlite3.Row]:
    stale_before = utc_days_ago(stale_days)
    where = ""
    params: list[Any] = []
    if not force:
        where = """
        WHERE fetch_status = 'error'
           OR fetched_at IS NULL
           OR fetched_at < ?
        """
        params.append(stale_before)
    sql = f"""
        WITH candidates AS (
          SELECT ch.channel_id AS video_id,
                 ch.channel_id,
                 COALESCE(NULLIF(ch.title, ''), ch.channel_id) AS channel_title,
                 0 AS playlist_count,
                 '' AS current_title,
                 'channel' AS metadata_source,
                 0 AS priority,
                 ch.fetch_status,
                 ch.fetched_at,
                 ch.title,
                 ch.thumbnail_path,
                 '' AS latest_history_at
          FROM channels ch
          UNION ALL
          SELECT v.video_id,
                 COALESCE(v.channel_id, '') AS channel_id,
                 COALESCE(ch.title, '') AS channel_title,
                 COUNT(DISTINCT pi.playlist_id) AS playlist_count,
                 COALESCE(NULLIF(v.title, ''), v.video_id) AS current_title,
                 CASE WHEN COUNT(pi.playlist_id) > 0 THEN 'playlist' ELSE 'history' END AS metadata_source,
                 CASE WHEN COUNT(pi.playlist_id) > 0 THEN 2 ELSE 3 END AS priority,
                 v.fetch_status,
                 v.fetched_at,
                 v.title,
                 v.thumbnail_path,
                 COALESCE(h.latest_history_at, '') AS latest_history_at
          FROM videos v
          LEFT JOIN channels ch ON ch.channel_id = v.channel_id
          LEFT JOIN playlist_items pi ON pi.video_id = v.video_id
          LEFT JOIN (
            SELECT video_id, MAX(COALESCE(watched_at, watch_date, '')) AS latest_history_at
            FROM history_events
            GROUP BY video_id
          ) h ON h.video_id = v.video_id
          GROUP BY v.video_id
        )
        SELECT video_id, channel_id, channel_title, playlist_count, current_title,
               metadata_source, priority
        FROM candidates
        {where}
        ORDER BY priority,
                 CASE WHEN metadata_source = 'history' THEN latest_history_at ELSE '' END DESC,
                 CASE WHEN metadata_source = 'history' THEN '' ELSE COALESCE(fetched_at, '') END,
                 current_title COLLATE NOCASE,
                 video_id
    """
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, max(0, offset)])
    return conn.execute(sql, params).fetchall()


def metadata_queue_candidate_count(
    conn: sqlite3.Connection,
    force: bool = False,
    stale_days: int = 30,
) -> int:
    return len(metadata_queue_candidate_rows(conn, force=force, stale_days=stale_days))


def metadata_queue_subject_key(video_id: str, channel_id: str, metadata_source: str) -> str:
    metadata_source = (metadata_source or "").strip() or "history"
    channel_id = (channel_id or "").strip()
    video_id = (video_id or "").strip()
    if metadata_source == "channel":
        return f"metadata:channel:{channel_id or video_id}"
    return f"metadata:video:{video_id}"


def playlist_queue_subject_key(playlist_id: str) -> str:
    return f"playlist:scan:{(playlist_id or '').strip()}"


def metadata_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
    stale_days: int = 30,
    queue_id: int = 0,
) -> list[sqlite3.Row]:
    del force, stale_days
    sql = """
        SELECT queue_id,
               subject_key,
               video_id,
               channel_id,
               channel_title,
               playlist_count,
               current_title,
               task_type AS metadata_source,
               source_key,
               priority,
               manual,
               created_at,
               updated_at
        FROM worker_queue
        WHERE worker_type = 'metadata'
        ORDER BY priority, queue_id
    """
    params: list[Any] = []
    if queue_id:
        sql = sql.replace(
            "WHERE worker_type = 'metadata'",
            "WHERE worker_type = 'metadata' AND queue_id = ?",
        )
        params.append(queue_id)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def metadata_queue_count(
    conn: sqlite3.Connection,
    force: bool = False,
    stale_days: int = 30,
) -> int:
    del force, stale_days
    return worker_queue_type_count(conn, "metadata")


def clear_metadata_queue(conn: sqlite3.Connection) -> int:
    return clear_worker_queue_type(conn, "metadata")


def remove_metadata_queue_entry(conn: sqlite3.Connection, queue_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM worker_queue WHERE queue_id = ? AND worker_type = 'metadata'",
        (queue_id,),
    )
    return cursor.rowcount > 0


def normalize_metadata_queue_targets(conn: sqlite3.Connection) -> None:
    if "worker_queue" not in {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }:
        return
    rows = conn.execute(
        """
        SELECT queue_id, video_id, channel_id, channel_title, metadata_source, priority, manual
        FROM (
          SELECT queue_id, video_id, channel_id, channel_title, task_type AS metadata_source, priority, manual
          FROM worker_queue
          WHERE worker_type = 'metadata'
        )
        WHERE metadata_source = 'channel'
          AND (channel_id LIKE 'http://%' OR channel_id LIKE 'https://%')
        """
    ).fetchall()
    for row in rows:
        channel_ref = youtube_channel_ref_from_url(row["channel_id"])
        if not channel_ref:
            continue
        conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (row["queue_id"],))
        enqueue_metadata_item(
            conn,
            video_id=channel_ref,
            channel_id=channel_ref,
            channel_title=row["channel_title"] or row["channel_id"],
            metadata_source="channel",
            priority=int(row["priority"] or 0),
            manual=bool(row["manual"]),
        )


def enqueue_metadata_item(
    conn: sqlite3.Connection,
    *,
    video_id: str = "",
    channel_id: str = "",
    channel_title: str = "",
    current_title: str = "",
    metadata_source: str = "history",
    source_key: str = "",
    playlist_count: int = 0,
    priority: int = 100,
    manual: bool = False,
) -> str:
    now = utc_now()
    video_id = (video_id or "").strip()
    channel_id = (channel_id or "").strip()
    metadata_source = (metadata_source or "").strip() or "history"
    subject_key = metadata_queue_subject_key(video_id, channel_id, metadata_source)
    if not video_id and not channel_id:
        raise ValueError("Metadata queue item needs a video ID or channel ID")
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, channel_id, playlist_id,
          channel_title, current_title, source_key, playlist_count, priority, manual, created_at, updated_at
        )
        VALUES (?, 'metadata', ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          worker_type='metadata',
          task_type=excluded.task_type,
          video_id=excluded.video_id,
          channel_id=excluded.channel_id,
          channel_title=COALESCE(NULLIF(excluded.channel_title, ''), worker_queue.channel_title),
          current_title=COALESCE(NULLIF(excluded.current_title, ''), worker_queue.current_title),
          source_key=COALESCE(NULLIF(excluded.source_key, ''), worker_queue.source_key),
          playlist_count=MAX(worker_queue.playlist_count, excluded.playlist_count),
          priority=MIN(worker_queue.priority, excluded.priority),
          manual=MAX(worker_queue.manual, excluded.manual),
          updated_at=excluded.updated_at
        """,
        (
            subject_key,
            metadata_source,
            video_id,
            channel_id,
            channel_title or "",
            current_title or "",
            source_key or "",
            max(0, int(playlist_count or 0)),
            int(priority),
            1 if manual else 0,
            now,
            now,
        ),
    )
    return subject_key


def rebuild_metadata_queue(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    stale_days: int = 30,
) -> dict[str, int]:
    rows = metadata_queue_candidate_rows(conn, force=force, stale_days=stale_days)
    cleared = clear_metadata_queue(conn)
    inserted = 0
    for index, row in enumerate(rows, start=1):
        source = row["metadata_source"] or "history"
        priority = int(row["priority"] or 0) * 1_000_000 + index
        enqueue_metadata_item(
            conn,
            video_id=row["video_id"] or "",
            channel_id=row["channel_id"] or "",
            channel_title=row["channel_title"] or "",
            current_title=row["current_title"] or "",
            metadata_source=source,
            playlist_count=int(row["playlist_count"] or 0),
            priority=priority,
            manual=False,
        )
        inserted += 1
    return {"cleared": cleared, "inserted": inserted}


def enqueue_playlist_metadata_targets(conn: sqlite3.Connection, playlist_id: str) -> dict[str, str]:
    playlist_id = (playlist_id or "").strip()
    if not playlist_id:
        raise ValueError("Enter a YouTube playlist URL or playlist ID.")
    rows = conn.execute(
        """
        SELECT pi.video_id,
               MAX(v.title) AS title,
               MIN(pi.position) AS position
        FROM playlist_items pi
        JOIN videos v ON v.video_id = pi.video_id
        WHERE pi.playlist_id = ?
        GROUP BY pi.video_id
        ORDER BY position
        """,
        (playlist_id,),
    ).fetchall()
    if not rows:
        raise ValueError(f"No known videos found for playlist {playlist_id}. Scan the playlist first.")
    for index, row in enumerate(rows):
        enqueue_metadata_item(
            conn,
            video_id=row["video_id"] or "",
            current_title=row["title"] or "",
            metadata_source="playlist",
            source_key=playlist_id,
            priority=index,
            manual=True,
        )
    return {
        "subject_key": f"playlist:{playlist_id}",
        "video_id": "",
        "channel_id": "",
        "metadata_source": "playlist",
        "playlist_id": playlist_id,
        "queued_count": str(len(rows)),
    }


def local_queue_target_from_url(target: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(target)
    if not parsed.scheme and not parsed.netloc:
        return "", ""
    host = (parsed.hostname or "").lower()
    if host.endswith("youtube.com") or host == "youtu.be":
        return "", ""

    fragment_params = urllib.parse.parse_qs(parsed.fragment)
    for key, kind in (("playlist", "playlist"), ("video", "video"), ("channel", "channel")):
        value = (fragment_params.get(key) or [""])[0]
        if value:
            return kind, urllib.parse.unquote(value).strip()

    query_params = urllib.parse.parse_qs(parsed.query)
    if (query_params.get("list") or [""])[0]:
        return "playlist", (query_params.get("list") or [""])[0].strip()
    if (query_params.get("v") or [""])[0]:
        return "video", (query_params.get("v") or [""])[0].strip()

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) >= 2 and parts[0] in {"playlist", "video", "channel"}:
        return parts[0], urllib.parse.unquote(parts[1]).strip()
    return "", ""


def enqueue_worker_queue_target(conn: sqlite3.Connection, target: str) -> dict[str, str]:
    target = (target or "").strip()
    if not target:
        raise ValueError("Enter a YouTube URL, local URL, video ID, channel ID, @handle, or playlist ID.")

    parsed = urllib.parse.urlparse(target)
    host = (parsed.hostname or "").lower()
    is_youtube_url = parsed.scheme in {"http", "https"} and (
        host.endswith("youtube.com") or host == "youtu.be"
    )

    if is_youtube_url:
        video_id = extract_video_id(target)
        playlist_id = extract_playlist_id(target) or ""
        channel_id = "" if video_id else youtube_channel_ref_from_url(target)
        if playlist_id and not video_id:
            subject_key = enqueue_playlist_scan_item(
                conn,
                playlist_id,
                source_key=target,
                priority=0,
                manual=True,
            )
            return {
                "subject_key": subject_key,
                "worker_type": "playlist",
                "playlist_id": playlist_id,
                "source": "youtube",
            }
        if channel_id:
            subject_key = enqueue_metadata_item(
                conn,
                video_id=channel_id,
                channel_id=channel_id,
                channel_title=target,
                metadata_source="channel",
                source_key=target,
                priority=0,
                manual=True,
            )
            return {
                "subject_key": subject_key,
                "worker_type": "metadata",
                "channel_id": channel_id,
                "metadata_source": "channel",
                "source": "youtube",
            }
        if video_id:
            subject_key = enqueue_metadata_item(
                conn,
                video_id=video_id,
                current_title=target,
                metadata_source="provided",
                source_key=target,
                priority=0,
                manual=True,
            )
            return {
                "subject_key": subject_key,
                "worker_type": "metadata",
                "video_id": video_id,
                "metadata_source": "provided",
                "source": "youtube",
            }
        raise ValueError("Could not identify a YouTube video, channel, or playlist from that URL.")

    local_kind, local_value = local_queue_target_from_url(target)
    if local_kind:
        target = local_value

    playlist_id = extract_playlist_id(target) or ""
    if local_kind == "playlist" or (not local_kind and playlist_id):
        playlist_id = playlist_id or target.strip()
        subject_key = enqueue_playlist_scan_item(
            conn,
            playlist_id,
            source_key=target if local_kind else playlist_id,
            priority=0,
            manual=True,
        )
        return {
            "subject_key": subject_key,
            "playlist_id": playlist_id,
            "queued_count": "1",
            "worker_type": "playlist",
            "source": "local",
        }

    channel_ref = ""
    if local_kind == "channel":
        channel_ref = target.strip()
    elif target.startswith(("@", "channel/", "c/", "user/")):
        channel_ref = youtube_channel_ref_from_url(target) or target.removeprefix("channel/")
    elif target.startswith(("UC", "HC")) and len(target) >= 20:
        channel_ref = target
    if channel_ref:
        subject_key = enqueue_metadata_item(
            conn,
            video_id=channel_ref,
            channel_id=channel_ref,
            channel_title=target,
            metadata_source="channel",
            source_key="local",
            priority=0,
            manual=True,
        )
        return {
            "subject_key": subject_key,
            "worker_type": "metadata",
            "channel_id": channel_ref,
            "metadata_source": "channel",
            "source": "local",
        }

    video_id = extract_video_id(target) or target.strip()
    if local_kind and local_kind != "video":
        raise ValueError("Could not identify a video, channel, or playlist from that local URL.")
    if not video_id:
        raise ValueError("Enter a video ID, channel ID, @handle, playlist ID, or URL.")
    subject_key = enqueue_metadata_item(
        conn,
        video_id=video_id,
        current_title=video_id,
        metadata_source="provided",
        source_key="local",
        priority=0,
        manual=True,
    )
    return {
        "subject_key": subject_key,
        "worker_type": "metadata",
        "video_id": video_id,
        "metadata_source": "provided",
        "source": "local",
    }


def admin_status(
    db_path: Path,
    metadata_worker: "MetadataWorker | None" = None,
    playlist_worker: "PlaylistScanWorker | None" = None,
    live_history_worker: "LiveHistoryWorker | None" = None,
    queue_dispatcher: "WorkerQueueDispatcher | None" = None,
    include_logs: bool = True,
    worker_queue_limit: int = 500,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(DISTINCT video_id) AS distinct_playlist_item_videos,
                  COUNT(*) AS playlist_item_rows,
                  (SELECT COUNT(*) FROM history_events) AS history_rows,
                  (SELECT COUNT(DISTINCT video_id) FROM history_events) AS distinct_history_videos
                FROM playlist_items
                WHERE video_id IS NOT NULL
                """
            ).fetchone()
        )
        live_history_counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(*) AS live_rows,
                  COUNT(DISTINCT video_id) AS live_video_ids,
                  COALESCE(MAX(imported_at), '') AS last_imported_at
                FROM history_events
                WHERE youtube_ordinal IS NOT NULL
                """
            ).fetchone()
        )
        playlist_counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(*) AS total_playlists,
                  SUM(CASE WHEN ps.playlist_id IS NULL THEN 1 ELSE 0 END) AS unscanned_playlists,
                  SUM(CASE WHEN ps.scan_status = 'ok' THEN 1 ELSE 0 END) AS scanned_ok,
                  SUM(CASE WHEN ps.scan_status <> '' AND ps.scan_status <> 'ok' THEN 1 ELSE 0 END) AS scan_errors
                FROM playlists p
                LEFT JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
                """
            ).fetchone()
        )
        metadata_counts = [
            dict(row)
            for row in conn.execute(
                """
                SELECT COALESCE(fetch_status, '') AS fetch_status, COUNT(*) AS count
                FROM videos
                GROUP BY COALESCE(fetch_status, '')
                ORDER BY count DESC
                """
            )
        ]
        channel_counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN COALESCE(thumbnail_path, '') <> '' THEN 1 ELSE 0 END) AS thumbnail_cached,
                  SUM(CASE WHEN COALESCE(thumbnail_path, '') = '' AND COALESCE(status, '') NOT IN ('terminated', 'deleted') THEN 1 ELSE 0 END) AS thumbnail_missing,
                  SUM(CASE WHEN COALESCE(status, '') IN ('terminated', 'deleted') THEN 1 ELSE 0 END) AS terminated,
                  0 AS url_missing
                FROM channels
                WHERE channel_id <> ''
                """
            ).fetchone()
        )
        worker_queue_count_value = worker_queue_count(conn)
        worker_queue_limit = max(0, min(10000, int(worker_queue_limit if worker_queue_limit is not None else 500)))
        worker_queue_preview_rows = (
            [dict(row) for row in worker_queue_rows(conn, limit=worker_queue_limit)]
            if worker_queue_limit > 0
            else []
        )
        latest_metadata_run = conn.execute(
            """
            SELECT *
            FROM metadata_worker_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        latest_playlist_run = conn.execute(
            """
            SELECT *
            FROM playlist_scan_worker_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        latest_live_history_run = conn.execute(
            """
            SELECT *
            FROM live_history_worker_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        latest_placeholder_recovery_run = conn.execute(
            """
            SELECT *
            FROM placeholder_recovery_worker_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        archivarix_request_counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN request_started_at >= ? THEN 1 ELSE 0 END) AS last_24_hours,
                  COALESCE(MAX(request_started_at), '') AS latest_at
                FROM placeholder_recovery_worker_runs
                WHERE request_started_at IS NOT NULL
                """,
                (utc_days_ago(1),),
            ).fetchone()
        )
        archivarix_block = external_service_block(conn, "archivarix")
        metadata_logs: list[dict[str, Any]] = []
        playlist_logs: list[dict[str, Any]] = []
        live_history_logs: list[dict[str, Any]] = []
        placeholder_recovery_logs: list[dict[str, Any]] = []
        if include_logs:
            metadata_logs = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM metadata_worker_log
                    ORDER BY id DESC
                    LIMIT 80
                    """
                )
            ]
            playlist_logs = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM playlist_scan_worker_log
                    ORDER BY id DESC
                    LIMIT 80
                    """
                )
            ]
            live_history_logs = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM live_history_worker_log
                    ORDER BY id DESC
                    LIMIT 80
                    """
                )
            ]
            placeholder_recovery_logs = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM placeholder_recovery_worker_log
                    ORDER BY id DESC
                    LIMIT 80
                    """
                )
            ]
    finally:
        conn.close()
    return {
        "metadataRunning": metadata_worker.is_running() if metadata_worker else False,
        "metadataStopping": metadata_worker.is_stopping() if metadata_worker else False,
        "playlistScanRunning": playlist_worker.is_running() if playlist_worker else False,
        "playlistScanStopping": playlist_worker.is_stopping() if playlist_worker else False,
        "liveHistoryRunning": live_history_worker.is_running() if live_history_worker else False,
        "liveHistoryStopping": live_history_worker.is_stopping() if live_history_worker else False,
        "workerQueueRunning": queue_dispatcher.is_running() if queue_dispatcher else False,
        "workerQueueStopping": queue_dispatcher.is_stopping() if queue_dispatcher else False,
        "workerQueueStats": queue_dispatcher.stats(worker_queue_count_value)
        if queue_dispatcher
        else {
            "started_at": "",
            "elapsed_seconds": 0,
            "eta_seconds": 0,
            "eta_available": False,
            "initial_count": 0,
            "completed_count": 0,
            "remaining_count": worker_queue_count_value,
        },
        "counts": counts,
        "liveHistoryCounts": live_history_counts,
        "playlistCounts": playlist_counts,
        "metadataCounts": metadata_counts,
        "channelCounts": channel_counts,
        "workerQueueCount": worker_queue_count_value,
        "workerQueue": worker_queue_preview_rows,
        "latestRun": dict(latest_metadata_run) if latest_metadata_run else None,
        "latestMetadataRun": dict(latest_metadata_run) if latest_metadata_run else None,
        "latestPlaylistScanRun": dict(latest_playlist_run) if latest_playlist_run else None,
        "latestLiveHistoryRun": dict(latest_live_history_run) if latest_live_history_run else None,
        "latestPlaceholderRecoveryRun": (
            dict(latest_placeholder_recovery_run) if latest_placeholder_recovery_run else None
        ),
        "archivarixRequestCounts": archivarix_request_counts,
        "archivarixBlock": archivarix_block,
        "logs": metadata_logs,
        "metadataLogs": metadata_logs,
        "playlistScanLogs": playlist_logs,
        "liveHistoryLogs": live_history_logs,
        "placeholderRecoveryLogs": placeholder_recovery_logs,
    }
def reconcile_worker_runs(
    db_path: Path,
    metadata_worker: "MetadataWorker | None" = None,
    playlist_worker: "PlaylistScanWorker | None" = None,
    live_history_worker: "LiveHistoryWorker | None" = None,
    placeholder_recovery_worker: "PlaceholderRecoveryWorker | None" = None,
) -> None:
    metadata_running = metadata_worker.is_running() if metadata_worker else False
    playlist_running = playlist_worker.is_running() if playlist_worker else False
    live_history_running = live_history_worker.is_running() if live_history_worker else False
    placeholder_recovery_running = (
        placeholder_recovery_worker.is_running() if placeholder_recovery_worker else False
    )
    now = utc_now()
    conn = connect(db_path)
    try:
        with conn:
            if not metadata_running:
                conn.execute(
                    """
                    UPDATE metadata_worker_runs
                    SET status = 'interrupted',
                        finished_at = ?,
                        message = CASE
                          WHEN message = '' THEN 'Interrupted by server restart'
                          ELSE message || ' (interrupted by server restart)'
                        END
                    WHERE status = 'running'
                    """,
                    (now,),
                )
            if not playlist_running:
                conn.execute(
                    """
                    UPDATE playlist_scan_worker_runs
                    SET status = 'interrupted',
                        finished_at = ?,
                        message = CASE
                          WHEN message = '' THEN 'Interrupted by server restart'
                          ELSE message || ' (interrupted by server restart)'
                        END
                    WHERE status = 'running'
                    """,
                    (now,),
                )
            if not live_history_running:
                conn.execute(
                    """
                    UPDATE live_history_worker_runs
                    SET status = 'interrupted',
                        finished_at = ?,
                        message = CASE
                          WHEN message = '' THEN 'Interrupted by server restart'
                          ELSE message || ' (interrupted by server restart)'
                        END
                    WHERE status = 'running'
                    """,
                    (now,),
                )
            if not placeholder_recovery_running:
                conn.execute(
                    """
                    UPDATE placeholder_recovery_worker_runs
                    SET status = 'interrupted',
                        finished_at = ?,
                        message = CASE
                          WHEN message = '' THEN 'Interrupted by server restart'
                          ELSE message || ' (interrupted by server restart)'
                        END
                    WHERE status = 'running'
                    """,
                    (now,),
                )
    finally:
        conn.close()


def scan_hidden(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    conn = connect(db_path)
    opener = load_cookie_opener(Path(args.cookies))
    rows = conn.execute(
        "SELECT playlist_id, title FROM playlists ORDER BY title COLLATE NOCASE"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"Scanning {len(rows)} playlists for unavailable videos...")
    total_hidden = 0
    for index, row in enumerate(rows, start=1):
        playlist_id = row["playlist_id"]
        title = row["title"]
        status = "ok"
        error = ""
        videos: list[dict[str, Any]] = []
        try:
            videos = scan_playlist_videos(opener, playlist_id, Path(args.cookies))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            status = "error"
            error = str(exc)
        unavailable_count = sum(1 for video in videos if not video["is_playable"])
        total_hidden += unavailable_count
        with conn:
            save_playlist_scan(conn, playlist_id, videos, status, error)
        suffix = f"{unavailable_count} unavailable / {len(videos)} videos"
        if status != "ok":
            suffix = f"ERROR {error}"
        print(f"[{index:03d}/{len(rows):03d}] {suffix} - {title}")
    print(f"Found {total_hidden} unavailable videos.")


def recover_archivarix_thumbnails(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    thumb_dir = Path(args.thumbs)
    conn = connect(db_path)
    rows = conn.execute(
        """
        SELECT p.playlist_id, p.title, s.unavailable_count
        FROM playlist_scans s
        JOIN playlists p ON p.playlist_id = s.playlist_id
        WHERE s.unavailable_count > 0
        ORDER BY s.unavailable_count DESC, p.title COLLATE NOCASE
        """
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"Searching Archivarix for {len(rows)} affected playlists...")
    total_candidates = 0
    total_cached = 0
    for index, row in enumerate(rows, start=1):
        playlist_id = row["playlist_id"]
        query = row["title"]
        try:
            videos = archivarix_search_deleted(query, page_size=args.page_size)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"[{index:03d}/{len(rows):03d}] ERROR {query}: {exc}")
            continue
        candidates = [
            video
            for video in videos
            if isinstance(video, dict)
            and isinstance(video.get("videoId"), str)
            and video.get("status", "").upper().startswith("DELETED")
        ]
        cached = 0
        with conn:
            for video in candidates:
                video_id = video["videoId"]
                thumbnail_url = (
                    video.get("thumbnailArchiveUrl")
                    or video.get("thumbnailUrl")
                    or ""
                )
                thumbnail_path = cache_archivarix_thumbnail(
                    video_id, thumbnail_url, thumb_dir
                )
                if thumbnail_path:
                    cached += 1
                if conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,)).fetchone():
                    save_video_recovery(
                        conn,
                        video_id,
                        video,
                        "found",
                        "",
                        thumbnail_url,
                        thumbnail_path,
                    )
        total_candidates += len(candidates)
        total_cached += cached
        print(
            f"[{index:03d}/{len(rows):03d}] "
            f"{len(candidates)} candidates, {cached} thumbnails - {query}"
        )
    print(f"Found {total_candidates} candidates and cached {total_cached} thumbnails.")


def normalized_playlist_title(value: str) -> str:
    value = re.sub(r"\(\d+\)$", "", value).strip()
    return value


def import_takeout_playlists(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    takeout_dir = Path(args.takeout)
    playlists_dir = takeout_dir / "playlists"
    playlists_csv = playlists_dir / "playlists.csv"
    if not playlists_csv.exists():
        raise SystemExit(f"Takeout playlists.csv not found: {playlists_csv}")

    conn = connect(db_path)
    now = utc_now()
    playlist_rows: list[dict[str, str]] = []
    with playlists_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        playlist_rows = list(csv.DictReader(handle))

    playlist_by_title: dict[str, dict[str, str]] = {}
    for row in playlist_rows:
        title = row.get("Playlist Title (Original)", "").strip()
        if title:
            playlist_by_title[title.casefold()] = row

    with conn:
        for row in playlist_rows:
            playlist_id = row.get("Playlist ID", "").strip()
            if not playlist_id:
                continue
            conn.execute(
                """
                INSERT INTO playlists(playlist_id, title, visibility, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                  title=COALESCE(NULLIF(excluded.title, ''), playlists.title),
                  visibility=COALESCE(NULLIF(excluded.visibility, ''), playlists.visibility),
                  updated_at=excluded.updated_at
                """,
                (
                    playlist_id,
                    row.get("Playlist Title (Original)", "").strip(),
                    normalize_playlist_visibility(row.get("Playlist Visibility", "")),
                    now,
                ),
            )

    imported_video_rows = 0
    unmatched_files: list[str] = []
    video_files = sorted(playlists_dir.glob("*-videos.csv"))
    with conn:
        for video_file in video_files:
            title_from_file = normalized_playlist_title(video_file.name[: -len("-videos.csv")])
            playlist_row = playlist_by_title.get(title_from_file.casefold())
            if playlist_row is None:
                playlist_row = playlist_by_title.get(title_from_file.replace("_", "/").casefold())
            if playlist_row is None and title_from_file.endswith("_"):
                playlist_row = playlist_by_title.get((title_from_file[:-1] + "?").casefold())
            if playlist_row is None:
                unmatched_files.append(video_file.name)
                playlist_id = f"takeout:{title_from_file}"
                playlist_title = title_from_file
                conn.execute(
                    """
                    INSERT OR IGNORE INTO playlists(playlist_id, title, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        playlist_id,
                        playlist_title,
                        now,
                    ),
                )
            else:
                playlist_id = playlist_row.get("Playlist ID", "").strip()
                playlist_title = playlist_row.get("Playlist Title (Original)", "").strip()
            if not playlist_id:
                continue
            conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
            with video_file.open("r", encoding="utf-8-sig", newline="") as handle:
                for position, row in enumerate(csv.DictReader(handle), start=1):
                    video_id = row.get("Video ID", "").strip()
                    if not video_id:
                        continue
                    upsert_video(conn, video_id, source="takeout", updated_at=now)
                    added_at = row.get("Playlist Video Creation Timestamp", "").strip()
                    if added_at:
                        try:
                            added_at = normalize_utc_timestamp(added_at)
                        except ValueError:
                            pass
                    conn.execute(
                        """
                        INSERT INTO playlist_items(
                          playlist_id, position, video_id, membership_state,
                          source_quality, added_at, updated_at
                        )
                        VALUES (?, ?, ?, 'current', 'takeout', ?, ?)
                        """,
                        (
                            playlist_id,
                            position,
                            video_id,
                            added_at or None,
                            now,
                        ),
                    )
                    imported_video_rows += 1
        reconcile_stats = rebuild_playlist_reconciliation(conn)

    print(
        f"Imported {len(playlist_rows)} Takeout playlists and "
        f"{imported_video_rows} current playlist items."
    )
    print(
        f"Reconciled {reconcile_stats['rows']} playlist rows across "
        f"{reconcile_stats['playlists']} playlists."
    )
    if unmatched_files:
        print(f"Used filename-derived IDs for {len(unmatched_files)} playlist files.")


def strip_html_fragment(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def display_source_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def extract_video_id(value: str) -> str:
    parsed = urllib.parse.urlparse(html.unescape(value))
    if parsed.netloc.endswith("youtube.com") and parsed.path == "/watch":
        return (urllib.parse.parse_qs(parsed.query).get("v") or [""])[0]
    if parsed.netloc.endswith("youtube.com") and parsed.path.startswith(("/shorts/", "/embed/")):
        return parsed.path.strip("/").split("/", 1)[1].split("/", 1)[0]
    if parsed.netloc == "youtu.be":
        return parsed.path.strip("/")
    return ""


def parse_takeout_watch_history_text(text: str) -> list[dict[str, str]]:
    stripped = text.lstrip()
    if stripped.startswith("["):
        rows: list[dict[str, str]] = []
        try:
            records = json.loads(text)
        except json.JSONDecodeError:
            records = []
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            url = str(record.get("titleUrl") or "")
            video_id = extract_video_id(url)
            if not video_id:
                continue
            title = str(record.get("title") or "").strip()
            title = re.sub(r"^Watched\s+", "", title, flags=re.I).strip()
            subtitles = record.get("subtitles") if isinstance(record.get("subtitles"), list) else []
            channel = ""
            channel_url = ""
            if subtitles and isinstance(subtitles[0], dict):
                channel = str(subtitles[0].get("name") or "")
                channel_url = str(subtitles[0].get("url") or "")
            rows.append(
                {
                    "url": url,
                    "video_id": video_id,
                    "title": title or video_id,
                    "channel_id": youtube_channel_id_from_url(channel_url),
                    "channel_url": channel_url,
                    "channel": channel,
                    "watched_at": str(record.get("time") or ""),
                }
            )
        return rows

    action = r"(Watched|Viewed)"
    sep = r"(?:\s|&nbsp;|\xa0)*"
    anchor = r'<a href="([^"]+)">(.*?)</a>'
    pattern = re.compile(
        action + sep + anchor + r"<br>(?:" + anchor + r"<br>)?([^<]+)<br>",
        re.IGNORECASE | re.DOTALL,
    )
    rows: list[dict[str, str]] = []
    for match in pattern.finditer(text):
        url = html.unescape(match.group(2))
        video_id = extract_video_id(url)
        if not video_id:
            continue
        rows.append(
            {
                "url": url,
                "video_id": video_id,
                "title": strip_html_fragment(match.group(3)),
                "channel_id": youtube_channel_id_from_url(html.unescape(match.group(4) or "")),
                "channel_url": html.unescape(match.group(4) or ""),
                "channel": strip_html_fragment(match.group(5) or ""),
                "watched_at": strip_html_fragment(match.group(6)),
            }
        )
    return rows


def parse_takeout_watch_history(path: Path) -> list[dict[str, str]]:
    return parse_takeout_watch_history_text(path.read_text(encoding="utf-8", errors="replace"))


def takeout_key_from_path(path: Path) -> str:
    found = re.search(r"takeout-([0-9]{8}T[0-9]{6}Z)", path.name, re.I)
    if found:
        return found.group(1)
    return ""


def find_takeout_zips(path: Path) -> list[Path]:
    if path.is_file() and path.suffix.lower() == ".zip":
        return [path]
    if path.is_dir():
        return sorted(path.glob("takeout-*.zip"), key=lambda item: takeout_key_from_path(item) or item.name)
    return []


def find_takeout_zip(path: Path) -> Path | None:
    zips = find_takeout_zips(path)
    return zips[-1] if zips else None


def read_zip_member_text(zf: zipfile.ZipFile, suffix: str) -> str:
    normalized_suffix = suffix.replace("\\", "/").lower()
    for name in zf.namelist():
        if name.replace("\\", "/").lower().endswith(normalized_suffix):
            return zf.read(name).decode("utf-8", "replace")
    return ""


def import_takeout_playlists_zip(conn: sqlite3.Connection, zip_path: Path) -> dict[str, int]:
    now = utc_now()
    with zipfile.ZipFile(zip_path) as zf:
        playlists_text = read_zip_member_text(zf, "playlists/playlists.csv")
        if not playlists_text:
            return {"playlists": 0, "items": 0, "unmatched": 0}
        playlist_rows = list(csv.DictReader(io.StringIO(playlists_text)))
        by_title = {
            (row.get("Playlist Title (Original)") or "").strip().casefold(): row
            for row in playlist_rows
            if (row.get("Playlist Title (Original)") or "").strip()
        }
        for row in playlist_rows:
            playlist_id = (row.get("Playlist ID") or "").strip()
            if not playlist_id:
                continue
            conn.execute(
                """
                INSERT INTO playlists(playlist_id, title, visibility, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                  title=COALESCE(NULLIF(excluded.title, ''), playlists.title),
                  visibility=COALESCE(NULLIF(excluded.visibility, ''), playlists.visibility),
                  updated_at=excluded.updated_at
                """,
                (
                    playlist_id,
                    (row.get("Playlist Title (Original)") or "").strip(),
                    normalize_playlist_visibility(row.get("Playlist Visibility") or ""),
                    now,
                ),
            )

        item_count = 0
        unmatched = 0
        for member_name in zf.namelist():
            normalized = member_name.replace("\\", "/")
            if not normalized.lower().endswith("-videos.csv") or "/playlists/" not in normalized.lower():
                continue
            filename = normalized.rsplit("/", 1)[-1]
            title = normalized_playlist_title(filename[: -len("-videos.csv")])
            playlist_row = by_title.get(title.casefold())
            if playlist_row is None:
                playlist_row = by_title.get(title.replace("_", "/").casefold())
            if playlist_row is None and title.endswith("_"):
                playlist_row = by_title.get((title[:-1] + "?").casefold())
            if playlist_row is None:
                unmatched += 1
                continue
            playlist_id = (playlist_row.get("Playlist ID") or "").strip()
            if not playlist_id:
                continue
            scanned = conn.execute(
                "SELECT 1 FROM playlist_scans WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            if not scanned:
                conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
            text = zf.read(member_name).decode("utf-8-sig", "replace")
            for position, row in enumerate(csv.DictReader(io.StringIO(text)), start=1):
                video_id = (row.get("Video ID") or "").strip()
                if not video_id:
                    continue
                upsert_video(conn, video_id, source="takeout", updated_at=now)
                if scanned:
                    continue
                added_at = (row.get("Playlist Video Creation Timestamp") or "").strip()
                if added_at:
                    try:
                        added_at = normalize_utc_timestamp(added_at)
                    except ValueError:
                        pass
                conn.execute(
                    """
                    INSERT INTO playlist_items(
                      playlist_id, position, video_id, membership_state,
                      source_quality, added_at, updated_at
                    ) VALUES (?, ?, ?, 'current', 'takeout', ?, ?)
                    """,
                    (playlist_id, position, video_id, added_at or None, now),
                )
                item_count += 1
    return {"playlists": len(playlist_rows), "items": item_count, "unmatched": unmatched}


def load_takeout_subscriptions(takeout_path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for zip_path in find_takeout_zips(takeout_path):
        with zipfile.ZipFile(zip_path) as zf:
            text = read_zip_member_text(zf, "subscriptions/subscriptions.csv")
        if not text:
            continue
        for row in csv.DictReader(io.StringIO(text)):
            channel_id = (row.get("Channel Id") or row.get("Channel ID") or "").strip()
            channel_url = (row.get("Channel Url") or row.get("Channel URL") or "").strip()
            if not channel_id:
                channel_id = youtube_channel_id_from_url(channel_url)
            if not channel_id:
                continue
            rows.append(
                {
                    "channel_id": channel_id,
                    "channel_url": channel_url,
                    "title": (row.get("Channel Title") or "").strip(),
                }
            )
        if rows:
            break
    return rows


def sync_takeout_subscriptions(conn: sqlite3.Connection, takeout_path: Path) -> None:
    existing = conn.execute("SELECT COUNT(*) FROM channels WHERE subscribed = 1").fetchone()[0]
    if existing:
        return
    rows = load_takeout_subscriptions(takeout_path)
    if not rows:
        return
    now = utc_now()
    for row in rows:
        channel_id = upsert_channel(
            conn,
            row["channel_id"],
            title=row["title"],
            url=row["channel_url"],
            source="takeout_subscriptions",
            updated_at=now,
        )
        if channel_id:
            conn.execute("UPDATE channels SET subscribed = 1 WHERE channel_id = ?", (channel_id,))


def load_takeout_history_source(takeout_path: Path, requested_key: str = "") -> tuple[str, str]:
    zip_path = find_takeout_zip(takeout_path)
    if zip_path:
        history_key = requested_key or takeout_key_from_path(zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            watch_text = read_zip_member_text(zf, "history/watch-history.json")
            if not watch_text:
                watch_text = read_zip_member_text(zf, "history/watch-history.html")
        if not watch_text:
            raise SystemExit(f"Takeout watch history file not found in {zip_path}")
        if not history_key:
            raise SystemExit(f"Could not derive Takeout history key from {zip_path.name}")
        return history_key, watch_text

    history_dir = takeout_path / "history"
    watch_file = history_dir / "watch-history.html"
    if not watch_file.exists():
        raise SystemExit(f"Takeout watch history file not found in {history_dir}")
    history_key = requested_key or takeout_key_from_path(takeout_path) or takeout_path.name
    watch_text = watch_file.read_text(encoding="utf-8", errors="replace") if watch_file.exists() else ""
    return history_key, watch_text


def load_takeout_history_sources(takeout_path: Path, requested_key: str = "") -> list[tuple[Path | None, str, str]]:
    if requested_key:
        source_zip = find_takeout_zip(takeout_path)
        source_path = source_zip or takeout_path
        history_key, watch_text = load_takeout_history_source(source_path, requested_key)
        return [(source_zip, history_key, watch_text)]
    zips = find_takeout_zips(takeout_path)
    if zips:
        sources = []
        for zip_path in zips:
            history_key, watch_text = load_takeout_history_source(zip_path, "")
            sources.append((zip_path, history_key, watch_text))
        return sources
    history_key, watch_text = load_takeout_history_source(takeout_path, "")
    return [(None, history_key, watch_text)]


def existing_takeout_history_event_keys(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        (row["video_id"] or "", row["watched_at"] or "")
        for row in conn.execute(
            """
            SELECT video_id, watched_at
            FROM history_events
            WHERE takeout_history_key IS NOT NULL
            """
        )
    }


def takeout_import_message(stats: dict[str, Any]) -> str:
    keys = ", ".join(stats.get("imported_keys") or [])
    return (
        f"Takeout import: {stats['inserted_watch_rows']} new, "
        f"{stats['duplicate_watch_rows']} duplicates skipped, "
        f"{stats['total_watch_rows']} rows scanned from {keys}; "
        f"{stats['reconciled_rows']} reconciled, {stats['matched_rows']} matched"
    )


def import_history(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db)
    takeout_path = Path(args.takeout)
    requested_key = getattr(args, "history_key", "") or ""
    sources = load_takeout_history_sources(takeout_path, requested_key)

    conn = connect(db_path)
    imported_at = utc_now()
    total_watch_rows = 0
    inserted_watch_rows = 0
    duplicate_watch_rows = 0
    distinct_video_ids: set[str] = set()
    imported_keys: list[str] = []
    playlist_stats = {"playlists": 0, "items": 0, "unmatched": 0}
    with conn:
        sync_takeout_subscriptions(conn, takeout_path)
        for zip_path, _history_key, _watch_text in sources:
            if zip_path:
                stats = import_takeout_playlists_zip(conn, zip_path)
                playlist_stats["playlists"] += stats["playlists"]
                playlist_stats["items"] += stats["items"]
                playlist_stats["unmatched"] += stats["unmatched"]
        existing_events = existing_takeout_history_event_keys(conn)
        for _source_path, history_key, watch_text in sources:
            imported_keys.append(history_key)
            watch_rows = parse_takeout_watch_history_text(watch_text) if watch_text else []
            total_watch_rows += len(watch_rows)
            for position, row in enumerate(watch_rows, start=1):
                watched_at_iso = takeout_watch_datetime(row["watched_at"])
                event_key = (row["video_id"], watched_at_iso)
                if event_key in existing_events:
                    duplicate_watch_rows += 1
                    continue
                row_hash = history_row_hash(row)
                if row["video_id"]:
                    distinct_video_ids.add(row["video_id"])
                channel_id = upsert_channel(
                    conn,
                    row.get("channel_id") or youtube_channel_id_from_url(row["channel_url"]),
                    title=row["channel"],
                    url=row["channel_url"],
                    source="takeout_history",
                    updated_at=imported_at,
                )
                upsert_video(
                    conn,
                    row["video_id"],
                    title=row["title"],
                    channel_id=channel_id,
                    channel_title=row["channel"],
                    channel_url=row["channel_url"],
                    source="takeout",
                    updated_at=imported_at,
                )
                row_key = f"{row_hash}:{position}"
                conn.execute(
                    """
                    INSERT INTO history_events(
                      event_id, video_id, watched_at, watch_date, time_precision,
                      source_type, match_type, takeout_history_key, takeout_row_key,
                      imported_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'takeout', 'takeout_only', ?, ?, ?, ?)
                    """,
                    (
                        f"takeout:{history_key}:{row_key}",
                        row["video_id"],
                        watched_at_iso,
                        watched_at_iso[:10] if watched_at_iso else None,
                        "exact" if watched_at_iso else "unknown",
                        history_key,
                        row_key,
                        imported_at,
                        imported_at,
                    ),
                )
                inserted_watch_rows += 1
                existing_events.add(event_key)
        timezone_name = effective_display_timezone(getattr(args, "config_data", {}))
        stats = rebuild_history_reconciliation(conn, timezone_name)
    conn.close()
    result = {
        "total_watch_rows": total_watch_rows,
        "inserted_watch_rows": inserted_watch_rows,
        "duplicate_watch_rows": duplicate_watch_rows,
        "distinct_video_ids": len(distinct_video_ids),
        "imported_keys": imported_keys,
        "reconciled_rows": stats["rows"],
        "matched_rows": stats["matched"],
        "playlist_stats": playlist_stats,
    }
    print(
        f"Imported {inserted_watch_rows} new watch history rows from {', '.join(imported_keys)} "
        f"({duplicate_watch_rows} duplicates skipped, {total_watch_rows} rows scanned, "
        f"{len(distinct_video_ids)} distinct videos). "
        f"Reconciled {stats['rows']} rows ({stats['matched']} matched). "
        f"Loaded {playlist_stats['playlists']} playlists and {playlist_stats['items']} playlist items."
    )
    return result


def recover_unavailable_videos(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    thumb_dir = Path(args.thumbs)
    conn = connect(db_path)
    archivarix_opener = load_cookie_opener(Path(args.archivarix_cookies))
    where_clauses = [
        "pi.video_id IS NOT NULL",
        "(pi.membership_state = 'retained_unavailable' OR v.is_playable = 0)",
    ]
    params: list[Any] = []
    if args.likely_unavailable_only:
        where_clauses.append(
            """
            EXISTS (
              SELECT 1
              FROM playlist_scans ps
              WHERE ps.playlist_id = pi.playlist_id
                AND ps.unavailable_count > 0
            )
            """
        )
    rows = conn.execute(
        f"""
        SELECT DISTINCT pi.video_id
        FROM playlist_items pi
        JOIN playlists p ON p.playlist_id = pi.playlist_id
        JOIN videos v ON v.video_id = pi.video_id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY sv.video_id
        """,
        params,
    ).fetchall()
    if args.video_id:
        rows = [row for row in rows if row["video_id"] == args.video_id]
    if args.only_missing:
        rows = [
            row
            for row in rows
            if conn.execute(
                """
                SELECT 1
                FROM video_recovery vr
                JOIN videos v ON v.video_id = vr.video_id
                WHERE vr.video_id = ? AND v.thumbnail_path <> ''
                """,
                (row["video_id"],),
            ).fetchone()
            is None
        ]
    if args.limit:
        rows = rows[: args.limit]
    scope = "likely unavailable" if args.likely_unavailable_only else "unavailable"
    print(f"Recovering Archivarix thumbnails for {len(rows)} {scope} video IDs...")
    found = 0
    cached = 0
    channel_cache: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        video_id = row["video_id"]
        video, thumbnail_url, thumbnail_path, status, error = recover_archivarix_video(
            video_id,
            thumb_dir,
            archivarix_opener,
            refresh_metadata=args.refresh_metadata,
            no_api=args.no_api,
            delay=args.delay,
            channel_cache=channel_cache,
        )
        if status == "thumbnail_only":
            cached += 1
        if video:
            found += 1
            if thumbnail_path:
                cached += 1
        with conn:
            save_video_recovery(
                conn,
                video_id,
                video,
                status,
                error,
                thumbnail_url,
                thumbnail_path,
            )
        label = (video or {}).get("title") or video_id
        suffix = "thumbnail" if thumbnail_path else status
        print(f"[{index:03d}/{len(rows):03d}] {suffix} - {label}")
    with conn:
        reconcile_stats = rebuild_playlist_reconciliation(conn)
    print(f"Found {found} Archivarix records and cached {cached} thumbnails.")
    print(
        f"Reconciled {reconcile_stats['rows']} playlist rows across "
        f"{reconcile_stats['playlists']} playlists."
    )
