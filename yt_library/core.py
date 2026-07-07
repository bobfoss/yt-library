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

from .schema import load_schema

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "yt_library.sqlite3"
DEFAULT_THUMB_DIR = ROOT / "thumbs"
DEFAULT_ARCHIVARIX_THUMB_DIR = ROOT / "archivarix_thumbs"
DEFAULT_VIDEO_THUMB_DIR = ROOT / "video_thumbs"
COOKIE_FILE = ROOT / "YT cookies.txt"
ARCHIVARIX_COOKIE_FILE = ROOT / "archivarix.net cookies.txt"
POCKETTUBE_EXPORT = ROOT / "youtube_playlist_manager_2026-07-02-17_13.json"
TAKEOUT_DIR = ROOT / "YouTube and YouTube Music"
HISTORY_BATCH_SIZE = 1000
HISTORY_BATCH_DELAY_SECONDS = 10.0

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


SCHEMA = load_schema()


@dataclass(frozen=True)
class GroupNode:
    key: str
    name: str
    parent_key: str | None
    position: int
    icon: str


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    ensure_columns(
        conn,
        "playlist_videos",
        {"channel_id": "TEXT NOT NULL DEFAULT ''"},
    )
    ensure_columns(
        conn,
        "playlist_video_reconciled",
        {
            "channel_id": "TEXT NOT NULL DEFAULT ''",
            "match_type": "TEXT NOT NULL DEFAULT ''",
        },
    )
    ensure_playlist_video_reconciled_schema(conn)
    ensure_columns(
        conn,
        "archivarix_candidates",
        {"channel_id": "TEXT NOT NULL DEFAULT ''"},
    )
    ensure_columns(
        conn,
        "channels",
        {
            "description": "TEXT NOT NULL DEFAULT ''",
            "aliases": "TEXT NOT NULL DEFAULT ''",
            "subscribed": "INTEGER NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT ''",
            "status_reason": "TEXT NOT NULL DEFAULT ''",
            "fetch_status": "TEXT NOT NULL DEFAULT ''",
            "fetch_error": "TEXT NOT NULL DEFAULT ''",
            "fetched_at": "INTEGER NOT NULL DEFAULT 0",
        },
    )
    ensure_columns(
        conn,
        "snapshot_video_recovery",
        {
            "description": "TEXT NOT NULL DEFAULT ''",
            "channel_id": "TEXT NOT NULL DEFAULT ''",
            "archivarix_channel_id": "TEXT NOT NULL DEFAULT ''",
        },
    )
    ensure_columns(
        conn,
        "video_metadata",
        {
            "channel_id": "TEXT NOT NULL DEFAULT ''",
            "reaction": "TEXT NOT NULL DEFAULT ''",
        },
    )
    ensure_columns(
        conn,
        "youtube_history_occurrences",
        {"channel_id": "TEXT NOT NULL DEFAULT ''"},
    )
    ensure_columns(
        conn,
        "takeout_history_occurrences",
        {"channel_id": "TEXT NOT NULL DEFAULT ''"},
    )
    ensure_columns(
        conn,
        "history_reconciled",
        {"channel_id": "TEXT NOT NULL DEFAULT ''"},
    )
    ensure_youtube_history_schema(conn)
    ensure_takeout_history_schema(conn)
    ensure_history_reconciled_schema(conn)
    drop_legacy_history_tables(conn)
    ensure_channel_indexes(conn)
    if channel_backfill_needed(conn):
        backfill_channels(conn)
    backfill_playlist_channel_ids_by_name(conn)
    sync_takeout_subscriptions(conn, ROOT)
    drop_deprecated_channel_columns(conn)
    reconciled_count = conn.execute("SELECT COUNT(*) AS count FROM playlist_video_reconciled").fetchone()["count"]
    raw_playlist_count = conn.execute("SELECT COUNT(*) AS count FROM playlist_videos").fetchone()["count"]
    if reconciled_count == 0 and raw_playlist_count:
        rebuild_playlist_reconciliation(conn)
    conn.commit()
    return conn


def ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def table_row_count(conn: sqlite3.Connection, table: str) -> int:
    if not table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)


def begin_table_rebuild(conn: sqlite3.Connection, table: str) -> str:
    old_table = f"{table}_old"
    if table_exists(conn, old_table):
        if table_row_count(conn, table) > table_row_count(conn, old_table):
            conn.execute(f"DROP TABLE {old_table}")
            conn.execute(f"ALTER TABLE {table} RENAME TO {old_table}")
        else:
            conn.execute(f"DROP TABLE {table}")
    else:
        conn.execute(f"ALTER TABLE {table} RENAME TO {old_table}")
    return old_table


def playlist_match_type_note(match_type: str) -> str:
    return PLAYLIST_MATCH_TYPE_NOTES.get(match_type or "", "")


def playlist_match_type_label(match_type: str) -> str:
    return PLAYLIST_MATCH_TYPE_LABELS.get(match_type or "", "")


def playlist_match_type_from_legacy(source_quality: str, match_notes: str) -> str:
    source_quality = source_quality or ""
    if source_quality in PLAYLIST_MATCH_TYPE_NOTES:
        return source_quality
    normalized_note = (match_notes or "").strip()
    for match_type, note in PLAYLIST_MATCH_TYPE_NOTES.items():
        if normalized_note == note:
            return match_type
    return ""


def playlist_source_quality_from_legacy(source_quality: str, match_type: str) -> str:
    if source_quality not in PLAYLIST_MATCH_TYPE_NOTES:
        return source_quality or ""
    if match_type in {"ambiguous_hidden_candidate", "inferred_hidden_slot"}:
        return "takeout"
    if match_type == "ambiguous_hidden_slot":
        return "current"
    return ""


def video_availability_from_recovery_status(status: str) -> str:
    status = (status or "").strip()
    status_upper = status.upper()
    if status_upper == "LIVE":
        return "live"
    if status_upper == "NOT_FOUND" or status_upper.startswith("DELETED_"):
        return "unavailable"
    return ""


def normalize_video_availability(
    video_id: str,
    availability: str = "",
    is_playable: bool | int | None = None,
    recovered_status: str = "",
) -> str:
    if not video_id:
        return ""
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
    if availability:
        return availability
    recovered_availability = video_availability_from_recovery_status(recovered_status)
    if recovered_availability:
        return recovered_availability
    if is_playable:
        return "public"
    return ""


def reconciled_video_availability(
    video_id: str,
    current_availability: str = "",
    recovered_status: str = "",
    is_playable: bool | int | None = None,
) -> str:
    return normalize_video_availability(video_id, current_availability, is_playable, recovered_status)


def ensure_playlist_video_reconciled_schema(conn: sqlite3.Connection) -> None:
    cols = table_columns(conn, "playlist_video_reconciled")
    if "match_type" in cols and "match_notes" not in cols:
        return
    old_table = begin_table_rebuild(conn, "playlist_video_reconciled")
    old_cols = table_columns(conn, old_table)
    conn.execute(
        """
        CREATE TABLE playlist_video_reconciled (
          playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
          display_position INTEGER NOT NULL,
          current_position INTEGER NOT NULL DEFAULT 0,
          snapshot_position INTEGER NOT NULL DEFAULT 0,
          video_id TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          channel_id TEXT NOT NULL DEFAULT '',
          channel TEXT NOT NULL DEFAULT '',
          duration_text TEXT NOT NULL DEFAULT '',
          is_playable INTEGER NOT NULL DEFAULT 1,
          availability TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          source_quality TEXT NOT NULL DEFAULT '',
          match_type TEXT NOT NULL DEFAULT '',
          match_confidence TEXT NOT NULL DEFAULT '',
          snapshot_key TEXT NOT NULL DEFAULT '',
          added_at TEXT NOT NULL DEFAULT '',
          updated_at INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (playlist_id, display_position)
        )
        """
    )
    for row in conn.execute(f"SELECT * FROM {old_table}").fetchall():
        source_quality = row["source_quality"] if "source_quality" in old_cols else ""
        legacy_notes = row["match_notes"] if "match_notes" in old_cols else ""
        match_type = (
            row["match_type"] if "match_type" in old_cols else ""
        ) or playlist_match_type_from_legacy(source_quality, legacy_notes)
        source_quality = playlist_source_quality_from_legacy(source_quality, match_type)
        video_id = row["video_id"] if "video_id" in old_cols else ""
        snapshot_key = row["snapshot_key"] if "snapshot_key" in old_cols else ""
        recovered_status = ""
        if video_id and snapshot_key:
            recovery = conn.execute(
                """
                SELECT status
                FROM snapshot_video_recovery
                WHERE snapshot_key = ? AND video_id = ?
                """,
                (snapshot_key, video_id),
            ).fetchone()
            recovered_status = recovery["status"] if recovery else ""
        availability = reconciled_video_availability(
            video_id,
            row["availability"] if "availability" in old_cols else "",
            recovered_status,
            row["is_playable"] if "is_playable" in old_cols else 1,
        )
        if availability in PLAYLIST_MATCH_TYPE_NOTES.values():
            availability = reconciled_video_availability(
                video_id,
                "",
                recovered_status,
                row["is_playable"] if "is_playable" in old_cols else 1,
            )
        if availability == "Takeout candidate; current hidden slot match is ambiguous":
            availability = reconciled_video_availability(
                video_id,
                "",
                recovered_status,
                row["is_playable"] if "is_playable" in old_cols else 1,
            )
        conn.execute(
            """
            INSERT INTO playlist_video_reconciled(
              playlist_id, display_position, current_position, snapshot_position,
              video_id, title, channel_id, channel, duration_text, is_playable, availability, url,
              source_quality, match_type, match_confidence, snapshot_key, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["playlist_id"],
                row["display_position"],
                row["current_position"] if "current_position" in old_cols else 0,
                row["snapshot_position"] if "snapshot_position" in old_cols else 0,
                video_id,
                row["title"] if "title" in old_cols else "",
                row["channel_id"] if "channel_id" in old_cols else "",
                row["channel"] if "channel" in old_cols else "",
                row["duration_text"] if "duration_text" in old_cols else "",
                row["is_playable"] if "is_playable" in old_cols else 1,
                availability,
                row["url"] if "url" in old_cols else "",
                source_quality,
                match_type,
                row["match_confidence"] if "match_confidence" in old_cols else "",
                snapshot_key,
                row["added_at"] if "added_at" in old_cols else "",
                row["updated_at"] if "updated_at" in old_cols else int(time.time()),
            ),
        )
    conn.execute(f"DROP TABLE {old_table}")


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
    return f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""


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
    fetched_at: int = 0,
    source: str = "",
    updated_at: int | None = None,
) -> str:
    channel_id = (channel_id or "").strip()
    if not channel_id:
        channel_id = youtube_channel_id_from_url(url)
    if not channel_id:
        return ""
    url = url or youtube_channel_url(channel_id)
    now = updated_at or int(time.time())
    existing = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE channels
            SET title = ?,
                url = ?,
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
                source = ?,
                updated_at = ?
            WHERE channel_id = ?
            """,
            (
                merge_channel_value(existing["title"], title),
                merge_channel_value(existing["url"], url),
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
                merge_channel_value(existing["source"], source),
                now,
                channel_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO channels(
              channel_id, title, url, description, aliases, thumbnail_url, thumbnail_path,
              archivarix_channel_id, status, status_reason, fetch_status, fetch_error,
              fetched_at, source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                title,
                url,
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


def backfill_channels(conn: sqlite3.Connection) -> None:
    specs = [
        ("video_metadata", "channel_id", "channel", "channel_url", "channel_thumbnail_url", "channel_thumbnail_path", "", "metadata"),
        ("snapshot_video_recovery", "channel_id", "channel", "channel_url", "channel_thumbnail_url", "channel_thumbnail_path", "archivarix_channel_id", "archivarix"),
        ("playlist_videos", "channel_id", "channel", "", "", "", "", "playlist"),
        ("playlist_video_reconciled", "channel_id", "channel", "", "", "", "", "playlist_reconciled"),
        ("archivarix_candidates", "channel_id", "channel", "", "", "", "", "archivarix_candidate"),
        ("youtube_history_occurrences", "channel_id", "channel", "channel_url", "", "", "", "youtube_history"),
        ("takeout_history_occurrences", "channel_id", "channel", "channel_url", "", "", "", "takeout_history"),
        ("history_reconciled", "channel_id", "channel", "channel_url", "", "", "", "history_reconciled"),
    ]
    for table, channel_id_col, title_col, url_col, thumb_url_col, thumb_path_col, arch_col, source in specs:
        cols = table_columns(conn, table)
        if channel_id_col not in cols:
            continue
        select_cols = ["rowid", channel_id_col]
        for col in (title_col, url_col, thumb_url_col, thumb_path_col, arch_col):
            if col and col in cols and col not in select_cols:
                select_cols.append(col)
        for row in conn.execute(f"SELECT {', '.join(select_cols)} FROM {table}").fetchall():
            title = row[title_col] if title_col in row.keys() else ""
            url = row[url_col] if url_col and url_col in row.keys() else ""
            channel_id = row[channel_id_col] or youtube_channel_id_from_url(url)
            channel_id = upsert_channel(
                conn,
                channel_id,
                title=title or "",
                url=url or "",
                thumbnail_url=(row[thumb_url_col] if thumb_url_col and thumb_url_col in row.keys() else "") or "",
                thumbnail_path=(row[thumb_path_col] if thumb_path_col and thumb_path_col in row.keys() else "") or "",
                archivarix_channel_id=(row[arch_col] if arch_col and arch_col in row.keys() else "") or "",
                source=source,
            )
            if channel_id and row[channel_id_col] != channel_id:
                conn.execute(f"UPDATE {table} SET {channel_id_col} = ? WHERE rowid = ?", (channel_id, row["rowid"]))


def channel_backfill_needed(conn: sqlite3.Connection) -> bool:
    channel_count = conn.execute("SELECT COUNT(*) AS count FROM channels").fetchone()["count"]
    if not channel_count:
        return True
    checks = [
        ("video_metadata", "channel_id", "channel_url"),
        ("snapshot_video_recovery", "channel_id", "channel_url"),
        ("youtube_history_occurrences", "channel_id", "channel_url"),
        ("takeout_history_occurrences", "channel_id", "channel_url"),
        ("history_reconciled", "channel_id", "channel_url"),
    ]
    for table, channel_id_col, url_col in checks:
        cols = table_columns(conn, table)
        if channel_id_col not in cols or url_col not in cols:
            continue
        row = conn.execute(
            f"""
            SELECT 1
            FROM {table}
            WHERE {channel_id_col} = ''
              AND {url_col} LIKE '%/channel/UC%'
            LIMIT 1
            """
        ).fetchone()
        if row:
            return True
    return False


def ensure_channel_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_title ON channels(title COLLATE NOCASE)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_fetch ON channels(fetch_status, fetched_at)")
    indexes = [
        ("idx_snapshot_video_recovery_channel", "snapshot_video_recovery", "channel_id"),
        ("idx_video_metadata_channel", "video_metadata", "channel_id"),
        ("idx_playlist_videos_channel", "playlist_videos", "channel_id"),
        ("idx_playlist_video_reconciled_channel", "playlist_video_reconciled", "channel_id"),
        ("idx_archivarix_candidates_channel", "archivarix_candidates", "channel_id"),
        ("idx_youtube_history_occurrences_channel", "youtube_history_occurrences", "channel_id"),
        ("idx_takeout_history_occurrences_channel", "takeout_history_occurrences", "channel_id"),
        ("idx_history_reconciled_channel", "history_reconciled", "channel_id"),
    ]
    for index_name, table, column in indexes:
        if column in table_columns(conn, table):
            conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({column})")


def backfill_playlist_channel_ids_by_name(conn: sqlite3.Connection) -> None:
    for table in ("playlist_videos", "playlist_video_reconciled", "archivarix_candidates"):
        cols = table_columns(conn, table)
        if "channel_id" not in cols or "channel" not in cols:
            continue
        needs_backfill = conn.execute(
            f"""
            SELECT 1
            FROM {table}
            WHERE channel_id = ''
              AND channel <> ''
            LIMIT 1
            """
        ).fetchone()
        if not needs_backfill:
            continue
        unique_channels: dict[str, str] = {}
        ambiguous: set[str] = set()
        for row in conn.execute("SELECT channel_id, title FROM channels WHERE title <> '' AND channel_id <> ''"):
            key = row["title"].casefold()
            if key in unique_channels and unique_channels[key] != row["channel_id"]:
                ambiguous.add(key)
            else:
                unique_channels[key] = row["channel_id"]
        for key in ambiguous:
            unique_channels.pop(key, None)
        for row in conn.execute(
            f"""
            SELECT rowid, channel
            FROM {table}
            WHERE channel_id = ''
              AND channel <> ''
            """
        ).fetchall():
            channel_id = unique_channels.get(row["channel"].casefold(), "")
            if channel_id:
                conn.execute(f"UPDATE {table} SET channel_id = ? WHERE rowid = ?", (channel_id, row["rowid"]))


def drop_deprecated_channel_columns(conn: sqlite3.Connection) -> None:
    cleanup_video_metadata_columns(conn)
    cleanup_snapshot_video_recovery_columns(conn)


def cleanup_video_metadata_columns(conn: sqlite3.Connection) -> None:
    deprecated = {"channel", "channel_url", "channel_thumbnail_url", "channel_thumbnail_path", "watch_url"}
    cols = table_columns(conn, "video_metadata")
    if "reaction" not in cols:
        conn.execute("ALTER TABLE video_metadata ADD COLUMN reaction TEXT NOT NULL DEFAULT ''")
    for name in ("watch_progress_percent", "watch_resume_seconds"):
        if name not in cols:
            conn.execute(f"ALTER TABLE video_metadata ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
    cols = table_columns(conn, "video_metadata")
    if not deprecated.intersection(cols):
        return
    conn.execute("ALTER TABLE video_metadata RENAME TO video_metadata_old")
    conn.execute(
        """
        CREATE TABLE video_metadata (
          video_id TEXT PRIMARY KEY,
          title TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          channel_id TEXT NOT NULL DEFAULT '',
          duration_text TEXT NOT NULL DEFAULT '',
          view_count TEXT NOT NULL DEFAULT '',
          upload_date TEXT NOT NULL DEFAULT '',
          thumbnail_url TEXT NOT NULL DEFAULT '',
          thumbnail_path TEXT NOT NULL DEFAULT '',
          reaction TEXT NOT NULL DEFAULT '',
          watch_progress_percent INTEGER NOT NULL DEFAULT 0,
          watch_resume_seconds INTEGER NOT NULL DEFAULT 0,
          yt_status TEXT NOT NULL DEFAULT '',
          fetch_status TEXT NOT NULL DEFAULT '',
          fetch_error TEXT NOT NULL DEFAULT '',
          fetched_at INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    old_cols = table_columns(conn, "video_metadata_old")
    select_channel_id = "channel_id" if "channel_id" in old_cols else "''"
    select_reaction = "reaction" if "reaction" in old_cols else "''"
    conn.execute(
        f"""
        INSERT INTO video_metadata(
          video_id, title, description, channel_id, duration_text, view_count,
          upload_date, thumbnail_url, thumbnail_path, reaction, yt_status,
          watch_progress_percent, watch_resume_seconds,
          fetch_status, fetch_error, fetched_at, updated_at
        )
        SELECT video_id, title, description, {select_channel_id}, duration_text, view_count,
               upload_date, thumbnail_url, thumbnail_path, {select_reaction}, yt_status,
               watch_progress_percent, watch_resume_seconds,
               fetch_status, fetch_error, fetched_at, updated_at
        FROM video_metadata_old
        """
    )
    conn.execute("DROP TABLE video_metadata_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_video_metadata_status ON video_metadata(fetch_status, fetched_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_video_metadata_channel ON video_metadata(channel_id)")


def cleanup_snapshot_video_recovery_columns(conn: sqlite3.Connection) -> None:
    deprecated = {"channel", "channel_url", "channel_thumbnail_url", "channel_thumbnail_path"}
    cols = table_columns(conn, "snapshot_video_recovery")
    if not deprecated.intersection(cols):
        return
    conn.execute("ALTER TABLE snapshot_video_recovery RENAME TO snapshot_video_recovery_old")
    conn.execute(
        """
        CREATE TABLE snapshot_video_recovery (
          snapshot_key TEXT NOT NULL,
          video_id TEXT NOT NULL,
          title TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          channel_id TEXT NOT NULL DEFAULT '',
          archivarix_channel_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT '',
          duration_text TEXT NOT NULL DEFAULT '',
          upload_date TEXT NOT NULL DEFAULT '',
          view_count TEXT NOT NULL DEFAULT '',
          thumbnail_url TEXT NOT NULL DEFAULT '',
          thumbnail_path TEXT NOT NULL DEFAULT '',
          archive_url TEXT NOT NULL DEFAULT '',
          video_file_url TEXT NOT NULL DEFAULT '',
          searched_at INTEGER NOT NULL DEFAULT 0,
          search_status TEXT NOT NULL DEFAULT '',
          search_error TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (snapshot_key, video_id)
        )
        """
    )
    old_cols = table_columns(conn, "snapshot_video_recovery_old")
    select_channel_id = "channel_id" if "channel_id" in old_cols else "''"
    select_archivarix_channel_id = "archivarix_channel_id" if "archivarix_channel_id" in old_cols else "''"
    select_description = "description" if "description" in old_cols else "''"
    conn.execute(
        f"""
        INSERT INTO snapshot_video_recovery(
          snapshot_key, video_id, title, description, channel_id, archivarix_channel_id,
          status, duration_text, upload_date, view_count, thumbnail_url, thumbnail_path,
          archive_url, video_file_url, searched_at, search_status, search_error
        )
        SELECT snapshot_key, video_id, title, {select_description}, {select_channel_id}, {select_archivarix_channel_id},
               status, duration_text, upload_date, view_count, thumbnail_url, thumbnail_path,
               archive_url, video_file_url, searched_at, search_status, search_error
        FROM snapshot_video_recovery_old
        """
    )
    conn.execute("DROP TABLE snapshot_video_recovery_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_video_recovery_status ON snapshot_video_recovery(snapshot_key, search_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_video_recovery_channel ON snapshot_video_recovery(channel_id)")


def current_iso_timestamp() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def iso_from_unix(value: int) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value).astimezone().replace(microsecond=0).isoformat()


def is_iso_datetime(value: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}T", value or ""))


def normalize_youtube_history_observed_at(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT rowid, observed_at, imported_at
        FROM youtube_history_occurrences
        WHERE observed_at = ''
           OR observed_at NOT GLOB '????-??-??T*'
        """
    ).fetchall()
    for row in rows:
        observed_at = iso_from_unix(row["imported_at"]) or current_iso_timestamp()
        conn.execute(
            "UPDATE youtube_history_occurrences SET observed_at = ? WHERE rowid = ?",
            (observed_at, row["rowid"]),
        )


def ensure_youtube_history_schema(conn: sqlite3.Connection) -> None:
    expected = {
        "ordinal",
        "video_id",
        "title",
        "url",
        "channel_id",
        "channel",
        "watch_date",
        "watch_progress_percent",
        "watch_resume_seconds",
        "observed_at",
        "imported_at",
        "updated_at",
    }
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(youtube_history_occurrences)")}
    if existing == expected:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_video ON youtube_history_occurrences(video_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_search ON youtube_history_occurrences(title, channel, ordinal)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_date ON youtube_history_occurrences(watch_date, video_id)")
        normalize_youtube_history_observed_at(conn)
        return
    old_table = begin_table_rebuild(conn, "youtube_history_occurrences")
    conn.execute(
        """
        CREATE TABLE youtube_history_occurrences (
          ordinal INTEGER NOT NULL,
          video_id TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          channel_id TEXT NOT NULL DEFAULT '',
          channel TEXT NOT NULL DEFAULT '',
          watch_date TEXT NOT NULL DEFAULT '',
          watch_progress_percent INTEGER NOT NULL DEFAULT 0,
          watch_resume_seconds INTEGER NOT NULL DEFAULT 0,
          observed_at TEXT NOT NULL DEFAULT '',
          imported_at INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (ordinal)
        )
        """
    )
    old_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({old_table})")}
    if old_cols:
        for row in conn.execute(f"SELECT * FROM {old_table}").fetchall():
            imported_at = row["imported_at"] if "imported_at" in old_cols else 0
            observed_at = row["observed_at"] if "observed_at" in old_cols else ""
            if not is_iso_datetime(observed_at):
                observed_at = iso_from_unix(imported_at) or current_iso_timestamp()
            channel_url = row["channel_url"] if "channel_url" in old_cols else ""
            channel_id = row["channel_id"] if "channel_id" in old_cols else ""
            channel_id = upsert_channel(
                conn,
                channel_id or youtube_channel_id_from_url(channel_url),
                title=row["channel"] if "channel" in old_cols else "",
                url=channel_url,
                source="youtube_history",
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO youtube_history_occurrences(
                  ordinal, video_id, title, url, channel_id, channel,
                  watch_date, watch_progress_percent, watch_resume_seconds,
                  observed_at, imported_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["ordinal"] if "ordinal" in old_cols else 0,
                    row["video_id"] if "video_id" in old_cols else "",
                    row["title"] if "title" in old_cols else "",
                    row["url"] if "url" in old_cols else "",
                    channel_id,
                    row["channel"] if "channel" in old_cols else "",
                    row["watch_date"] if "watch_date" in old_cols else "",
                    row["watch_progress_percent"] if "watch_progress_percent" in old_cols else 0,
                    row["watch_resume_seconds"] if "watch_resume_seconds" in old_cols else 0,
                    observed_at,
                    imported_at,
                    row["updated_at"] if "updated_at" in old_cols else imported_at,
                ),
            )
    conn.execute(f"DROP TABLE {old_table}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_video ON youtube_history_occurrences(video_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_search ON youtube_history_occurrences(title, channel, ordinal)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_date ON youtube_history_occurrences(watch_date, video_id)")
    normalize_youtube_history_observed_at(conn)


def drop_legacy_history_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS watch_history")
    conn.execute("DROP TABLE IF EXISTS search_history")


def ensure_takeout_history_schema(conn: sqlite3.Connection) -> None:
    expected = {
        "history_key",
        "row_hash",
        "video_id",
        "title",
        "url",
        "channel_id",
        "channel",
        "watched_at_iso",
    }
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(takeout_history_occurrences)")}
    if existing == expected:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_video ON takeout_history_occurrences(video_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_time ON takeout_history_occurrences(watched_at_iso, video_id)"
        )
        return
    old_table = begin_table_rebuild(conn, "takeout_history_occurrences")
    conn.execute(
        """
        CREATE TABLE takeout_history_occurrences (
          history_key TEXT NOT NULL,
          row_hash TEXT NOT NULL DEFAULT '',
          video_id TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          channel_id TEXT NOT NULL DEFAULT '',
          channel TEXT NOT NULL DEFAULT '',
          watched_at_iso TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (history_key, row_hash)
        )
        """
    )
    old_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({old_table})")}
    for row in conn.execute(f"SELECT * FROM {old_table}").fetchall():
        channel_url = row["channel_url"] if "channel_url" in old_cols else ""
        channel_id = row["channel_id"] if "channel_id" in old_cols else ""
        channel_id = upsert_channel(
            conn,
            channel_id or youtube_channel_id_from_url(channel_url),
            title=row["channel"] if "channel" in old_cols else "",
            url=channel_url,
            source="takeout_history",
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO takeout_history_occurrences(
              history_key, row_hash, video_id, title, url, channel_id, channel, watched_at_iso
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["history_key"] if "history_key" in old_cols else "",
                row["row_hash"] if "row_hash" in old_cols else "",
                row["video_id"] if "video_id" in old_cols else "",
                row["title"] if "title" in old_cols else "",
                row["url"] if "url" in old_cols else "",
                channel_id,
                row["channel"] if "channel" in old_cols else "",
                row["watched_at_iso"] if "watched_at_iso" in old_cols else "",
            ),
        )
    conn.execute(f"DROP TABLE {old_table}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_video ON takeout_history_occurrences(video_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_time ON takeout_history_occurrences(watched_at_iso, video_id)"
    )


def ensure_history_reconciled_schema(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(history_reconciled)")}
    if (
        "takeout_position" not in existing
        and "takeout_row_hash" in existing
        and "channel_url" not in existing
        and "watch_progress_percent" in existing
        and "watch_resume_seconds" in existing
    ):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_reconciled_video ON history_reconciled(video_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_reconciled_channel ON history_reconciled(channel_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_reconciled_date ON history_reconciled(watch_date, source_quality)")
        return
    old_table = begin_table_rebuild(conn, "history_reconciled")
    conn.execute(
        """
        CREATE TABLE history_reconciled (
          reconciled_id TEXT PRIMARY KEY,
          video_id TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          channel_id TEXT NOT NULL DEFAULT '',
          channel TEXT NOT NULL DEFAULT '',
          best_watch_time TEXT NOT NULL DEFAULT '',
          watch_date TEXT NOT NULL DEFAULT '',
          source_quality TEXT NOT NULL DEFAULT '',
          youtube_history_key TEXT NOT NULL DEFAULT '',
          youtube_ordinal INTEGER NOT NULL DEFAULT 0,
          takeout_history_key TEXT NOT NULL DEFAULT '',
          takeout_row_hash TEXT NOT NULL DEFAULT '',
          match_confidence TEXT NOT NULL DEFAULT '',
          match_notes TEXT NOT NULL DEFAULT '',
          watch_progress_percent INTEGER NOT NULL DEFAULT 0,
          watch_resume_seconds INTEGER NOT NULL DEFAULT 0,
          imported_at INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    old_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({old_table})")}
    for row in conn.execute(f"SELECT * FROM {old_table}").fetchall():
        channel_url = row["channel_url"] if "channel_url" in old_cols else ""
        channel_id = row["channel_id"] if "channel_id" in old_cols else ""
        channel_id = upsert_channel(
            conn,
            channel_id or youtube_channel_id_from_url(channel_url),
            title=row["channel"] if "channel" in old_cols else "",
            url=channel_url,
            source="history_reconciled",
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO history_reconciled(
              reconciled_id, video_id, title, url, channel_id, channel,
              best_watch_time, watch_date, source_quality,
              youtube_history_key, youtube_ordinal, takeout_history_key, takeout_row_hash,
              match_confidence, match_notes, watch_progress_percent, watch_resume_seconds,
              imported_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["reconciled_id"] if "reconciled_id" in old_cols else "",
                row["video_id"] if "video_id" in old_cols else "",
                row["title"] if "title" in old_cols else "",
                row["url"] if "url" in old_cols else "",
                channel_id,
                row["channel"] if "channel" in old_cols else "",
                row["best_watch_time"] if "best_watch_time" in old_cols else "",
                row["watch_date"] if "watch_date" in old_cols else "",
                row["source_quality"] if "source_quality" in old_cols else "",
                row["youtube_history_key"] if "youtube_history_key" in old_cols else "",
                row["youtube_ordinal"] if "youtube_ordinal" in old_cols else 0,
                row["takeout_history_key"] if "takeout_history_key" in old_cols else "",
                row["takeout_row_hash"] if "takeout_row_hash" in old_cols else "",
                row["match_confidence"] if "match_confidence" in old_cols else "",
                row["match_notes"] if "match_notes" in old_cols else "",
                row["watch_progress_percent"] if "watch_progress_percent" in old_cols else 0,
                row["watch_resume_seconds"] if "watch_resume_seconds" in old_cols else 0,
                row["imported_at"] if "imported_at" in old_cols else 0,
                row["updated_at"] if "updated_at" in old_cols else 0,
            ),
        )
    conn.execute(f"DROP TABLE {old_table}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_reconciled_video ON history_reconciled(video_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_reconciled_channel ON history_reconciled(channel_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_reconciled_date ON history_reconciled(watch_date, source_quality)")


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


def parse_playlist_lockup(lockup: dict[str, Any]) -> dict[str, str] | None:
    playlist_id = lockup.get("contentId")
    if not isinstance(playlist_id, str) or not playlist_id:
        return None
    if lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_PLAYLIST":
        return None

    title = content_text(
        lockup.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title")
    ).strip()
    rows = lockup_metadata_rows(lockup)
    privacy = ""
    video_count_text = ""
    updated_text = ""
    for parts in rows:
        joined = " • ".join(parts)
        if "Playlist" in parts and parts:
            privacy = parts[0]
        if re.search(r"\bvideos?\b", joined, re.I):
            video_count_text = joined
        if joined.lower().startswith("updated"):
            updated_text = joined
    if not video_count_text:
        for node in walk(lockup):
            if not isinstance(node, dict):
                continue
            badge = node.get("thumbnailBadgeViewModel")
            if isinstance(badge, dict):
                text = badge.get("text")
                if isinstance(text, str) and re.search(r"\bvideos?\b", text, re.I):
                    video_count_text = text
                    break

    return {
        "playlist_id": playlist_id,
        "title": title or playlist_id,
        "description": updated_text,
        "owner": privacy,
        "video_count_text": video_count_text,
        "thumbnail_url": pick_lockup_thumbnail(lockup),
        "url": f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}",
    }


def extract_playlist_metadata(html_text: str, playlist_id: str) -> dict[str, str]:
    initial_data = extract_json_assignment(html_text, "ytInitialData")
    metadata = {
        "playlist_id": playlist_id,
        "title": "",
        "description": "",
        "owner": "",
        "video_count_text": "",
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
        owner = text_from_runs(renderer.get("ownerText") or renderer.get("subtitle"))
        if owner and not metadata["owner"]:
            metadata["owner"] = owner
        for key in ("numVideosText", "numVideosTextText", "videoCountText"):
            count_text = text_from_runs(renderer.get(key))
            if count_text and not metadata["video_count_text"]:
                metadata["video_count_text"] = count_text
        thumbnail = renderer.get("playlistHeaderBanner")
        if isinstance(thumbnail, dict):
            thumbs = thumbnail.get("heroPlaylistThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])
            if thumbs and not metadata["thumbnail_url"]:
                metadata["thumbnail_url"] = pick_thumbnail(thumbs)

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
    return str(path.resolve().relative_to(ROOT)).replace("\\", "/")


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
) -> str:
    if not subject_id or not thumbnail_url:
        return ""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    try:
        body, content_type = request_bytes(
            opener,
            thumbnail_url,
            timeout=30,
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


def archivarix_lookup_video(
    video_id: str,
    opener: urllib.request.OpenerDirector | None = None,
    channel_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
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
    with opener.open(request, timeout=20) as response:
        session = json.loads(response.read().decode("utf-8", "replace")).get("data", {})
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
        try:
            body, content_type = request_bytes(
                opener,
                source,
                timeout=12,
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


def is_hidden_video(renderer: dict[str, Any], title: str, reason: str) -> bool:
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
    hidden = is_hidden_video(renderer, title, reason)
    return {
        "playlist_id": playlist_id,
        "position": position,
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "duration_text": duration,
        "is_playable": 0 if hidden else 1,
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
    hidden = is_hidden_video(renderer, title, reason)
    return {
        "playlist_id": playlist_id,
        "position": position,
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "duration_text": duration,
        "is_playable": 0 if hidden else 1,
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


def pacific_offset_for_date(value: date) -> str:
    march = date(value.year, 3, 1)
    second_sunday_march = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    november = date(value.year, 11, 1)
    first_sunday_november = november + timedelta(days=(6 - november.weekday()) % 7)
    return "-07:00" if second_sunday_march <= value < first_sunday_november else "-08:00"


def youtube_watch_datetime(watch_date: str) -> str:
    try:
        parsed = date.fromisoformat(watch_date)
    except ValueError:
        return watch_date
    return f"{watch_date}T00:00:00{pacific_offset_for_date(parsed)}"


def pacific_date_for_iso_instant(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value[:10]
    if parsed.tzinfo is None:
        return parsed.date().isoformat()
    utc_value = parsed.astimezone(timezone.utc)
    fallback_local_date = (utc_value + timedelta(hours=-8)).date()
    offset = pacific_offset_for_date(fallback_local_date)
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = (int(part) for part in offset[1:].split(":", 1))
    local_value = utc_value + sign * timedelta(hours=hours, minutes=minutes)
    return local_value.date().isoformat()


def takeout_watch_datetime(watched_at: str) -> str:
    cleaned = re.sub(r"\s+", " ", watched_at).strip()
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$", cleaned)
    if iso_match:
        offset = "+00:00" if iso_match.group(3) == "Z" else iso_match.group(3)
        return f"{iso_match.group(1)}T{iso_match.group(2)}{offset}"
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
            return parsed.strftime("%Y-%m-%dT%H:%M:%S") + offset
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


def fetch_youtube_history_web(cookie_file: Path, limit: int = 100, start: int = 1) -> list[dict[str, Any]]:
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
    today = date.today()

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


def expected_video_count(text: str) -> int:
    found = re.search(r"([\d,]+)\s+videos?", text or "", re.I)
    if not found:
        return 0
    return int(found.group(1).replace(",", ""))


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
) -> dict[str, str]:
    channel_url = youtube_channel_url(channel_id)
    page = request_text(opener, channel_url)
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
    if reason and status and reason not in status:
        status = f"{status}: {reason}"
    return {
        "video_id": video_id,
        "title": title,
        "description": str(details.get("shortDescription") or "").strip(),
        "channel_id": channel_id,
        "channel": str(details.get("author") or "").strip(),
        "channel_url": channel_url,
        "duration_text": format_duration(details.get("lengthSeconds")),
        "view_count": str(details.get("viewCount") or ""),
        "upload_date": str(microformat.get("uploadDate") or microformat.get("publishDate") or ""),
        "thumbnail_url": thumbnail_url,
        "channel_thumbnail_url": channel_thumbnail_url,
        "reaction": reaction,
        "watch_progress_percent": str(watch_progress_percent),
        "watch_resume_seconds": str(watch_resume_seconds),
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
) -> dict[str, str]:
    watch_url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    page = request_text(opener, watch_url)
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
    updated_at: int | None = None,
) -> str:
    now = updated_at or int(time.time())
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
    conn.execute(
        """
        INSERT INTO video_metadata(
          video_id, title, description, channel_id, duration_text, view_count,
          upload_date, thumbnail_url, thumbnail_path,
          reaction, watch_progress_percent, watch_resume_seconds,
          yt_status, fetch_status, fetch_error, fetched_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
          title=excluded.title,
          description=excluded.description,
          channel_id=excluded.channel_id,
          duration_text=excluded.duration_text,
          view_count=excluded.view_count,
          upload_date=excluded.upload_date,
          thumbnail_url=excluded.thumbnail_url,
          thumbnail_path=excluded.thumbnail_path,
          reaction=excluded.reaction,
          watch_progress_percent=excluded.watch_progress_percent,
          watch_resume_seconds=excluded.watch_resume_seconds,
          yt_status=excluded.yt_status,
          fetch_status=excluded.fetch_status,
          fetch_error=excluded.fetch_error,
          fetched_at=excluded.fetched_at,
          updated_at=excluded.updated_at
        """,
        (
            metadata.get("video_id", ""),
            metadata.get("title", ""),
            metadata.get("description", ""),
            channel_id,
            metadata.get("duration_text", ""),
            metadata.get("view_count", ""),
            metadata.get("upload_date", ""),
            metadata.get("thumbnail_url", ""),
            metadata.get("thumbnail_path", ""),
            metadata.get("reaction", ""),
            bounded_int(metadata.get("watch_progress_percent")),
            max(0, int(metadata.get("watch_resume_seconds") or 0)),
            metadata.get("yt_status", ""),
            status,
            error,
            now,
            now,
        ),
    )
    return channel_id


def useful_video_metadata(metadata: dict[str, str]) -> bool:
    title = (metadata.get("title") or "").strip()
    yt_status = (metadata.get("yt_status") or "").strip().upper()
    if title in {"", "YouTube", "- YouTube"}:
        return False
    if yt_status.startswith("ERROR") and not metadata.get("channel_id"):
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
    updated_at: int | None = None,
) -> str:
    now = updated_at or int(time.time())
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


def fetch_provided_metadata(
    conn: sqlite3.Connection,
    opener: urllib.request.OpenerDirector,
    thumb_dir: Path,
    target: str,
) -> dict[str, str]:
    source, subject_id = resolve_metadata_target(opener, target)
    if not source or not subject_id:
        raise ValueError("Enter a YouTube watch URL, video ID, channel URL, channel ID, or @handle.")
    now = int(time.time())
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
            for row in conn.execute(
                "SELECT DISTINCT snapshot_key FROM snapshot_videos WHERE video_id = ?",
                (subject_id,),
            ).fetchall():
                save_snapshot_video_recovery(
                    conn,
                    row["snapshot_key"],
                    subject_id,
                    video,
                    thumbnail_url,
                    thumbnail_path,
                    arch_status,
                    arch_error,
                )
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
        hidden = availability.lower() in {"private", "needs_auth", "premium_only", "subscriber_only"}
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
    return videos


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
) -> list[dict[str, Any]]:
    playlist_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
    page = request_text(opener, playlist_url)
    initial_data = extract_json_assignment(page, "ytInitialData")
    config = extract_ytcfg(page)
    pages = [initial_data]
    token = continuation_token(initial_data)

    api_key = config.get("INNERTUBE_API_KEY", "")
    client_name = config.get("INNERTUBE_CLIENT_NAME", "WEB")
    client_version = config.get("INNERTUBE_CLIENT_VERSION", "")
    visitor_data = config.get("VISITOR_DATA", "")
    seen_tokens: set[str] = set()
    while token and token not in seen_tokens and api_key and client_version:
        seen_tokens.add(token)
        payload = {
            "context": {
                "client": {
                    "clientName": client_name,
                    "clientVersion": client_version,
                    "visitorData": visitor_data,
                }
            },
            "continuation": token,
        }
        data = request_json(
            opener,
            f"https://www.youtube.com/youtubei/v1/browse?key={urllib.parse.quote(api_key)}",
            payload,
            playlist_url,
        )
        pages.append(data)
        token = continuation_token(data)

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

    if not videos:
        for lockup in video_lockup_renderers(initial_data):
            fallback = len(videos) + 1
            add_video(parse_video_lockup(playlist_id, lockup, fallback))

    if not videos:
        for renderer in shorts_lockup_renderers(initial_data):
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
                "video_count_text": "",
                "thumbnail_url": "",
                "url": url,
            }
            thumbnail_path = ""
            status = "error"
            error = str(exc)
        with conn:
            conn.execute(
                """
                INSERT INTO playlists(
                  playlist_id, title, description, owner, video_count_text,
                  thumbnail_url, thumbnail_path, url, fetch_status, fetch_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                  title=excluded.title,
                  description=excluded.description,
                  owner=excluded.owner,
                  video_count_text=excluded.video_count_text,
                  thumbnail_url=excluded.thumbnail_url,
                  thumbnail_path=excluded.thumbnail_path,
                  url=excluded.url,
                  fetch_status=excluded.fetch_status,
                  fetch_error=excluded.fetch_error,
                  updated_at=excluded.updated_at
                """,
                (
                    playlist_id,
                    metadata["title"],
                    metadata["description"],
                    metadata["owner"],
                    metadata["video_count_text"],
                    metadata["thumbnail_url"],
                    thumbnail_path,
                    metadata["url"],
                    status,
                    error,
                    int(time.time()),
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
            conn.execute(
                """
                INSERT INTO playlists(
                  playlist_id, title, description, owner, video_count_text,
                  thumbnail_url, thumbnail_path, url, fetch_status, fetch_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', '', ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                  title=excluded.title,
                  description=excluded.description,
                  owner=excluded.owner,
                  video_count_text=excluded.video_count_text,
                  thumbnail_url=excluded.thumbnail_url,
                  thumbnail_path=excluded.thumbnail_path,
                  url=excluded.url,
                  fetch_status='ok',
                  fetch_error='',
                  updated_at=excluded.updated_at
                """,
                (
                    playlist_id,
                    record["title"],
                    record["description"],
                    record["owner"],
                    record["video_count_text"],
                    record["thumbnail_url"],
                    thumbnail_path,
                    record["url"],
                    int(time.time()),
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
        (run_id, int(time.time()), level, video_id, message),
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
        (run_id, int(time.time()), level, playlist_id, message),
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
        (run_id, int(time.time()), level, video_id, message),
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
        (run_id, int(time.time()), level, video_id, message),
    )


def playlist_placeholder_recovery_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
) -> list[sqlite3.Row]:
    where = [
        "v.video_id <> ''",
        """
        (
          v.is_playable = 0
          OR lower(trim(COALESCE(v.title, ''), '[]() ')) IN ('deleted video', 'private video')
          OR lower(COALESCE(v.title, '')) LIKE '%unavailable%'
          OR lower(COALESCE(v.availability, '')) LIKE '%unavailable%'
          OR lower(COALESCE(v.availability, '')) LIKE '%deleted%'
          OR lower(COALESCE(v.availability, '')) LIKE '%private%'
        )
        """,
    ]
    if not force:
        where.append("(r.video_id IS NULL OR r.search_status = 'error')")
    sql = f"""
        SELECT v.snapshot_key,
               v.video_id,
               MIN(v.display_position) AS display_position,
               MIN(p.title) AS playlist_title,
               COUNT(DISTINCT v.playlist_id) AS playlist_count,
               COALESCE(r.search_status, '') AS previous_status
        FROM playlist_video_reconciled v
        JOIN playlists p ON p.playlist_id = v.playlist_id
        LEFT JOIN snapshot_video_recovery r
          ON r.snapshot_key = v.snapshot_key
         AND r.video_id = v.video_id
        WHERE {" AND ".join(where)}
        GROUP BY v.snapshot_key, v.video_id, COALESCE(r.search_status, '')
        ORDER BY MIN(p.title) COLLATE NOCASE, MIN(v.display_position), v.video_id
    """
    params: list[Any] = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def playlist_placeholder_recovery_count(conn: sqlite3.Connection, force: bool = False) -> int:
    return len(playlist_placeholder_recovery_rows(conn, limit=0, force=force))


def save_snapshot_video_recovery(
    conn: sqlite3.Connection,
    snapshot_key: str,
    video_id: str,
    video: dict[str, Any] | None,
    thumbnail_url: str,
    thumbnail_path: str,
    search_status: str,
    search_error: str,
) -> None:
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
    conn.execute(
        """
        INSERT INTO snapshot_video_recovery(
          snapshot_key, video_id, title, description, channel_id, archivarix_channel_id, status, duration_text,
          upload_date, view_count, thumbnail_url, thumbnail_path,
          archive_url, video_file_url, searched_at, search_status, search_error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_key, video_id) DO UPDATE SET
          title=excluded.title,
          description=excluded.description,
          channel_id=excluded.channel_id,
          archivarix_channel_id=excluded.archivarix_channel_id,
          status=excluded.status,
          duration_text=excluded.duration_text,
          upload_date=excluded.upload_date,
          view_count=excluded.view_count,
          thumbnail_url=excluded.thumbnail_url,
          thumbnail_path=excluded.thumbnail_path,
          archive_url=excluded.archive_url,
          video_file_url=excluded.video_file_url,
          searched_at=excluded.searched_at,
          search_status=excluded.search_status,
          search_error=excluded.search_error
        """,
        (
            snapshot_key,
            video_id,
            (video or {}).get("title") or "",
            (video or {}).get("description") or "",
            channel_id,
            archivarix_channel_id if not archivarix_channel_id.startswith("UC") else "",
            recovered_status,
            format_duration((video or {}).get("duration")),
            (video or {}).get("uploadDate") or "",
            str((video or {}).get("viewCount") or ""),
            thumbnail_url,
            thumbnail_path,
            (video or {}).get("archiveUrl") or "",
            (video or {}).get("videoFileUrl") or "",
            int(time.time()),
            search_status,
            search_error,
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
) -> tuple[dict[str, Any] | None, str, str, str, str]:
    status = "not_found"
    error = ""
    video: dict[str, Any] | None = None
    thumbnail_url = ""
    thumbnail_path = cache_archivarix_thumbnail(
        video_id,
        "",
        thumb_dir,
        archivarix_opener,
    )
    if thumbnail_path and not refresh_metadata:
        status = "thumbnail_only"
    elif not no_api:
        try:
            if delay:
                time.sleep(delay)
            video = archivarix_lookup_video(video_id, archivarix_opener, channel_cache=channel_cache)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            status = "error"
            error = str(exc)
    if video:
        status = "found"
        thumbnail_url = video.get("thumbnailArchiveUrl") or video.get("thumbnailUrl") or ""
        thumbnail_path = thumbnail_path or cache_archivarix_thumbnail(
            video_id,
            thumbnail_url,
            thumb_dir,
            archivarix_opener,
        )
        channel_thumbnail_url = video.get("channelThumbnailUrl") or ""
        if channel_thumbnail_url:
            video["channelThumbnailPath"] = cache_channel_thumbnail(
                archivarix_opener,
                video_id,
                channel_thumbnail_url,
                thumb_dir,
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
            FROM youtube_history_occurrences
            WHERE ordinal >= ?
              AND ordinal < ?
            ORDER BY ordinal
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


def save_youtube_history_occurrences(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    start: int,
) -> tuple[int, int, str]:
    now = int(time.time())
    observed_at = current_iso_timestamp()
    inserted = 0
    existing = 0
    last_video_id = ""
    for index, row in enumerate(rows, start=start):
        video_id = row.get("video_id") or ""
        if not video_id:
            continue
        previous = conn.execute(
            """
            SELECT video_id
            FROM youtube_history_occurrences
            WHERE ordinal = ?
            """,
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
        )
        conn.execute(
            """
            INSERT INTO youtube_history_occurrences(
              ordinal, video_id, title, url, channel_id, channel,
              watch_date, watch_progress_percent, watch_resume_seconds,
              observed_at, imported_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ordinal) DO UPDATE SET
              video_id=excluded.video_id,
              title=excluded.title,
              url=excluded.url,
              channel_id=excluded.channel_id,
              channel=excluded.channel,
              watch_date=excluded.watch_date,
              watch_progress_percent=excluded.watch_progress_percent,
              watch_resume_seconds=excluded.watch_resume_seconds,
              observed_at=excluded.observed_at,
              updated_at=excluded.updated_at
            """,
            (
                index,
                video_id,
                row.get("title") or video_id,
                row.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                channel_id,
                row.get("channel") or "",
                row.get("watch_date") or "",
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
        row.get(key, "")
        for key in ("video_id", "title", "url", "channel", "channel_url", "watched_at")
    )
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()


def rebuild_history_reconciliation(conn: sqlite3.Connection) -> dict[str, int]:
    now = int(time.time())
    youtube_rows = conn.execute(
        """
        SELECT *
        FROM youtube_history_occurrences
        WHERE video_id <> ''
        ORDER BY ordinal
        """
    ).fetchall()
    youtube_by_ordinal = {row["ordinal"]: row for row in youtube_rows}
    takeout_rows = conn.execute(
        """
        SELECT *
        FROM takeout_history_occurrences
        WHERE video_id <> ''
        ORDER BY history_key, watched_at_iso DESC, row_hash
        """
    ).fetchall()

    takeout_by_video_date: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in takeout_rows:
        match_date = pacific_date_for_iso_instant(row["watched_at_iso"])
        takeout_by_video_date.setdefault((row["video_id"], match_date), []).append(row)

    matched_youtube: dict[int, sqlite3.Row] = {}
    takeout_to_youtube: dict[tuple[str, str], int] = {}
    matched_takeout: set[tuple[str, str]] = set()
    for youtube in youtube_rows:
        key = (youtube["video_id"], youtube["watch_date"])
        if not key[0] or not key[1]:
            continue
        candidates = takeout_by_video_date.get(key, [])
        for takeout in candidates:
            takeout_key = (takeout["history_key"], takeout["row_hash"])
            if takeout_key in matched_takeout:
                continue
            matched_takeout.add(takeout_key)
            youtube_key = youtube["ordinal"]
            matched_youtube[youtube_key] = takeout
            takeout_to_youtube[takeout_key] = youtube_key
            break

    conn.execute("DELETE FROM history_reconciled")
    inserted = 0
    matched = 0
    for takeout in takeout_rows:
        takeout_key = (takeout["history_key"], takeout["row_hash"])
        youtube_match = takeout_to_youtube.get(takeout_key)
        youtube_progress = 0
        youtube_resume = 0
        if youtube_match:
            youtube_row = youtube_by_ordinal.get(youtube_match)
            if youtube_row:
                youtube_progress = youtube_row["watch_progress_percent"] if "watch_progress_percent" in youtube_row.keys() else 0
                youtube_resume = youtube_row["watch_resume_seconds"] if "watch_resume_seconds" in youtube_row.keys() else 0
        source_quality = "matched" if youtube_match else "takeout_exact"
        if youtube_match:
            matched += 1
        conn.execute(
            """
            INSERT INTO history_reconciled(
              reconciled_id, video_id, title, url, channel_id, channel,
              best_watch_time, watch_date, source_quality,
              youtube_history_key, youtube_ordinal, takeout_history_key, takeout_row_hash,
              match_confidence, match_notes, watch_progress_percent, watch_resume_seconds,
              imported_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"takeout:{takeout['history_key']}:{takeout['row_hash']}",
                takeout["video_id"],
                takeout["title"],
                takeout["url"],
                takeout["channel_id"],
                takeout["channel"],
                takeout["watched_at_iso"],
                takeout["watched_at_iso"][:10],
                source_quality,
                "youtube" if youtube_match else "",
                youtube_match if youtube_match else 0,
                takeout["history_key"],
                takeout["row_hash"],
                "video_id_date" if youtube_match else "takeout_only",
                "same video_id and watch_date" if youtube_match else "",
                youtube_progress,
                youtube_resume,
                now,
                now,
            ),
        )
        inserted += 1

    for youtube in youtube_rows:
        youtube_key = youtube["ordinal"]
        if youtube_key in matched_youtube:
            continue
        youtube_watch_time = youtube_watch_datetime(youtube["watch_date"]) if youtube["watch_date"] else ""
        youtube_source_quality = "youtube_date_only" if youtube["watch_date"] else "youtube_observed_only"
        youtube_match_confidence = "youtube_only" if youtube["watch_date"] else "observed_only"
        youtube_match_notes = "" if youtube["watch_date"] else "YouTube history entry had no watch date; observed_at is fetch time"
        conn.execute(
            """
            INSERT INTO history_reconciled(
              reconciled_id, video_id, title, url, channel_id, channel,
              best_watch_time, watch_date, source_quality,
                youtube_history_key, youtube_ordinal, takeout_history_key, takeout_row_hash,
                match_confidence, match_notes, watch_progress_percent, watch_resume_seconds,
                imported_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?)
            """,
            (
                f"youtube:{youtube['ordinal']}",
                youtube["video_id"],
                youtube["title"],
                youtube["url"],
                youtube["channel_id"],
                youtube["channel"],
                youtube_watch_time,
                youtube["watch_date"],
                youtube_source_quality,
                "youtube",
                youtube["ordinal"],
                youtube_match_confidence,
                youtube_match_notes,
                youtube["watch_progress_percent"] if "watch_progress_percent" in youtube.keys() else 0,
                youtube["watch_resume_seconds"] if "watch_resume_seconds" in youtube.keys() else 0,
                youtube["imported_at"],
                now,
            ),
        )
        inserted += 1

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
    hidden_count = sum(1 for video in videos if not video["is_playable"])
    now = int(time.time())
    conn.execute("DELETE FROM playlist_videos WHERE playlist_id = ?", (playlist_id,))
    for video in videos:
        channel_id = upsert_channel(
            conn,
            video.get("channel_id") or "",
            title=video.get("channel") or "",
            source="playlist",
        )
        conn.execute(
            """
            INSERT INTO playlist_videos(
              playlist_id, position, video_id, title, channel_id, channel, duration_text,
              is_playable, availability, url, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video["playlist_id"],
                video["position"],
                video["video_id"],
                video["title"],
                channel_id,
                video["channel"],
                video["duration_text"],
                video["is_playable"],
                video["availability"],
                video["url"],
                now,
            ),
        )
    conn.execute(
        """
        INSERT INTO playlist_scans(
          playlist_id, scanned_at, video_count, hidden_count, scan_status, scan_error
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(playlist_id) DO UPDATE SET
          scanned_at=excluded.scanned_at,
          video_count=excluded.video_count,
          hidden_count=excluded.hidden_count,
          scan_status=excluded.scan_status,
          scan_error=excluded.scan_error
        """,
        (playlist_id, now, len(videos), hidden_count, status, error),
    )
    rebuild_playlist_reconciliation(conn, playlist_id)
    return len(videos), hidden_count


def latest_snapshot_key_for_playlist(conn: sqlite3.Connection, playlist_id: str) -> str:
    row = conn.execute(
        """
        SELECT sv.snapshot_key
        FROM snapshot_videos sv
        JOIN snapshots s ON s.snapshot_key = sv.snapshot_key
        WHERE sv.playlist_id = ?
        GROUP BY sv.snapshot_key
        ORDER BY s.imported_at DESC, sv.snapshot_key DESC
        LIMIT 1
        """,
        (playlist_id,),
    ).fetchone()
    return row["snapshot_key"] if row else ""


def rebuild_playlist_reconciliation(
    conn: sqlite3.Connection,
    playlist_id: str | None = None,
) -> dict[str, int]:
    now = int(time.time())
    if playlist_id:
        playlist_ids = [playlist_id]
    else:
        playlist_ids = [
            row["playlist_id"]
            for row in conn.execute(
                """
                SELECT playlist_id
                FROM playlists
                ORDER BY playlist_id
                """
            )
        ]

    total_rows = 0
    inferred = 0
    ambiguous = 0
    for pid in playlist_ids:
        conn.execute("DELETE FROM playlist_video_reconciled WHERE playlist_id = ?", (pid,))
        current_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM playlist_videos
                WHERE playlist_id = ?
                ORDER BY position
                """,
                (pid,),
            )
        ]
        snapshot_key = latest_snapshot_key_for_playlist(conn, pid)
        snapshot_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM snapshot_videos
                WHERE playlist_id = ? AND snapshot_key = ?
                ORDER BY position
                """,
                (pid, snapshot_key),
            )
        ] if snapshot_key else []
        snapshot_by_video_id = {
            row["video_id"]: row
            for row in snapshot_rows
            if row.get("video_id")
        }

        current_ids = {row["video_id"] for row in current_rows if row.get("video_id")}
        hidden_rows = [row for row in current_rows if not row.get("video_id") or not row.get("is_playable")]
        missing_snapshot_rows = [
            row for row in snapshot_rows if row.get("video_id") and row["video_id"] not in current_ids
        ]
        assign_hidden = bool(hidden_rows and len(hidden_rows) == len(missing_snapshot_rows))
        hidden_assignments = {
            hidden["position"]: snap
            for hidden, snap in zip(hidden_rows, missing_snapshot_rows)
        } if assign_hidden else {}

        for current in current_rows:
            assigned = hidden_assignments.get(current["position"])
            if assigned:
                recovery = conn.execute(
                    """
                    SELECT *
                    FROM snapshot_video_recovery
                    WHERE snapshot_key = ? AND video_id = ?
                    """,
                    (assigned["snapshot_key"], assigned["video_id"]),
                ).fetchone()
                title = (recovery["title"] if recovery else "") or assigned["video_id"]
                channel_id = recovery["channel_id"] if recovery else ""
                channel = channel_title_for_id(conn, channel_id)
                duration = recovery["duration_text"] if recovery else ""
                recovered_status = recovery["status"] if recovery else ""
                source_quality = "takeout"
                match_type = "inferred_hidden_slot"
                match_confidence = "count_equal_ordered"
                availability = reconciled_video_availability(assigned["video_id"], "", recovered_status, 0)
                inferred += 1
                video_id = assigned["video_id"]
                snapshot_position = assigned["position"]
                snapshot_key_value = assigned["snapshot_key"]
                added_at = assigned["added_at"]
            else:
                title = current["title"]
                channel_id = current["channel_id"]
                channel = current["channel"]
                duration = current["duration_text"]
                video_id = current["video_id"]
                snapshot_match = snapshot_by_video_id.get(video_id) if video_id else None
                snapshot_position = snapshot_match["position"] if snapshot_match else 0
                snapshot_key_value = snapshot_match["snapshot_key"] if snapshot_match else ""
                added_at = snapshot_match["added_at"] if snapshot_match else ""
                if current.get("video_id"):
                    source_quality = "current_exact"
                    match_type = ""
                    match_confidence = "video_id"
                    availability = reconciled_video_availability(video_id, current["availability"], "", current["is_playable"])
                elif current.get("is_playable"):
                    source_quality = "current_unknown"
                    match_type = ""
                    match_confidence = "current_only"
                    availability = ""
                else:
                    source_quality = "current"
                    match_type = "ambiguous_hidden_slot"
                    match_confidence = "hidden_slot_only"
                    availability = ""
                    ambiguous += 1
            conn.execute(
                """
                INSERT INTO playlist_video_reconciled(
                  playlist_id, display_position, current_position, snapshot_position,
                  video_id, title, channel_id, channel, duration_text, is_playable, availability, url,
                  source_quality, match_type, match_confidence, snapshot_key, added_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    current["position"],
                    current["position"],
                    snapshot_position,
                    video_id,
                    title,
                    channel_id,
                    channel,
                    duration,
                    current["is_playable"],
                    availability,
                    current["url"] or (f"https://www.youtube.com/watch?v={video_id}&list={pid}" if video_id else ""),
                    source_quality,
                    match_type,
                    match_confidence,
                    snapshot_key_value,
                    added_at,
                    now,
                ),
            )
            total_rows += 1

        if not assign_hidden:
            used_missing_ids: set[str] = set()
            next_position = (max((row["position"] for row in current_rows), default=0) + 1) * 1000
            for snap in missing_snapshot_rows:
                if snap["video_id"] in used_missing_ids:
                    continue
                used_missing_ids.add(snap["video_id"])
                recovery = conn.execute(
                    """
                    SELECT *
                    FROM snapshot_video_recovery
                    WHERE snapshot_key = ? AND video_id = ?
                    """,
                    (snap["snapshot_key"], snap["video_id"]),
                ).fetchone()
                title = (recovery["title"] if recovery else "") or snap["video_id"]
                channel_id = recovery["channel_id"] if recovery else ""
                channel = channel_title_for_id(conn, channel_id)
                duration = recovery["duration_text"] if recovery else ""
                recovered_status = recovery["status"] if recovery else ""
                conn.execute(
                    """
                    INSERT INTO playlist_video_reconciled(
                      playlist_id, display_position, current_position, snapshot_position,
                      video_id, title, channel_id, channel, duration_text, is_playable, availability, url,
                      source_quality, match_type, match_confidence, snapshot_key, added_at, updated_at
                    )
                    VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pid,
                        next_position,
                        snap["position"],
                        snap["video_id"],
                        title,
                        channel_id,
                        channel,
                        duration,
                        reconciled_video_availability(snap["video_id"], "", recovered_status, 0),
                        f"https://www.youtube.com/watch?v={snap['video_id']}&list={pid}",
                        "takeout",
                        "ambiguous_hidden_candidate",
                        "snapshot_missing",
                        snap["snapshot_key"],
                        snap["added_at"],
                        now,
                    ),
                )
                ambiguous += 1
                total_rows += 1
                next_position += 1

    return {"playlists": len(playlist_ids), "rows": total_rows, "inferred": inferred, "ambiguous": ambiguous}


def playlist_scan_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
    stale_days: int = 7,
) -> list[sqlite3.Row]:
    stale_before = int(time.time()) - max(stale_days, 0) * 86400
    where = ["p.playlist_id <> ''"]
    params: list[Any] = []
    if not force:
        where.append(
            """
            (
              ps.playlist_id IS NULL
              OR ps.scan_status <> 'ok'
              OR (
                CAST(REPLACE(p.video_count_text, ',', '') AS INTEGER) > 0
                AND CAST(REPLACE(p.video_count_text, ',', '') AS INTEGER) <> COALESCE(ps.video_count, -1)
              )
              OR (ps.scanned_at > 0 AND ps.scanned_at < ?)
            )
            """
        )
        params.append(stale_before)
    sql = f"""
        SELECT p.playlist_id,
               p.title,
               p.video_count_text,
               COALESCE(ps.scanned_at, 0) AS scanned_at,
               COALESCE(ps.scan_status, '') AS scan_status,
               COALESCE(ps.video_count, 0) AS video_count,
               COALESCE(ps.hidden_count, 0) AS hidden_count
        FROM playlists p
        LEFT JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
        WHERE {" AND ".join(where)}
        ORDER BY
          CASE WHEN ps.playlist_id IS NULL THEN 0 ELSE 1 END,
          COALESCE(ps.scanned_at, 0),
          p.title COLLATE NOCASE
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def metadata_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
    stale_days: int = 30,
) -> list[sqlite3.Row]:
    stale_before = int(time.time()) - max(stale_days, 0) * 86400
    channel_retry_sql = f"""
      (
        COALESCE(ch.fetch_status, '') = 'error'
        OR COALESCE(ch.fetched_at, 0) = 0
        OR COALESCE(ch.fetched_at, 0) < {stale_before}
      )
    """
    if limit and not force:
        known_channel_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM channels ch
                WHERE ch.channel_id <> ''
                  AND COALESCE(ch.status, '') NOT IN ('terminated', 'deleted')
                  AND (
                    COALESCE(ch.url, '') = ''
                    OR COALESCE(ch.thumbnail_path, '') = ''
                  )
                  AND {channel_retry_sql}
                """
            ).fetchone()[0]
        )
        if offset < known_channel_count:
            rows = conn.execute(
                f"""
                SELECT ch.channel_id AS video_id,
                       ch.channel_id,
                       COALESCE(NULLIF(ch.title, ''), ch.channel_id) AS channel_title,
                       0 AS playlist_count,
                       '' AS current_title,
                       'channel' AS metadata_source
                FROM channels ch
                WHERE ch.channel_id <> ''
                  AND COALESCE(ch.status, '') NOT IN ('terminated', 'deleted')
                  AND (
                    COALESCE(ch.url, '') = ''
                    OR COALESCE(ch.thumbnail_path, '') = ''
                  )
                  AND {channel_retry_sql}
                ORDER BY COALESCE(NULLIF(ch.title, ''), ch.channel_id) COLLATE NOCASE,
                         ch.channel_id
                LIMIT ? OFFSET ?
                """,
                (limit, max(0, offset)),
            ).fetchall()
            if len(rows) == limit or offset + len(rows) < known_channel_count:
                return rows

    where = ["q.video_id <> ''"]
    params: list[Any] = []
    if not force:
        where.append(
            f"""
            (
              (q.source_priority IN (0, 1) AND COALESCE(ch.status, '') NOT IN ('terminated', 'deleted') AND {channel_retry_sql})
              OR (q.source_priority NOT IN (0, 1) AND vm.video_id IS NULL)
              OR vm.fetch_status = 'error'
              OR (vm.channel_id <> '' AND COALESCE(ch.status, '') NOT IN ('terminated', 'deleted') AND COALESCE(ch.url, '') = '' AND {channel_retry_sql})
              OR (vm.channel_id <> '' AND COALESCE(ch.status, '') NOT IN ('terminated', 'deleted') AND COALESCE(ch.thumbnail_path, '') = '' AND {channel_retry_sql})
              OR (vm.fetched_at > 0 AND vm.fetched_at < ?)
            )
            """
        )
        params.append(stale_before)
    sql = f"""
        WITH all_channel_refs AS (
          SELECT vm.video_id,
                 vm.channel_id,
                 COALESCE(NULLIF(ch.title, ''), vm.channel_id) AS channel_title,
                 0 AS playlist_count,
                 COALESCE(NULLIF(vm.title, ''), vm.video_id) AS current_title,
                 0 AS playlist_sort,
                 '' AS history_sort
          FROM video_metadata vm
          LEFT JOIN channels ch ON ch.channel_id = vm.channel_id
          WHERE vm.video_id <> ''
            AND vm.channel_id <> ''
            AND (
              COALESCE(ch.url, '') = ''
              OR COALESCE(ch.thumbnail_path, '') = ''
            )
            AND {channel_retry_sql}
          UNION ALL
          SELECT pv.video_id,
                 pv.channel_id,
                 COALESCE(NULLIF(ch.title, ''), NULLIF(pv.channel, ''), pv.channel_id) AS channel_title,
                 COUNT(DISTINCT pv.playlist_id) AS playlist_count,
                 MIN(pv.title) AS current_title,
                 MAX(MAX(COALESCE(ps.scanned_at, 0), COALESCE(p.updated_at, 0), COALESCE(pv.updated_at, 0))) AS playlist_sort,
                 '' AS history_sort
          FROM playlist_videos pv
          JOIN playlists p ON p.playlist_id = pv.playlist_id
          LEFT JOIN playlist_scans ps ON ps.playlist_id = pv.playlist_id
          LEFT JOIN channels ch ON ch.channel_id = pv.channel_id
          WHERE pv.video_id <> ''
            AND pv.channel_id <> ''
            AND (
              COALESCE(ch.url, '') = ''
              OR COALESCE(ch.thumbnail_path, '') = ''
            )
            AND {channel_retry_sql}
          GROUP BY pv.video_id, pv.channel_id
          UNION ALL
          SELECT pvr.video_id,
                 pvr.channel_id,
                 COALESCE(NULLIF(ch.title, ''), NULLIF(pvr.channel, ''), pvr.channel_id) AS channel_title,
                 COUNT(DISTINCT pvr.playlist_id) AS playlist_count,
                 MIN(pvr.title) AS current_title,
                 MAX(MAX(COALESCE(ps.scanned_at, 0), COALESCE(p.updated_at, 0), COALESCE(pvr.updated_at, 0))) AS playlist_sort,
                 '' AS history_sort
          FROM playlist_video_reconciled pvr
          JOIN playlists p ON p.playlist_id = pvr.playlist_id
          LEFT JOIN playlist_scans ps ON ps.playlist_id = pvr.playlist_id
          LEFT JOIN channels ch ON ch.channel_id = pvr.channel_id
          WHERE pvr.video_id <> ''
            AND pvr.channel_id <> ''
            AND (
              COALESCE(ch.url, '') = ''
              OR COALESCE(ch.thumbnail_path, '') = ''
            )
            AND {channel_retry_sql}
          GROUP BY pvr.video_id, pvr.channel_id
          UNION ALL
          SELECT hr.video_id,
                 hr.channel_id,
                 COALESCE(NULLIF(ch.title, ''), NULLIF(hr.channel, ''), hr.channel_id) AS channel_title,
                 0 AS playlist_count,
                 MIN(hr.title) AS current_title,
                 0 AS playlist_sort,
                 MAX(hr.best_watch_time) AS history_sort
          FROM history_reconciled hr
          LEFT JOIN channels ch ON ch.channel_id = hr.channel_id
          WHERE hr.video_id <> ''
            AND hr.channel_id <> ''
            AND (
              COALESCE(ch.url, '') = ''
              OR COALESCE(ch.thumbnail_path, '') = ''
            )
            AND {channel_retry_sql}
          GROUP BY hr.video_id, hr.channel_id
        ),
        known_channel_sources AS (
          SELECT ch.channel_id AS video_id,
                 ch.channel_id,
                 COALESCE(NULLIF(ch.title, ''), ch.channel_id) AS channel_title,
                 0 AS source_priority,
                 0 AS playlist_count,
                 '' AS current_title,
                 0 AS playlist_sort,
                 '' AS history_sort
          FROM channels ch
          WHERE ch.channel_id <> ''
            AND (
              COALESCE(ch.url, '') = ''
              OR COALESCE(ch.thumbnail_path, '') = ''
            )
            AND {channel_retry_sql}
        ),
        discovered_channel_sources AS (
          SELECT ref.channel_id AS video_id,
                 ref.channel_id,
                 MAX(ref.channel_title) AS channel_title,
                 1 AS source_priority,
                 SUM(ref.playlist_count) AS playlist_count,
                 '' AS current_title,
                 MAX(ref.playlist_sort) AS playlist_sort,
                 MAX(ref.history_sort) AS history_sort
          FROM all_channel_refs ref
          LEFT JOIN channels ch ON ch.channel_id = ref.channel_id
          WHERE ch.channel_id IS NULL
          GROUP BY ref.channel_id
        ),
        queue_sources AS (
          SELECT * FROM known_channel_sources
          UNION ALL
          SELECT * FROM discovered_channel_sources
          UNION ALL
          SELECT pv.video_id,
                 '' AS channel_id,
                 '' AS channel_title,
                 2 AS source_priority,
                 COUNT(DISTINCT pv.playlist_id) AS playlist_count,
                 MIN(pv.title) AS current_title,
                 MAX(MAX(COALESCE(ps.scanned_at, 0), COALESCE(p.updated_at, 0), COALESCE(pv.updated_at, 0))) AS playlist_sort,
                 '' AS history_sort
          FROM playlist_videos pv
          JOIN playlists p ON p.playlist_id = pv.playlist_id
          LEFT JOIN playlist_scans ps ON ps.playlist_id = pv.playlist_id
          WHERE pv.video_id <> ''
          GROUP BY pv.video_id
          UNION ALL
          SELECT hr.video_id,
                 '' AS channel_id,
                 '' AS channel_title,
                 3 AS source_priority,
                 0 AS playlist_count,
                 MIN(hr.title) AS current_title,
                 0 AS playlist_sort,
                 MAX(hr.best_watch_time) AS history_sort
          FROM history_reconciled hr
          WHERE hr.video_id <> ''
          GROUP BY hr.video_id
        ),
        q AS (
          SELECT video_id,
                 MIN(source_priority) AS source_priority,
                 MAX(channel_id) AS channel_id,
                 MAX(channel_title) AS channel_title,
                 SUM(playlist_count) AS playlist_count,
                 MIN(current_title) AS current_title,
                 MAX(playlist_sort) AS playlist_sort,
                 MAX(history_sort) AS history_sort
          FROM queue_sources
          GROUP BY video_id
        )
        SELECT q.video_id,
               q.channel_id,
               q.channel_title,
               q.playlist_count,
               q.current_title,
               CASE
                 WHEN q.source_priority IN (0, 1) THEN 'channel'
                 WHEN q.source_priority = 2 THEN 'playlist'
                 ELSE 'history'
               END AS metadata_source
        FROM q
        LEFT JOIN video_metadata vm ON vm.video_id = q.video_id
        LEFT JOIN channels ch ON ch.channel_id = COALESCE(NULLIF(vm.channel_id, ''), NULLIF(q.channel_id, ''))
        WHERE {" AND ".join(where)}
        ORDER BY q.source_priority,
                 CASE WHEN q.source_priority IN (0, 1) THEN q.channel_title ELSE '' END COLLATE NOCASE,
                 CASE WHEN q.source_priority = 2 THEN q.playlist_sort ELSE 0 END DESC,
                 CASE WHEN q.source_priority = 3 THEN q.history_sort ELSE '' END DESC,
                 COALESCE(vm.fetched_at, 0),
                 q.video_id
    """
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
    stale_before = int(time.time()) - max(stale_days, 0) * 86400
    channel_retry_sql = f"""
      (
        COALESCE(ch.fetch_status, '') = 'error'
        OR COALESCE(ch.fetched_at, 0) = 0
        OR COALESCE(ch.fetched_at, 0) < {stale_before}
      )
    """
    where = ["q.video_id <> ''"]
    params: list[Any] = []
    if not force:
        where.append(
            f"""
            (
              (q.source_priority IN (0, 1) AND COALESCE(ch.status, '') NOT IN ('terminated', 'deleted') AND {channel_retry_sql})
              OR (q.source_priority NOT IN (0, 1) AND vm.video_id IS NULL)
              OR vm.fetch_status = 'error'
              OR (vm.channel_id <> '' AND COALESCE(ch.status, '') NOT IN ('terminated', 'deleted') AND COALESCE(ch.url, '') = '' AND {channel_retry_sql})
              OR (vm.channel_id <> '' AND COALESCE(ch.status, '') NOT IN ('terminated', 'deleted') AND COALESCE(ch.thumbnail_path, '') = '' AND {channel_retry_sql})
              OR (vm.fetched_at > 0 AND vm.fetched_at < ?)
            )
            """
        )
        params.append(stale_before)
    row = conn.execute(
        f"""
        WITH all_channel_refs AS (
          SELECT vm.video_id,
                 vm.channel_id
          FROM video_metadata vm
          LEFT JOIN channels ch ON ch.channel_id = vm.channel_id
          WHERE vm.video_id <> ''
            AND vm.channel_id <> ''
            AND (
              COALESCE(ch.url, '') = ''
              OR COALESCE(ch.thumbnail_path, '') = ''
            )
            AND {channel_retry_sql}
          UNION
          SELECT pv.video_id,
                 pv.channel_id
          FROM playlist_videos pv
          LEFT JOIN channels ch ON ch.channel_id = pv.channel_id
          WHERE pv.video_id <> ''
            AND pv.channel_id <> ''
            AND (
              COALESCE(ch.url, '') = ''
              OR COALESCE(ch.thumbnail_path, '') = ''
            )
            AND {channel_retry_sql}
          UNION
          SELECT pvr.video_id,
                 pvr.channel_id
          FROM playlist_video_reconciled pvr
          LEFT JOIN channels ch ON ch.channel_id = pvr.channel_id
          WHERE pvr.video_id <> ''
            AND pvr.channel_id <> ''
            AND (
              COALESCE(ch.url, '') = ''
              OR COALESCE(ch.thumbnail_path, '') = ''
            )
            AND {channel_retry_sql}
          UNION
          SELECT hr.video_id,
                 hr.channel_id
          FROM history_reconciled hr
          LEFT JOIN channels ch ON ch.channel_id = hr.channel_id
          WHERE hr.video_id <> ''
            AND hr.channel_id <> ''
            AND (
              COALESCE(ch.url, '') = ''
              OR COALESCE(ch.thumbnail_path, '') = ''
            )
            AND {channel_retry_sql}
        ),
        known_channel_sources AS (
          SELECT ch.channel_id AS video_id,
                 0 AS source_priority
          FROM channels ch
          WHERE ch.channel_id <> ''
            AND (
              COALESCE(ch.url, '') = ''
              OR COALESCE(ch.thumbnail_path, '') = ''
            )
            AND {channel_retry_sql}
        ),
        discovered_channel_sources AS (
          SELECT ref.channel_id AS video_id,
                 1 AS source_priority
          FROM all_channel_refs ref
          LEFT JOIN channels ch ON ch.channel_id = ref.channel_id
          WHERE ch.channel_id IS NULL
          GROUP BY ref.channel_id
        ),
        queue_sources AS (
          SELECT video_id, source_priority
          FROM known_channel_sources
          UNION
          SELECT video_id, source_priority
          FROM discovered_channel_sources
          UNION
          SELECT video_id, 2 AS source_priority
          FROM playlist_videos
          WHERE video_id <> ''
          UNION
          SELECT video_id, 3 AS source_priority
          FROM history_reconciled
          WHERE video_id <> ''
        ),
        q AS (
          SELECT video_id,
                 MIN(source_priority) AS source_priority
          FROM queue_sources
          GROUP BY video_id
        )
        SELECT COUNT(*) AS count
        FROM q
        LEFT JOIN video_metadata vm ON vm.video_id = q.video_id
        LEFT JOIN channels ch ON ch.channel_id = COALESCE(NULLIF(vm.channel_id, ''), q.video_id)
        WHERE {" AND ".join(where)}
        """,
        params,
    ).fetchone()
    return int(row["count"] or 0)


def admin_status(
    db_path: Path,
    metadata_worker: "MetadataWorker | None" = None,
    playlist_worker: "PlaylistScanWorker | None" = None,
    live_history_worker: "LiveHistoryWorker | None" = None,
    placeholder_recovery_worker: "PlaceholderRecoveryWorker | None" = None,
) -> dict[str, Any]:
    reconcile_worker_runs(db_path, metadata_worker, playlist_worker, live_history_worker, placeholder_recovery_worker)
    conn = connect(db_path)
    try:
        counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(DISTINCT video_id) AS distinct_playlist_videos,
                  COUNT(*) AS playlist_video_rows,
                  (SELECT COUNT(*) FROM history_reconciled) AS history_rows,
                  (SELECT COUNT(DISTINCT video_id) FROM history_reconciled WHERE video_id <> '') AS distinct_history_videos
                FROM playlist_videos
                WHERE video_id <> ''
                """
            ).fetchone()
        )
        live_history_counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(*) AS live_rows,
                  COUNT(DISTINCT video_id) AS live_video_ids,
                  COALESCE(MAX(imported_at), 0) AS last_imported_at
                FROM youtube_history_occurrences
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
                FROM video_metadata
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
                  SUM(CASE WHEN COALESCE(url, '') = '' THEN 1 ELSE 0 END) AS url_missing
                FROM channels
                WHERE channel_id <> ''
                """
            ).fetchone()
        )
        metadata_queue_count_value = metadata_queue_count(conn, force=False, stale_days=30)
        playlist_queue_count = len(playlist_scan_queue_rows(conn, force=False, stale_days=7))
        placeholder_recovery_queue_count = playlist_placeholder_recovery_count(conn, force=False)
        playlist_queue_rows = [
            dict(row)
            for row in playlist_scan_queue_rows(conn, limit=20, force=False, stale_days=7)
        ]
        metadata_queue_preview_rows = [
            dict(row)
            for row in metadata_queue_rows(conn, limit=20, force=False, stale_days=30)
        ]
        placeholder_recovery_queue_rows = [
            dict(row)
            for row in playlist_placeholder_recovery_rows(conn, limit=20, force=False)
        ]
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
        "running": metadata_worker.is_running() if metadata_worker else False,
        "metadataRunning": metadata_worker.is_running() if metadata_worker else False,
        "playlistScanRunning": playlist_worker.is_running() if playlist_worker else False,
        "liveHistoryRunning": live_history_worker.is_running() if live_history_worker else False,
        "placeholderRecoveryRunning": placeholder_recovery_worker.is_running() if placeholder_recovery_worker else False,
        "counts": counts,
        "liveHistoryCounts": live_history_counts,
        "playlistCounts": playlist_counts,
        "metadataCounts": metadata_counts,
        "channelCounts": channel_counts,
            "queueCount": metadata_queue_count_value,
            "metadataQueueCount": metadata_queue_count_value,
        "playlistScanQueueCount": playlist_queue_count,
        "placeholderRecoveryQueueCount": placeholder_recovery_queue_count,
        "playlistScanQueue": playlist_queue_rows,
        "metadataQueue": metadata_queue_preview_rows,
        "placeholderRecoveryQueue": placeholder_recovery_queue_rows,
        "latestRun": dict(latest_metadata_run) if latest_metadata_run else None,
        "latestMetadataRun": dict(latest_metadata_run) if latest_metadata_run else None,
        "latestPlaylistScanRun": dict(latest_playlist_run) if latest_playlist_run else None,
        "latestLiveHistoryRun": dict(latest_live_history_run) if latest_live_history_run else None,
        "latestPlaceholderRecoveryRun": dict(latest_placeholder_recovery_run) if latest_placeholder_recovery_run else None,
        "logs": metadata_logs,
        "metadataLogs": metadata_logs,
        "playlistScanLogs": playlist_logs,
        "liveHistoryLogs": live_history_logs,
        "placeholderRecoveryLogs": placeholder_recovery_logs,
    }


def metadata_admin_status(
    db_path: Path,
    worker: "MetadataWorker | None" = None,
) -> dict[str, Any]:
    return admin_status(db_path, metadata_worker=worker)


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
    placeholder_recovery_running = placeholder_recovery_worker.is_running() if placeholder_recovery_worker else False
    now = int(time.time())
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
    print(f"Scanning {len(rows)} playlists for hidden videos...")
    total_hidden = 0
    for index, row in enumerate(rows, start=1):
        playlist_id = row["playlist_id"]
        title = row["title"]
        status = "ok"
        error = ""
        videos: list[dict[str, Any]] = []
        try:
            videos = scan_playlist_videos(opener, playlist_id)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            status = "error"
            error = str(exc)
        hidden_count = sum(1 for video in videos if not video["is_playable"])
        total_hidden += hidden_count
        with conn:
            conn.execute("DELETE FROM playlist_videos WHERE playlist_id = ?", (playlist_id,))
            now = int(time.time())
            for video in videos:
                channel_id = upsert_channel(
                    conn,
                    video.get("channel_id") or "",
                    title=video.get("channel") or "",
                    source="playlist",
                    updated_at=now,
                )
                conn.execute(
                    """
                    INSERT INTO playlist_videos(
                      playlist_id, position, video_id, title, channel_id, channel, duration_text,
                      is_playable, availability, url, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        video["playlist_id"],
                        video["position"],
                        video["video_id"],
                        video["title"],
                        channel_id,
                        video["channel"],
                        video["duration_text"],
                        video["is_playable"],
                        video["availability"],
                        video["url"],
                        now,
                    ),
                )
            conn.execute(
                """
                INSERT INTO playlist_scans(
                  playlist_id, scanned_at, video_count, hidden_count, scan_status, scan_error
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                  scanned_at=excluded.scanned_at,
                  video_count=excluded.video_count,
                  hidden_count=excluded.hidden_count,
                  scan_status=excluded.scan_status,
                  scan_error=excluded.scan_error
                """,
                (playlist_id, now, len(videos), hidden_count, status, error),
            )
        suffix = f"{hidden_count} hidden / {len(videos)} videos"
        if status != "ok":
            suffix = f"ERROR {error}"
        print(f"[{index:03d}/{len(rows):03d}] {suffix} - {title}")
    print(f"Found {total_hidden} hidden videos.")


def recover_archivarix_thumbnails(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    thumb_dir = Path(args.thumbs)
    conn = connect(db_path)
    rows = conn.execute(
        """
        SELECT p.playlist_id, p.title, s.hidden_count
        FROM playlist_scans s
        JOIN playlists p ON p.playlist_id = s.playlist_id
        WHERE s.hidden_count > 0
        ORDER BY s.hidden_count DESC, p.title COLLATE NOCASE
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
            conn.execute(
                "DELETE FROM archivarix_candidates WHERE playlist_id = ?", (playlist_id,)
            )
            now = int(time.time())
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
                channel_id = upsert_channel(
                    conn,
                    video.get("channelExternalId") or "",
                    title=video.get("channelTitle") or "",
                    source="archivarix_candidate",
                    updated_at=now,
                )
                conn.execute(
                    """
                    INSERT INTO archivarix_candidates(
                      playlist_id, video_id, title, channel_id, channel, status, duration_text,
                      upload_date, view_count, thumbnail_url, thumbnail_path,
                      archive_url, video_file_url, query, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                      title=excluded.title,
                      channel_id=excluded.channel_id,
                      channel=excluded.channel,
                      status=excluded.status,
                      duration_text=excluded.duration_text,
                      upload_date=excluded.upload_date,
                      view_count=excluded.view_count,
                      thumbnail_url=excluded.thumbnail_url,
                      thumbnail_path=excluded.thumbnail_path,
                      archive_url=excluded.archive_url,
                      video_file_url=excluded.video_file_url,
                      query=excluded.query,
                      updated_at=excluded.updated_at
                    """,
                    (
                        playlist_id,
                        video_id,
                        video.get("title") or "",
                        channel_id,
                        video.get("channelTitle") or "",
                        video.get("status") or "",
                        format_duration(video.get("duration")),
                        video.get("uploadDate") or "",
                        str(video.get("viewCount") or ""),
                        thumbnail_url,
                        thumbnail_path,
                        video.get("archiveUrl") or "",
                        video.get("videoFileUrl") or "",
                        query,
                        now,
                    ),
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


def import_takeout_snapshot(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    takeout_dir = Path(args.takeout)
    playlists_dir = takeout_dir / "playlists"
    playlists_csv = playlists_dir / "playlists.csv"
    if not playlists_csv.exists():
        raise SystemExit(f"Takeout playlists.csv not found: {playlists_csv}")

    conn = connect(db_path)
    snapshot_key = args.snapshot_key
    now = int(time.time())
    playlist_rows: list[dict[str, str]] = []
    with playlists_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        playlist_rows = list(csv.DictReader(handle))

    playlist_by_title: dict[str, dict[str, str]] = {}
    for row in playlist_rows:
        title = row.get("Playlist Title (Original)", "").strip()
        if title:
            playlist_by_title[title.casefold()] = row

    with conn:
        conn.execute("DELETE FROM snapshots WHERE snapshot_key = ?", (snapshot_key,))
        conn.execute(
            "INSERT INTO snapshots(snapshot_key, label, source_path, imported_at) VALUES (?, ?, ?, ?)",
            (snapshot_key, args.label, str(takeout_dir), now),
        )
        for row in playlist_rows:
            playlist_id = row.get("Playlist ID", "").strip()
            if not playlist_id:
                continue
            conn.execute(
                """
                INSERT INTO snapshot_playlists(
                  snapshot_key, playlist_id, title, created_at, updated_at,
                  visibility, video_order, source_file
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_key,
                    playlist_id,
                    row.get("Playlist Title (Original)", "").strip(),
                    row.get("Playlist Create Timestamp", "").strip(),
                    row.get("Playlist Update Timestamp", "").strip(),
                    row.get("Playlist Visibility", "").strip(),
                    row.get("Playlist Video Order", "").strip(),
                    str(playlists_csv.relative_to(ROOT)).replace("\\", "/"),
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
                    INSERT OR IGNORE INTO snapshot_playlists(
                      snapshot_key, playlist_id, title, source_file
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        snapshot_key,
                        playlist_id,
                        playlist_title,
                        str(video_file.relative_to(ROOT)).replace("\\", "/"),
                    ),
                )
            else:
                playlist_id = playlist_row.get("Playlist ID", "").strip()
                playlist_title = playlist_row.get("Playlist Title (Original)", "").strip()
            if not playlist_id:
                continue
            with video_file.open("r", encoding="utf-8-sig", newline="") as handle:
                for position, row in enumerate(csv.DictReader(handle), start=1):
                    video_id = row.get("Video ID", "").strip()
                    if not video_id:
                        continue
                    conn.execute(
                        """
                        INSERT INTO snapshot_videos(
                          snapshot_key, playlist_id, playlist_title, position,
                          video_id, added_at, source_file
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_key,
                            playlist_id,
                            playlist_title,
                            position,
                            video_id,
                            row.get("Playlist Video Creation Timestamp", "").strip(),
                            str(video_file.relative_to(ROOT)).replace("\\", "/"),
                        ),
                    )
                    imported_video_rows += 1
        reconcile_stats = rebuild_playlist_reconciliation(conn)

    print(
        f"Imported {len(playlist_rows)} snapshot playlists and "
        f"{imported_video_rows} snapshot video rows into {snapshot_key}."
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
    now = int(time.time())
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


def import_history(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    takeout_path = Path(args.takeout)
    requested_key = getattr(args, "history_key", "") or ""
    if requested_key:
        sources = [load_takeout_history_source(takeout_path, requested_key)]
    else:
        zips = find_takeout_zips(takeout_path)
        if zips:
            sources = [load_takeout_history_source(zip_path, "") for zip_path in zips]
        else:
            sources = [load_takeout_history_source(takeout_path, "")]

    conn = connect(db_path)
    imported_at = int(time.time())
    total_watch_rows = 0
    distinct_video_ids: set[str] = set()
    imported_keys: list[str] = []
    with conn:
        conn.execute("DELETE FROM takeout_history_occurrences")
        for history_key, watch_text in sources:
            imported_keys.append(history_key)
            watch_rows = parse_takeout_watch_history_text(watch_text) if watch_text else []
            total_watch_rows += len(watch_rows)
            for position, row in enumerate(watch_rows, start=1):
                watched_at_iso = takeout_watch_datetime(row["watched_at"])
                row_hash = history_row_hash(row)
                if row["video_id"]:
                    distinct_video_ids.add(row["video_id"])
                channel_id = upsert_channel(
                    conn,
                    row.get("channel_id") or youtube_channel_id_from_url(row["channel_url"]),
                    title=row["channel"],
                    url=row["channel_url"],
                    source="takeout_history",
                )
                conn.execute(
                    """
                    INSERT INTO takeout_history_occurrences(
                      history_key, row_hash, video_id, title, url, channel_id, channel,
                      watched_at_iso
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        history_key,
                        row_hash,
                        row["video_id"],
                        row["title"],
                        row["url"],
                        channel_id,
                        row["channel"],
                        watched_at_iso,
                    ),
                )
        stats = rebuild_history_reconciliation(conn)
    conn.close()
    print(
        f"Imported {total_watch_rows} watch history rows from {', '.join(imported_keys)} "
        f"({len(distinct_video_ids)} distinct videos). "
        f"Reconciled {stats['rows']} rows ({stats['matched']} matched)."
    )


def recover_snapshot_missing(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    thumb_dir = Path(args.thumbs)
    conn = connect(db_path)
    archivarix_opener = load_cookie_opener(Path(args.archivarix_cookies))
    where_clauses = [
        "sv.snapshot_key = ?",
        """
        (
          pv.video_id IS NULL
          OR pv.is_playable = 0
          OR lower(trim(COALESCE(pv.title, ''), '[]() ')) IN ('deleted video', 'private video')
          OR lower(COALESCE(pv.title, '')) LIKE '%unavailable%'
          OR lower(COALESCE(pv.availability, '')) LIKE '%unavailable%'
          OR lower(COALESCE(pv.availability, '')) LIKE '%deleted%'
          OR lower(COALESCE(pv.availability, '')) LIKE '%private%'
        )
        """,
    ]
    params: list[Any] = [args.snapshot_key]
    if args.likely_hidden_only:
        where_clauses.append(
            """
            EXISTS (
              SELECT 1
              FROM playlist_scans ps
              WHERE ps.playlist_id = sv.playlist_id
                AND ps.hidden_count > 0
            )
            """
        )
    rows = conn.execute(
        f"""
        SELECT DISTINCT sv.snapshot_key, sv.video_id
        FROM snapshot_videos sv
        JOIN playlists p ON p.playlist_id = sv.playlist_id
        LEFT JOIN playlist_videos pv
          ON pv.playlist_id = sv.playlist_id
         AND pv.video_id = sv.video_id
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
                FROM snapshot_video_recovery
                WHERE snapshot_key = ? AND video_id = ? AND thumbnail_path <> ''
                """,
                (row["snapshot_key"], row["video_id"]),
            ).fetchone()
            is None
        ]
    if args.limit:
        rows = rows[: args.limit]
    scope = "likely hidden" if args.likely_hidden_only else "missing snapshot"
    print(f"Recovering Archivarix thumbnails for {len(rows)} {scope} video IDs...")
    found = 0
    cached = 0
    channel_cache: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        snapshot_key = row["snapshot_key"]
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
            save_snapshot_video_recovery(
                conn,
                snapshot_key,
                video_id,
                video,
                thumbnail_url,
                thumbnail_path,
                status,
                error,
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
