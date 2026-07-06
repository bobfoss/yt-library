#!/usr/bin/env python3
"""Import YouTube library data and browse it locally."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import http.cookiejar
import http.server
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


ROOT = Path(__file__).resolve().parent
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


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS playlists (
  playlist_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '',
  video_count_text TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS groups (
  group_key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent_key TEXT REFERENCES groups(group_key) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  icon TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS group_playlists (
  group_key TEXT NOT NULL REFERENCES groups(group_key) ON DELETE CASCADE,
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  PRIMARY KEY (group_key, playlist_id)
);

CREATE TABLE IF NOT EXISTS playlist_scans (
  playlist_id TEXT PRIMARY KEY REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  scanned_at INTEGER NOT NULL DEFAULT 0,
  video_count INTEGER NOT NULL DEFAULT 0,
  hidden_count INTEGER NOT NULL DEFAULT 0,
  scan_status TEXT NOT NULL DEFAULT '',
  scan_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS playlist_videos (
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  duration_text TEXT NOT NULL DEFAULT '',
  is_playable INTEGER NOT NULL DEFAULT 1,
  availability TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (playlist_id, position)
);

CREATE TABLE IF NOT EXISTS playlist_video_reconciled (
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
  match_confidence TEXT NOT NULL DEFAULT '',
  match_notes TEXT NOT NULL DEFAULT '',
  snapshot_key TEXT NOT NULL DEFAULT '',
  added_at TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (playlist_id, display_position)
);

CREATE TABLE IF NOT EXISTS archivarix_candidates (
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  video_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  duration_text TEXT NOT NULL DEFAULT '',
  upload_date TEXT NOT NULL DEFAULT '',
  view_count TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  archive_url TEXT NOT NULL DEFAULT '',
  video_file_url TEXT NOT NULL DEFAULT '',
  query TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (playlist_id, video_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_key TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  source_path TEXT NOT NULL DEFAULT '',
  imported_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshot_playlists (
  snapshot_key TEXT NOT NULL REFERENCES snapshots(snapshot_key) ON DELETE CASCADE,
  playlist_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  visibility TEXT NOT NULL DEFAULT '',
  video_order TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (snapshot_key, playlist_id)
);

CREATE TABLE IF NOT EXISTS snapshot_videos (
  snapshot_key TEXT NOT NULL REFERENCES snapshots(snapshot_key) ON DELETE CASCADE,
  playlist_id TEXT NOT NULL,
  playlist_title TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL,
  video_id TEXT NOT NULL,
  added_at TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (snapshot_key, playlist_id, position, video_id)
);

CREATE TABLE IF NOT EXISTS snapshot_video_recovery (
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
);

CREATE TABLE IF NOT EXISTS video_metadata (
  video_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  duration_text TEXT NOT NULL DEFAULT '',
  view_count TEXT NOT NULL DEFAULT '',
  upload_date TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  yt_status TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  fetched_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS channels (
  channel_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  archivarix_channel_id TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS youtube_history_occurrences (
  ordinal INTEGER NOT NULL,
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  channel_url TEXT NOT NULL DEFAULT '',
  watch_date TEXT NOT NULL DEFAULT '',
  observed_at TEXT NOT NULL DEFAULT '',
  imported_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ordinal)
);

CREATE TABLE IF NOT EXISTS takeout_history_occurrences (
  history_key TEXT NOT NULL,
  row_hash TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  channel_url TEXT NOT NULL DEFAULT '',
  watched_at_iso TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (history_key, row_hash)
);

CREATE TABLE IF NOT EXISTS history_reconciled (
  reconciled_id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  channel_url TEXT NOT NULL DEFAULT '',
  best_watch_time TEXT NOT NULL DEFAULT '',
  watch_date TEXT NOT NULL DEFAULT '',
  source_quality TEXT NOT NULL DEFAULT '',
  youtube_history_key TEXT NOT NULL DEFAULT '',
  youtube_ordinal INTEGER NOT NULL DEFAULT 0,
  takeout_history_key TEXT NOT NULL DEFAULT '',
  takeout_row_hash TEXT NOT NULL DEFAULT '',
  match_confidence TEXT NOT NULL DEFAULT '',
  match_notes TEXT NOT NULL DEFAULT '',
  imported_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metadata_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL DEFAULT 0,
  finished_at INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  force INTEGER NOT NULL DEFAULT 0,
  stale_days INTEGER NOT NULL DEFAULT 0,
  last_video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS metadata_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS playlist_scan_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL DEFAULT 0,
  finished_at INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  force INTEGER NOT NULL DEFAULT 0,
  stale_days INTEGER NOT NULL DEFAULT 0,
  last_playlist_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS playlist_scan_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0,
  level TEXT NOT NULL DEFAULT '',
  playlist_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS live_history_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL DEFAULT 0,
  finished_at INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  last_video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS live_history_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS placeholder_recovery_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL DEFAULT 0,
  finished_at INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  force INTEGER NOT NULL DEFAULT 0,
  last_video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS placeholder_recovery_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_groups_parent_position ON groups(parent_key, position);
CREATE INDEX IF NOT EXISTS idx_group_playlists_position ON group_playlists(group_key, position);
CREATE INDEX IF NOT EXISTS idx_playlist_videos_hidden ON playlist_videos(is_playable, playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_playlist_video_reconciled_playlist ON playlist_video_reconciled(playlist_id, display_position);
CREATE INDEX IF NOT EXISTS idx_playlist_video_reconciled_video ON playlist_video_reconciled(video_id);
CREATE INDEX IF NOT EXISTS idx_archivarix_candidates_playlist ON archivarix_candidates(playlist_id, title);
CREATE INDEX IF NOT EXISTS idx_snapshot_videos_video ON snapshot_videos(snapshot_key, video_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_videos_playlist ON snapshot_videos(snapshot_key, playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_snapshot_video_recovery_status ON snapshot_video_recovery(snapshot_key, search_status);
CREATE INDEX IF NOT EXISTS idx_video_metadata_status ON video_metadata(fetch_status, fetched_at);
CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_video ON youtube_history_occurrences(video_id);
CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_search ON youtube_history_occurrences(title, channel, ordinal);
CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_date ON youtube_history_occurrences(watch_date, video_id);
CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_video ON takeout_history_occurrences(video_id);
CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_time ON takeout_history_occurrences(watched_at_iso, video_id);
CREATE INDEX IF NOT EXISTS idx_history_reconciled_video ON history_reconciled(video_id);
CREATE INDEX IF NOT EXISTS idx_history_reconciled_date ON history_reconciled(watch_date, source_quality);
CREATE INDEX IF NOT EXISTS idx_metadata_worker_log_run ON metadata_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_playlist_scan_worker_log_run ON playlist_scan_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_live_history_worker_log_run ON live_history_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_placeholder_recovery_worker_log_run ON placeholder_recovery_worker_log(run_id, created_at);
"""


@dataclass(frozen=True)
class GroupNode:
    key: str
    name: str
    parent_key: str | None
    position: int
    icon: str


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    ensure_columns(
        conn,
        "playlist_videos",
        {"channel_id": "TEXT NOT NULL DEFAULT ''"},
    )
    ensure_columns(
        conn,
        "playlist_video_reconciled",
        {"channel_id": "TEXT NOT NULL DEFAULT ''"},
    )
    ensure_columns(
        conn,
        "archivarix_candidates",
        {"channel_id": "TEXT NOT NULL DEFAULT ''"},
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


def merge_channel_value(existing: str, incoming: str) -> str:
    return incoming if incoming else existing


def upsert_channel(
    conn: sqlite3.Connection,
    channel_id: str,
    *,
    title: str = "",
    url: str = "",
    thumbnail_url: str = "",
    thumbnail_path: str = "",
    archivarix_channel_id: str = "",
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
                thumbnail_url = ?,
                thumbnail_path = ?,
                archivarix_channel_id = ?,
                source = ?,
                updated_at = ?
            WHERE channel_id = ?
            """,
            (
                merge_channel_value(existing["title"], title),
                merge_channel_value(existing["url"], url),
                merge_channel_value(existing["thumbnail_url"], thumbnail_url),
                merge_channel_value(existing["thumbnail_path"], thumbnail_path),
                merge_channel_value(existing["archivarix_channel_id"], archivarix_channel_id),
                merge_channel_value(existing["source"], source),
                now,
                channel_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO channels(
              channel_id, title, url, thumbnail_url, thumbnail_path,
              archivarix_channel_id, source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (channel_id, title, url, thumbnail_url, thumbnail_path, archivarix_channel_id, source, now),
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


def drop_deprecated_channel_columns(conn: sqlite3.Connection) -> None:
    cleanup_video_metadata_columns(conn)
    cleanup_snapshot_video_recovery_columns(conn)


def cleanup_video_metadata_columns(conn: sqlite3.Connection) -> None:
    deprecated = {"channel", "channel_url", "channel_thumbnail_url", "channel_thumbnail_path", "watch_url"}
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
    conn.execute(
        f"""
        INSERT INTO video_metadata(
          video_id, title, description, channel_id, duration_text, view_count,
          upload_date, thumbnail_url, thumbnail_path, yt_status,
          fetch_status, fetch_error, fetched_at, updated_at
        )
        SELECT video_id, title, description, {select_channel_id}, duration_text, view_count,
               upload_date, thumbnail_url, thumbnail_path, yt_status,
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
        "channel_url",
        "watch_date",
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
    conn.execute("ALTER TABLE youtube_history_occurrences RENAME TO youtube_history_occurrences_old")
    conn.execute(
        """
        CREATE TABLE youtube_history_occurrences (
          ordinal INTEGER NOT NULL,
          video_id TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          channel_id TEXT NOT NULL DEFAULT '',
          channel TEXT NOT NULL DEFAULT '',
          channel_url TEXT NOT NULL DEFAULT '',
          watch_date TEXT NOT NULL DEFAULT '',
          observed_at TEXT NOT NULL DEFAULT '',
          imported_at INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (ordinal)
        )
        """
    )
    old_cols = {row["name"] for row in conn.execute("PRAGMA table_info(youtube_history_occurrences_old)")}
    if old_cols:
        for row in conn.execute("SELECT * FROM youtube_history_occurrences_old").fetchall():
            imported_at = row["imported_at"] if "imported_at" in old_cols else 0
            observed_at = row["observed_at"] if "observed_at" in old_cols else ""
            if not is_iso_datetime(observed_at):
                observed_at = iso_from_unix(imported_at) or current_iso_timestamp()
            conn.execute(
                """
                INSERT OR IGNORE INTO youtube_history_occurrences(
                  ordinal, video_id, title, url, channel_id, channel, channel_url,
                  watch_date, observed_at, imported_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["ordinal"] if "ordinal" in old_cols else 0,
                    row["video_id"] if "video_id" in old_cols else "",
                    row["title"] if "title" in old_cols else "",
                    row["url"] if "url" in old_cols else "",
                    row["channel_id"] if "channel_id" in old_cols else youtube_channel_id_from_url(row["channel_url"] if "channel_url" in old_cols else ""),
                    row["channel"] if "channel" in old_cols else "",
                    row["channel_url"] if "channel_url" in old_cols else "",
                    row["watch_date"] if "watch_date" in old_cols else "",
                    observed_at,
                    imported_at,
                    row["updated_at"] if "updated_at" in old_cols else imported_at,
                ),
            )
    conn.execute("DROP TABLE youtube_history_occurrences_old")
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
        "channel_url",
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
    conn.execute("ALTER TABLE takeout_history_occurrences RENAME TO takeout_history_occurrences_old")
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
          channel_url TEXT NOT NULL DEFAULT '',
          watched_at_iso TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (history_key, row_hash)
        )
        """
    )
    conn.execute("DROP TABLE takeout_history_occurrences_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_video ON takeout_history_occurrences(video_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_time ON takeout_history_occurrences(watched_at_iso, video_id)"
    )


def ensure_history_reconciled_schema(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(history_reconciled)")}
    if "takeout_position" not in existing and "takeout_row_hash" in existing:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_reconciled_video ON history_reconciled(video_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_reconciled_channel ON history_reconciled(channel_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_reconciled_date ON history_reconciled(watch_date, source_quality)")
        return
    conn.execute("ALTER TABLE history_reconciled RENAME TO history_reconciled_old")
    conn.execute(
        """
        CREATE TABLE history_reconciled (
          reconciled_id TEXT PRIMARY KEY,
          video_id TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          channel_id TEXT NOT NULL DEFAULT '',
          channel TEXT NOT NULL DEFAULT '',
          channel_url TEXT NOT NULL DEFAULT '',
          best_watch_time TEXT NOT NULL DEFAULT '',
          watch_date TEXT NOT NULL DEFAULT '',
          source_quality TEXT NOT NULL DEFAULT '',
          youtube_history_key TEXT NOT NULL DEFAULT '',
          youtube_ordinal INTEGER NOT NULL DEFAULT 0,
          takeout_history_key TEXT NOT NULL DEFAULT '',
          takeout_row_hash TEXT NOT NULL DEFAULT '',
          match_confidence TEXT NOT NULL DEFAULT '',
          match_notes TEXT NOT NULL DEFAULT '',
          imported_at INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("DROP TABLE history_reconciled_old")
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
    target = thumb_dir / f"{safe_name(video_id)}_channel{ext}"
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
    for node in walk(lockup):
        if not isinstance(node, dict):
            continue
        endpoint = node.get("watchEndpoint")
        if isinstance(endpoint, dict) and endpoint.get("videoId") == video_id:
            start = endpoint.get("startTimeSeconds")
            url = f"https://www.youtube.com/watch?v={video_id}"
            if isinstance(start, int) and start > 0:
                url = f"{url}&t={start}s"
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
        "yt_status": status or ("OK" if title else ""),
    }


def fetch_watch_metadata(
    opener: urllib.request.OpenerDirector,
    video_id: str,
    thumb_dir: Path,
) -> dict[str, str]:
    watch_url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    page = request_text(opener, watch_url)
    metadata = extract_watch_metadata(page, video_id)
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
    if limit:
        sql += " LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()
    return conn.execute(sql).fetchall()


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
              ordinal, video_id, title, url, channel_id, channel, channel_url,
              watch_date, observed_at, imported_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ordinal) DO UPDATE SET
              video_id=excluded.video_id,
              title=excluded.title,
              url=excluded.url,
              channel_id=excluded.channel_id,
              channel=excluded.channel,
              channel_url=excluded.channel_url,
              watch_date=excluded.watch_date,
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
                channel_url,
                row.get("watch_date") or "",
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
        source_quality = "matched" if youtube_match else "takeout_exact"
        if youtube_match:
            matched += 1
        conn.execute(
            """
            INSERT INTO history_reconciled(
              reconciled_id, video_id, title, url, channel_id, channel, channel_url,
              best_watch_time, watch_date, source_quality,
              youtube_history_key, youtube_ordinal, takeout_history_key, takeout_row_hash,
              match_confidence, match_notes, imported_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"takeout:{takeout['history_key']}:{takeout['row_hash']}",
                takeout["video_id"],
                takeout["title"],
                takeout["url"],
                takeout["channel_id"],
                takeout["channel"],
                takeout["channel_url"],
                takeout["watched_at_iso"],
                takeout["watched_at_iso"][:10],
                source_quality,
                "youtube" if youtube_match else "",
                youtube_match if youtube_match else 0,
                takeout["history_key"],
                takeout["row_hash"],
                "video_id_date" if youtube_match else "takeout_only",
                "same video_id and watch_date" if youtube_match else "",
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
              reconciled_id, video_id, title, url, channel_id, channel, channel_url,
              best_watch_time, watch_date, source_quality,
                youtube_history_key, youtube_ordinal, takeout_history_key, takeout_row_hash,
                match_confidence, match_notes, imported_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?)
            """,
            (
                f"youtube:{youtube['ordinal']}",
                youtube["video_id"],
                youtube["title"],
                youtube["url"],
                youtube["channel_id"],
                youtube["channel"],
                youtube["channel_url"],
                youtube_watch_time,
                youtube["watch_date"],
                youtube_source_quality,
                "youtube",
                youtube["ordinal"],
                youtube_match_confidence,
                youtube_match_notes,
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
                channel = recovery["channel"] if recovery else ""
                duration = recovery["duration_text"] if recovery else ""
                source_quality = "inferred_hidden_slot"
                match_confidence = "count_equal_ordered"
                match_notes = "matched hidden current slot to missing Takeout video by ordered equal counts"
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
                    match_confidence = "video_id"
                    match_notes = ""
                elif current.get("is_playable"):
                    source_quality = "current_unknown"
                    match_confidence = "current_only"
                    match_notes = ""
                else:
                    source_quality = "ambiguous_hidden_slot"
                    match_confidence = "hidden_slot_only"
                    match_notes = "current hidden slot has no exposed video ID"
                    ambiguous += 1
            conn.execute(
                """
                INSERT INTO playlist_video_reconciled(
                  playlist_id, display_position, current_position, snapshot_position,
                  video_id, title, channel_id, channel, duration_text, is_playable, availability, url,
                  source_quality, match_confidence, match_notes, snapshot_key, added_at, updated_at
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
                    current["availability"],
                    current["url"] or (f"https://www.youtube.com/watch?v={video_id}&list={pid}" if video_id else ""),
                    source_quality,
                    match_confidence,
                    match_notes,
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
                channel = recovery["channel"] if recovery else ""
                duration = recovery["duration_text"] if recovery else ""
                conn.execute(
                    """
                    INSERT INTO playlist_video_reconciled(
                      playlist_id, display_position, current_position, snapshot_position,
                      video_id, title, channel_id, channel, duration_text, is_playable, availability, url,
                      source_quality, match_confidence, match_notes, snapshot_key, added_at, updated_at
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
                        "Takeout candidate; current hidden slot match is ambiguous",
                        f"https://www.youtube.com/watch?v={snap['video_id']}&list={pid}",
                        "ambiguous_hidden_candidate",
                        "snapshot_missing",
                        "missing from current playable scan; hidden slot mapping is ambiguous",
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
    return conn.execute(sql, params).fetchall()


def metadata_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    force: bool = False,
    stale_days: int = 30,
) -> list[sqlite3.Row]:
    stale_before = int(time.time()) - max(stale_days, 0) * 86400
    where = ["q.video_id <> ''"]
    params: list[Any] = []
    if not force:
        where.append(
            """
            (
              vm.video_id IS NULL
              OR vm.fetch_status = 'error'
              OR (vm.channel_id <> '' AND COALESCE(ch.url, '') = '')
              OR (vm.fetched_at > 0 AND vm.fetched_at < ?)
            )
            """
        )
        params.append(stale_before)
    sql = f"""
        WITH queue_sources AS (
          SELECT pv.video_id,
                 0 AS source_priority,
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
                 1 AS source_priority,
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
                 SUM(playlist_count) AS playlist_count,
                 MIN(current_title) AS current_title,
                 MAX(playlist_sort) AS playlist_sort,
                 MAX(history_sort) AS history_sort
          FROM queue_sources
          GROUP BY video_id
        )
        SELECT q.video_id,
               q.playlist_count,
               q.current_title,
               CASE WHEN q.source_priority = 0 THEN 'playlist' ELSE 'history' END AS metadata_source
        FROM q
        LEFT JOIN video_metadata vm ON vm.video_id = q.video_id
        LEFT JOIN channels ch ON ch.channel_id = vm.channel_id
        WHERE {" AND ".join(where)}
        ORDER BY q.source_priority,
                 CASE WHEN q.source_priority = 0 THEN q.playlist_sort ELSE 0 END DESC,
                 CASE WHEN q.source_priority = 1 THEN q.history_sort ELSE '' END DESC,
                 COALESCE(vm.fetched_at, 0),
                 q.video_id
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def metadata_queue_count(
    conn: sqlite3.Connection,
    force: bool = False,
    stale_days: int = 30,
) -> int:
    stale_before = int(time.time()) - max(stale_days, 0) * 86400
    where = ["q.video_id <> ''"]
    params: list[Any] = []
    if not force:
        where.append(
            """
            (
              vm.video_id IS NULL
              OR vm.fetch_status = 'error'
              OR (vm.channel_id <> '' AND COALESCE(ch.url, '') = '')
              OR (vm.fetched_at > 0 AND vm.fetched_at < ?)
            )
            """
        )
        params.append(stale_before)
    row = conn.execute(
        f"""
        WITH q AS (
          SELECT video_id
          FROM playlist_videos
          WHERE video_id <> ''
          UNION
          SELECT video_id
          FROM history_reconciled
          WHERE video_id <> ''
        )
        SELECT COUNT(*) AS count
        FROM q
        LEFT JOIN video_metadata vm ON vm.video_id = q.video_id
        LEFT JOIN channels ch ON ch.channel_id = vm.channel_id
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
        metadata_queue_count_value = metadata_queue_count(conn, force=False, stale_days=30)
        playlist_queue_count = len(playlist_scan_queue_rows(conn, force=False, stale_days=7))
        placeholder_recovery_queue_count = playlist_placeholder_recovery_count(conn, force=False)
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
            "queueCount": metadata_queue_count_value,
            "metadataQueueCount": metadata_queue_count_value,
        "playlistScanQueueCount": playlist_queue_count,
        "placeholderRecoveryQueueCount": placeholder_recovery_queue_count,
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


class MetadataWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_id = ""

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(
        self,
        db_path: Path,
        cookie_file: Path,
        thumb_dir: Path,
        delay: float,
        limit: int,
        force: bool,
        stale_days: int,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "run_id": self._run_id, "message": "Worker already running"}
            self._stop.clear()
            self._run_id = uuid.uuid4().hex
            self._thread = threading.Thread(
                target=self._run,
                args=(self._run_id, db_path, cookie_file, thumb_dir, delay, limit, force, stale_days),
                daemon=True,
            )
            self._thread.start()
            return {"started": True, "run_id": self._run_id, "message": "Worker started"}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return {"stopping": False, "message": "Worker is not running"}
            self._stop.set()
            return {"stopping": True, "run_id": self._run_id, "message": "Stop requested"}

    def _run(
        self,
        run_id: str,
        db_path: Path,
        cookie_file: Path,
        thumb_dir: Path,
        delay: float,
        limit: int,
        force: bool,
        stale_days: int,
    ) -> None:
        conn = connect(db_path)
        opener = load_cookie_opener(cookie_file)
        try:
            rows = metadata_queue_rows(conn, limit=limit, force=force, stale_days=stale_days)
            with conn:
                conn.execute(
                    """
                    INSERT INTO metadata_worker_runs(
                      run_id, status, started_at, total, delay_seconds,
                      requested_limit, force, stale_days, message
                    )
                    VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        int(time.time()),
                        len(rows),
                        delay,
                        limit,
                        1 if force else 0,
                        stale_days,
                        "Metadata worker started",
                    ),
                )
                log_worker_event(conn, run_id, "info", f"Queued {len(rows)} videos")

            processed = 0
            found = 0
            failed = 0
            for row in rows:
                if self._stop.is_set():
                    with conn:
                        conn.execute(
                            """
                            UPDATE metadata_worker_runs
                            SET status = 'stopped', finished_at = ?, message = ?
                            WHERE run_id = ?
                            """,
                            (int(time.time()), "Stop requested", run_id),
                        )
                        log_worker_event(conn, run_id, "warn", "Worker stopped by request")
                    return
                video_id = row["video_id"]
                metadata_source = row["metadata_source"] if "metadata_source" in row.keys() else "history"
                status = "ok"
                error = ""
                metadata: dict[str, str] = {
                    "video_id": video_id,
                    "title": "",
                    "description": "",
                    "channel_id": "",
                    "channel": "",
                    "channel_url": "",
                    "duration_text": "",
                    "view_count": "",
                    "upload_date": "",
                    "thumbnail_url": "",
                    "thumbnail_path": "",
                    "channel_thumbnail_url": "",
                    "channel_thumbnail_path": "",
                    "yt_status": "",
                }
                try:
                    metadata = fetch_watch_metadata(opener, video_id, thumb_dir)
                    if not metadata.get("title"):
                        status = "no_metadata"
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                    status = "error"
                    error = str(exc)
                now = int(time.time())
                with conn:
                    channel_id = upsert_channel(
                        conn,
                        metadata.get("channel_id", ""),
                        title=metadata.get("channel", ""),
                        url=metadata.get("channel_url", ""),
                        thumbnail_url=metadata.get("channel_thumbnail_url", ""),
                        thumbnail_path=metadata.get("channel_thumbnail_path", ""),
                        source="metadata",
                        updated_at=now,
                    )
                    conn.execute(
                        """
                        INSERT INTO video_metadata(
                          video_id, title, description, channel_id, duration_text, view_count,
                          upload_date, thumbnail_url, thumbnail_path,
                          yt_status, fetch_status, fetch_error, fetched_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(video_id) DO UPDATE SET
                          title=excluded.title,
                          description=excluded.description,
                          channel_id=excluded.channel_id,
                          duration_text=excluded.duration_text,
                          view_count=excluded.view_count,
                          upload_date=excluded.upload_date,
                          thumbnail_url=excluded.thumbnail_url,
                          thumbnail_path=excluded.thumbnail_path,
                          yt_status=excluded.yt_status,
                          fetch_status=excluded.fetch_status,
                          fetch_error=excluded.fetch_error,
                          fetched_at=excluded.fetched_at,
                          updated_at=excluded.updated_at
                        """,
                        (
                            video_id,
                            metadata.get("title", ""),
                            metadata.get("description", ""),
                            channel_id,
                            metadata.get("duration_text", ""),
                            metadata.get("view_count", ""),
                            metadata.get("upload_date", ""),
                            metadata.get("thumbnail_url", ""),
                            metadata.get("thumbnail_path", ""),
                            metadata.get("yt_status", ""),
                            status,
                            error,
                            now,
                            now,
                        ),
                    )
                    processed += 1
                    if status == "error":
                        failed += 1
                        log_worker_event(conn, run_id, f"{metadata_source} error", error, video_id)
                    else:
                        found += 1
                        title = metadata.get("title") or video_id
                        log_worker_event(conn, run_id, metadata_source, f"{status}: {title}", video_id)
                    conn.execute(
                        """
                        UPDATE metadata_worker_runs
                        SET processed = ?, found = ?, failed = ?, last_video_id = ?, message = ?
                        WHERE run_id = ?
                        """,
                        (
                            processed,
                            found,
                            failed,
                            video_id,
                            f"Processed {processed} of {len(rows)}",
                            run_id,
                        ),
                    )
                if delay and processed < len(rows):
                    time.sleep(delay)
            with conn:
                conn.execute(
                    """
                    UPDATE metadata_worker_runs
                    SET status = 'complete', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (int(time.time()), f"Completed {processed} videos", run_id),
                )
                log_worker_event(conn, run_id, "info", f"Worker complete: {processed} processed")
        except Exception as exc:
            with conn:
                conn.execute(
                    """
                    UPDATE metadata_worker_runs
                    SET status = 'error', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (int(time.time()), str(exc), run_id),
                )
                log_worker_event(conn, run_id, "error", f"Worker crashed: {exc}")
        finally:
            conn.close()


METADATA_WORKER = MetadataWorker()


class PlaylistScanWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_id = ""

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(
        self,
        db_path: Path,
        cookie_file: Path,
        delay: float,
        limit: int,
        force: bool,
        stale_days: int,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "run_id": self._run_id, "message": "Playlist scan already running"}
            self._stop.clear()
            self._run_id = uuid.uuid4().hex
            self._thread = threading.Thread(
                target=self._run,
                args=(self._run_id, db_path, cookie_file, delay, limit, force, stale_days),
                daemon=True,
            )
            self._thread.start()
            return {"started": True, "run_id": self._run_id, "message": "Playlist scan started"}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return {"stopping": False, "message": "Playlist scan is not running"}
            self._stop.set()
            return {"stopping": True, "run_id": self._run_id, "message": "Playlist scan stop requested"}

    def _run(
        self,
        run_id: str,
        db_path: Path,
        cookie_file: Path,
        delay: float,
        limit: int,
        force: bool,
        stale_days: int,
    ) -> None:
        conn = connect(db_path)
        opener = load_cookie_opener(cookie_file)
        try:
            rows = playlist_scan_queue_rows(conn, limit=limit, force=force, stale_days=stale_days)
            with conn:
                conn.execute(
                    """
                    INSERT INTO playlist_scan_worker_runs(
                      run_id, status, started_at, total, delay_seconds,
                      requested_limit, force, stale_days, message
                    )
                    VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        int(time.time()),
                        len(rows),
                        delay,
                        limit,
                        1 if force else 0,
                        stale_days,
                        "Playlist scan worker started",
                    ),
                )
                log_playlist_scan_event(conn, run_id, "info", f"Queued {len(rows)} playlists")

            processed = 0
            found = 0
            failed = 0
            for row in rows:
                if self._stop.is_set():
                    with conn:
                        conn.execute(
                            """
                            UPDATE playlist_scan_worker_runs
                            SET status = 'stopped', finished_at = ?, message = ?
                            WHERE run_id = ?
                            """,
                            (int(time.time()), "Stop requested", run_id),
                        )
                        log_playlist_scan_event(conn, run_id, "warn", "Playlist scan stopped by request")
                    return

                playlist_id = row["playlist_id"]
                title = row["title"] or playlist_id
                status = "ok"
                error = ""
                backend = "web"
                ytdlp_error = ""
                videos: list[dict[str, Any]] = []
                try:
                    videos = scan_playlist_videos_ytdlp(playlist_id, cookie_file)
                    backend = "yt-dlp"
                except Exception as exc:
                    ytdlp_error = str(exc)
                    try:
                        videos = scan_playlist_videos(opener, playlist_id)
                    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as web_exc:
                        status = "error"
                        error = str(web_exc)
                expected_count = expected_video_count(row["video_count_text"] if "video_count_text" in row.keys() else "")
                if status == "ok" and not videos and expected_count > 0:
                    status = "error"
                    error = f"Parsed 0 videos, but playlist metadata says {expected_count} videos"
                    if ytdlp_error:
                        error += f"; yt-dlp failed: {ytdlp_error[:500]}"
                with conn:
                    video_count, hidden_count = save_playlist_scan(
                        conn,
                        playlist_id,
                        videos,
                        status,
                        error,
                    )
                    processed += 1
                    if status == "error":
                        failed += 1
                        log_playlist_scan_event(conn, run_id, "error", f"{title}: {error}", playlist_id)
                    else:
                        found += 1
                        log_playlist_scan_event(
                            conn,
                            run_id,
                            "info",
                            f"{title}: {video_count} videos, {hidden_count} hidden ({backend})",
                            playlist_id,
                        )
                    conn.execute(
                        """
                        UPDATE playlist_scan_worker_runs
                        SET processed = ?, found = ?, failed = ?, last_playlist_id = ?, message = ?
                        WHERE run_id = ?
                        """,
                        (
                            processed,
                            found,
                            failed,
                            playlist_id,
                            f"Processed {processed} of {len(rows)}",
                            run_id,
                        ),
                    )
                if delay and processed < len(rows):
                    time.sleep(delay)
            with conn:
                conn.execute(
                    """
                    UPDATE playlist_scan_worker_runs
                    SET status = 'complete', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (int(time.time()), f"Completed {processed} playlists", run_id),
                )
                log_playlist_scan_event(conn, run_id, "info", f"Playlist scan complete: {processed} processed")
        except Exception as exc:
            with conn:
                conn.execute(
                    """
                    UPDATE playlist_scan_worker_runs
                    SET status = 'error', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (int(time.time()), str(exc), run_id),
                )
                log_playlist_scan_event(conn, run_id, "error", f"Playlist scan crashed: {exc}")
        finally:
            conn.close()


PLAYLIST_SCAN_WORKER = PlaylistScanWorker()


class LiveHistoryWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_id = ""

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(
        self,
        db_path: Path,
        cookie_file: Path,
        mode: str,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "run_id": self._run_id, "message": "History fetch already running"}
            self._stop.clear()
            self._run_id = uuid.uuid4().hex
            self._thread = threading.Thread(
                target=self._run,
                args=(self._run_id, db_path, cookie_file, mode),
                daemon=True,
            )
            self._thread.start()
            label = "Verify history" if mode == "verify" else "History fetch"
            return {"started": True, "run_id": self._run_id, "message": f"{label} started"}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return {"stopping": False, "message": "History fetch is not running"}
            self._stop.set()
            return {"stopping": True, "run_id": self._run_id, "message": "History fetch stop requested"}

    def _run(
        self,
        run_id: str,
        db_path: Path,
        cookie_file: Path,
        mode: str,
    ) -> None:
        conn = connect(db_path)
        mode = "verify" if mode == "verify" else "recent"
        label = "Verify history" if mode == "verify" else "History fetch"
        batch_size = HISTORY_BATCH_SIZE
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO live_history_worker_runs(
                      run_id, status, started_at, delay_seconds,
                      requested_limit, message
                    )
                    VALUES (?, 'running', ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        int(time.time()),
                        HISTORY_BATCH_DELAY_SECONDS,
                        batch_size,
                        f"{label} started",
                    ),
                )
                log_live_history_event(conn, run_id, "info", f"{label} started with {batch_size} per batch")

            if self._stop.is_set():
                with conn:
                    conn.execute(
                        """
                        UPDATE live_history_worker_runs
                        SET status = 'stopped', finished_at = ?, message = ?
                        WHERE run_id = ?
                        """,
                        (int(time.time()), "Stopped before fetch", run_id),
                    )
                    log_live_history_event(conn, run_id, "warn", "History fetch stopped before fetch")
                return

            start = 1
            processed = 0
            inserted_total = 0
            skipped_total = 0
            last_video_id = ""
            final_message = ""
            while not self._stop.is_set():
                end = start + batch_size - 1
                rows = fetch_youtube_history_web(cookie_file, limit=batch_size, start=start)
                fetched_ids = [row.get("video_id") or "" for row in rows if row.get("video_id")]
                with conn:
                    existing_ids = youtube_occurrence_sequence(conn, start, len(rows))
                    overlap_offset = find_feed_overlap(fetched_ids, existing_ids) if mode == "recent" else None
                    inserted, existing, batch_last_video_id = save_youtube_history_occurrences(conn, rows, start)
                    reconcile_stats = rebuild_history_reconciliation(conn)
                    seen = len(rows)
                    processed += seen
                    inserted_total += inserted
                    skipped_total += existing
                    if batch_last_video_id:
                        last_video_id = batch_last_video_id
                    final_message = (
                        f"{label}: entries {start}-{end}; {seen} fetched, "
                        f"{inserted} changed/new, {existing} existing, "
                        f"{reconcile_stats['matched']} matched"
                    )
                    conn.execute(
                        """
                        UPDATE live_history_worker_runs
                        SET total = ?, processed = ?, found = ?, skipped = ?,
                            last_video_id = ?, message = ?
                        WHERE run_id = ?
                        """,
                        (processed, processed, inserted_total, skipped_total, last_video_id, final_message, run_id),
                    )
                    log_live_history_event(conn, run_id, "info", final_message, last_video_id)
                if seen < batch_size:
                    break
                if mode == "recent" and overlap_offset is not None:
                    final_message = f"{label} reached already-known history after {processed} entries"
                    break
                if self._stop.wait(HISTORY_BATCH_DELAY_SECONDS):
                    break
                start += batch_size

            status = "stopped" if self._stop.is_set() else "complete"
            if not final_message:
                final_message = f"{label}: no history rows fetched"
            elif status == "complete":
                final_message = (
                    f"{label} complete: {processed} fetched, "
                    f"{inserted_total} changed/new, {skipped_total} existing"
                )
            else:
                final_message = (
                    f"{label} stopped: {processed} fetched, "
                    f"{inserted_total} changed/new, {skipped_total} existing"
                )
            with conn:
                conn.execute(
                    """
                    UPDATE live_history_worker_runs
                    SET status = ?, finished_at = ?, total = ?, processed = ?,
                        found = ?, skipped = ?, last_video_id = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (
                        status,
                        int(time.time()),
                        processed,
                        processed,
                        inserted_total,
                        skipped_total,
                        last_video_id,
                        final_message,
                        run_id,
                    ),
                )
                log_live_history_event(conn, run_id, "info" if status == "complete" else "warn", final_message, last_video_id)
        except Exception as exc:
            with conn:
                conn.execute(
                    """
                    UPDATE live_history_worker_runs
                    SET status = 'error', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (int(time.time()), str(exc), run_id),
                )
                log_live_history_event(conn, run_id, "error", f"History fetch crashed: {exc}")
        finally:
            conn.close()


LIVE_HISTORY_WORKER = LiveHistoryWorker()


class PlaceholderRecoveryWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_id = ""

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(
        self,
        db_path: Path,
        archivarix_cookie_file: Path,
        thumb_dir: Path,
        delay: float,
        limit: int,
        force: bool,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {
                    "started": False,
                    "run_id": self._run_id,
                    "message": "Placeholder recovery already running",
                }
            self._stop.clear()
            self._run_id = uuid.uuid4().hex
            self._thread = threading.Thread(
                target=self._run,
                args=(self._run_id, db_path, archivarix_cookie_file, thumb_dir, delay, limit, force),
                daemon=True,
            )
            self._thread.start()
            return {"started": True, "run_id": self._run_id, "message": "Placeholder recovery started"}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return {"stopping": False, "message": "Placeholder recovery is not running"}
            self._stop.set()
            return {"stopping": True, "run_id": self._run_id, "message": "Placeholder recovery stop requested"}

    def _run(
        self,
        run_id: str,
        db_path: Path,
        archivarix_cookie_file: Path,
        thumb_dir: Path,
        delay: float,
        limit: int,
        force: bool,
    ) -> None:
        conn = connect(db_path)
        archivarix_opener = load_cookie_opener(archivarix_cookie_file)
        try:
            rows = playlist_placeholder_recovery_rows(conn, limit=limit, force=force)
            channel_cache: dict[str, dict[str, Any]] = {}
            with conn:
                conn.execute(
                    """
                    INSERT INTO placeholder_recovery_worker_runs(
                      run_id, status, started_at, total, delay_seconds,
                      requested_limit, force, message
                    )
                    VALUES (?, 'running', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        int(time.time()),
                        len(rows),
                        delay,
                        limit,
                        1 if force else 0,
                        "Placeholder recovery started",
                    ),
                )
                log_placeholder_recovery_event(conn, run_id, "info", f"Queued {len(rows)} placeholder videos")

            processed = 0
            found = 0
            failed = 0
            skipped = 0
            for row in rows:
                if self._stop.is_set():
                    with conn:
                        conn.execute(
                            """
                            UPDATE placeholder_recovery_worker_runs
                            SET status = 'stopped', finished_at = ?, message = ?
                            WHERE run_id = ?
                            """,
                            (int(time.time()), "Stop requested", run_id),
                        )
                        log_placeholder_recovery_event(conn, run_id, "warn", "Placeholder recovery stopped by request")
                    return

                snapshot_key = row["snapshot_key"] or ""
                video_id = row["video_id"]
                title = ""
                status = "not_found"
                error = ""
                try:
                    video, thumbnail_url, thumbnail_path, status, error = recover_archivarix_video(
                        video_id,
                        thumb_dir,
                        archivarix_opener,
                        refresh_metadata=True,
                        no_api=False,
                        delay=delay,
                        channel_cache=channel_cache,
                    )
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
                    title = (video or {}).get("title") or video_id
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                    status = "error"
                    error = str(exc)
                    title = video_id

                with conn:
                    processed += 1
                    if status == "found":
                        found += 1
                        level = "found"
                        message = f"found: {title}"
                    elif status == "thumbnail_only":
                        found += 1
                        level = "thumbnail"
                        message = f"thumbnail only: {title}"
                    elif status == "not_found":
                        skipped += 1
                        level = "not found"
                        message = "not found"
                    else:
                        failed += 1
                        level = "error"
                        message = error or status
                    conn.execute(
                        """
                        UPDATE placeholder_recovery_worker_runs
                        SET processed = ?, found = ?, failed = ?, skipped = ?,
                            last_video_id = ?, message = ?
                        WHERE run_id = ?
                        """,
                        (
                            processed,
                            found,
                            failed,
                            skipped,
                            video_id,
                            f"Processed {processed} of {len(rows)}",
                            run_id,
                        ),
                    )
                    log_placeholder_recovery_event(conn, run_id, level, message, video_id)

            with conn:
                stats = rebuild_playlist_reconciliation(conn)
                final_message = (
                    f"Placeholder recovery complete: {processed} checked, "
                    f"{found} found, {skipped} not found, {failed} failed; "
                    f"reconciled {stats['rows']} rows"
                )
                conn.execute(
                    """
                    UPDATE placeholder_recovery_worker_runs
                    SET status = 'complete', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (int(time.time()), final_message, run_id),
                )
                log_placeholder_recovery_event(conn, run_id, "info", final_message)
        except Exception as exc:
            with conn:
                conn.execute(
                    """
                    UPDATE placeholder_recovery_worker_runs
                    SET status = 'error', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (int(time.time()), str(exc), run_id),
                )
                log_placeholder_recovery_event(conn, run_id, "error", f"Placeholder recovery crashed: {exc}")
        finally:
            conn.close()


PLACEHOLDER_RECOVERY_WORKER = PlaceholderRecoveryWorker()


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
                      channel_url, watched_at_iso
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        history_key,
                        row_hash,
                        row["video_id"],
                        row["title"],
                        row["url"],
                        channel_id,
                        row["channel"],
                        row["channel_url"],
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


def fetch_app_data(conn: sqlite3.Connection) -> dict[str, Any]:
    groups = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM groups ORDER BY COALESCE(parent_key, ''), position, name"
        )
    ]
    playlists = [
        dict(row)
        for row in conn.execute(
            """
            SELECT p.*,
                   COALESCE(s.video_count, 0) AS scanned_video_count,
                   COALESCE(s.hidden_count, 0) AS hidden_count,
                   COALESCE(s.scanned_at, 0) AS scanned_at,
                   COALESCE(s.scan_status, '') AS scan_status
            FROM playlists p
            LEFT JOIN playlist_scans s ON s.playlist_id = p.playlist_id
            ORDER BY p.title COLLATE NOCASE
            """
        )
    ]
    memberships = [
        dict(row)
        for row in conn.execute(
            """
            SELECT gp.group_key, gp.playlist_id, gp.position
            FROM group_playlists gp
            JOIN playlists p ON p.playlist_id = gp.playlist_id
            ORDER BY gp.group_key, gp.position, p.title COLLATE NOCASE
            """
        )
    ]
    hidden_videos = [
        dict(row)
        for row in conn.execute(
            """
            SELECT v.*, p.title AS playlist_title, p.url AS playlist_url
            FROM playlist_videos v
            JOIN playlists p ON p.playlist_id = v.playlist_id
            WHERE v.is_playable = 0
            ORDER BY p.title COLLATE NOCASE, v.position
            """
        )
    ]
    playlist_videos = [
        dict(row)
        for row in conn.execute(
            """
            SELECT v.*, p.title AS playlist_title, p.url AS playlist_url,
                   v.display_position AS position,
                   COALESCE(NULLIF(NULLIF(vm.title, '- YouTube'), 'YouTube'), r.title, '') AS metadata_title,
                   COALESCE(NULLIF(vm.description, ''), r.description, '') AS metadata_description,
                   COALESCE(NULLIF(vmc.title, ''), NULLIF(rc.title, ''), NULLIF(vc.title, ''), v.channel, '') AS metadata_channel,
                   COALESCE(NULLIF(vmc.url, ''), NULLIF(rc.url, ''), NULLIF(vc.url, ''), CASE WHEN r.channel_id <> '' THEN 'https://www.youtube.com/channel/' || r.channel_id ELSE '' END, '') AS metadata_channel_url,
                   COALESCE(NULLIF(vm.duration_text, ''), r.duration_text, '') AS metadata_duration,
                   COALESCE(NULLIF(vm.upload_date, ''), r.upload_date, '') AS metadata_upload_date,
                   COALESCE(NULLIF(vm.thumbnail_path, ''), r.thumbnail_path, '') AS metadata_thumbnail_path,
                   COALESCE(NULLIF(vmc.thumbnail_path, ''), NULLIF(rc.thumbnail_path, ''), '') AS metadata_channel_thumbnail_path,
                   COALESCE(NULLIF(vm.fetch_status, ''), r.search_status, '') AS metadata_fetch_status,
                   COALESCE(r.status, '') AS recovered_status
            FROM playlist_video_reconciled v
            JOIN playlists p ON p.playlist_id = v.playlist_id
            LEFT JOIN video_metadata vm ON vm.video_id = v.video_id
            LEFT JOIN snapshot_video_recovery r
              ON r.snapshot_key = v.snapshot_key AND r.video_id = v.video_id
            LEFT JOIN channels vmc ON vmc.channel_id = vm.channel_id
            LEFT JOIN channels rc ON rc.channel_id = r.channel_id
            LEFT JOIN channels vc ON vc.channel_id = v.channel_id
            ORDER BY p.title COLLATE NOCASE, v.display_position
            """
        )
    ]
    archivarix_candidates = [
        dict(row)
        for row in conn.execute(
            """
            SELECT a.*, p.title AS playlist_title, p.url AS playlist_url
            FROM archivarix_candidates a
            JOIN playlists p ON p.playlist_id = a.playlist_id
            ORDER BY p.title COLLATE NOCASE, a.upload_date DESC, a.title COLLATE NOCASE
            """
        )
    ]
    snapshots = [dict(row) for row in conn.execute("SELECT * FROM snapshots ORDER BY imported_at DESC")]
    snapshot_playlists = [
        dict(row)
        for row in conn.execute(
            """
            SELECT sp.*,
                   COUNT(sv.video_id) AS video_count,
                   p.title AS current_title,
                   COALESCE(ps.video_count, 0) AS current_video_count,
                   COALESCE(ps.hidden_count, 0) AS current_hidden_count
            FROM snapshot_playlists sp
            LEFT JOIN snapshot_videos sv
              ON sv.snapshot_key = sp.snapshot_key AND sv.playlist_id = sp.playlist_id
            LEFT JOIN playlists p ON p.playlist_id = sp.playlist_id
            LEFT JOIN playlist_scans ps ON ps.playlist_id = sp.playlist_id
            GROUP BY sp.snapshot_key, sp.playlist_id
            ORDER BY sp.title COLLATE NOCASE
            """
        )
    ]
    snapshot_missing = [
        dict(row)
        for row in conn.execute(
            """
            SELECT sv.snapshot_key,
                   sv.playlist_id,
                   sv.playlist_title,
                   sv.position,
                   sv.video_id,
                   sv.added_at,
                   p.title AS current_title,
                   p.url AS playlist_url,
                   COALESCE(r.title, '') AS recovered_title,
                   COALESCE(r.description, '') AS recovered_description,
                   COALESCE(NULLIF(rc.title, ''), '') AS recovered_channel,
                   COALESCE(r.status, '') AS recovered_status,
                   COALESCE(r.duration_text, '') AS recovered_duration,
                   COALESCE(r.upload_date, '') AS recovered_upload_date,
                   COALESCE(r.thumbnail_path, '') AS recovered_thumbnail_path,
                   COALESCE(r.search_status, '') AS recovery_search_status
            FROM snapshot_videos sv
            JOIN playlists p ON p.playlist_id = sv.playlist_id
            LEFT JOIN playlist_videos pv
              ON pv.playlist_id = sv.playlist_id
             AND pv.video_id = sv.video_id
             AND pv.is_playable = 1
            LEFT JOIN snapshot_video_recovery r
              ON r.snapshot_key = sv.snapshot_key
             AND r.video_id = sv.video_id
            LEFT JOIN channels rc ON rc.channel_id = r.channel_id
            WHERE pv.video_id IS NULL
            ORDER BY sv.playlist_title COLLATE NOCASE, sv.position
            """
        )
    ]
    snapshot_likely_hidden = [
        row
        for row in snapshot_missing
        if conn.execute(
            "SELECT hidden_count FROM playlist_scans WHERE playlist_id = ? AND hidden_count > 0",
            (row["playlist_id"],),
        ).fetchone()
    ]
    history_summary = dict(
        conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM history_reconciled) AS watch_rows,
              (SELECT COUNT(DISTINCT video_id) FROM history_reconciled WHERE video_id <> '') AS distinct_watch_videos
            """
        ).fetchone()
    )
    return {
        "groups": groups,
        "playlists": playlists,
        "memberships": memberships,
        "playlistVideos": playlist_videos,
        "hiddenVideos": hidden_videos,
        "archivarixCandidates": archivarix_candidates,
        "snapshots": snapshots,
        "snapshotPlaylists": snapshot_playlists,
        "snapshotMissing": snapshot_missing,
        "snapshotLikelyHidden": snapshot_likely_hidden,
        "historySummary": history_summary,
    }


def history_search_data(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    query = query.strip()
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    like = f"%{query.lower()}%"
    if query:
        watch_where = """
            WHERE lower(
              hr.title || ' ' ||
              hr.channel || ' ' ||
              hr.video_id || ' ' ||
              COALESCE(vm.title, '') || ' ' ||
              COALESCE(vmc.title, '') || ' ' ||
              COALESCE(hc.title, '') || ' ' ||
              COALESCE(vm.upload_date, '') || ' ' ||
              COALESCE(vm.description, '')
            ) LIKE ?
        """
        watch_params: list[Any] = [like]
    else:
        watch_where = ""
        watch_params = []
    filtered_watch_rows = int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM history_reconciled hr
            LEFT JOIN video_metadata vm ON vm.video_id = hr.video_id
            LEFT JOIN channels vmc ON vmc.channel_id = vm.channel_id
            LEFT JOIN channels hc ON hc.channel_id = hr.channel_id
            {watch_where}
            """,
            watch_params,
        ).fetchone()["count"]
    )
    watch_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT hr.reconciled_id,
                   COALESCE(NULLIF(hr.takeout_history_key, ''), hr.youtube_history_key) AS history_key,
                   hr.youtube_ordinal AS position,
                   'Watched' AS action,
                   hr.video_id,
                   hr.title,
                   hr.url,
                   hr.channel,
                   hr.channel_url,
                   hr.best_watch_time AS watched_at,
                   hr.watch_date,
                   hr.source_quality,
                   hr.match_confidence,
                   hr.match_notes,
                   hr.youtube_history_key,
                   hr.youtube_ordinal,
                   hr.takeout_history_key,
                   hr.takeout_row_hash,
                   hr.imported_at,
                   COALESCE(vm.title, '') AS metadata_title,
                   COALESCE(vm.description, '') AS metadata_description,
                   COALESCE(NULLIF(vmc.title, ''), NULLIF(hc.title, ''), hr.channel, '') AS metadata_channel,
                   COALESCE(NULLIF(vmc.url, ''), NULLIF(hc.url, ''), hr.channel_url, '') AS metadata_channel_url,
                   COALESCE(vm.duration_text, '') AS metadata_duration,
                   COALESCE(vm.thumbnail_path, '') AS metadata_thumbnail_path,
                   COALESCE(NULLIF(vmc.thumbnail_path, ''), NULLIF(hc.thumbnail_path, ''), '') AS metadata_channel_thumbnail_path,
                   COALESCE(vm.fetch_status, '') AS metadata_fetch_status
            FROM history_reconciled hr
            LEFT JOIN video_metadata vm ON vm.video_id = hr.video_id
            LEFT JOIN channels vmc ON vmc.channel_id = vm.channel_id
            LEFT JOIN channels hc ON hc.channel_id = hr.channel_id
            {watch_where}
            ORDER BY CASE WHEN hr.best_watch_time = '' THEN 1 ELSE 0 END,
                     hr.best_watch_time DESC,
                     hr.imported_at DESC,
                     position
            LIMIT ? OFFSET ?
            """,
            [*watch_params, limit, offset],
        )
    ]
    total = dict(
        conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM history_reconciled) AS watch_rows,
              (SELECT COUNT(DISTINCT video_id) FROM history_reconciled WHERE video_id <> '') AS distinct_watch_videos
            """
        ).fetchone()
    )
    return {
        "query": query,
        "limit": limit,
        "offset": offset,
        "watch": watch_rows,
        "totals": {**total, "filtered_watch_rows": filtered_watch_rows},
    }


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YT Library</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f4ef;
      --panel: #fffdf8;
      --ink: #25231f;
      --muted: #6b655c;
      --line: #ded6cb;
      --accent: #0b7285;
      --accent-soft: #d7f2f4;
      --warn: #9a3412;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #161513;
        --panel: #211f1b;
        --ink: #f4efe7;
        --muted: #b8afa3;
        --line: #39342d;
        --accent: #67d8e6;
        --accent-soft: #173b40;
        --warn: #f4a261;
      }
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--ink); }
    .app { display: grid; grid-template-columns: 280px 1fr; min-height: 100vh; }
    aside { border-right: 1px solid var(--line); background: var(--panel); padding: 18px 14px; position: sticky; top: 0; height: 100vh; overflow: auto; }
    main { padding: 24px; }
    h1 { font-size: 20px; margin: 0 0 14px; }
    .home-title { color: var(--ink); text-decoration: none; display: inline-block; }
    .home-title:hover { color: var(--ink); text-decoration: none; }
    .search { width: 100%; border: 1px solid var(--line); background: var(--bg); color: var(--ink); border-radius: 6px; padding: 10px 12px; font: inherit; margin-bottom: 16px; }
    .filters { border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); padding: 10px 0; margin-bottom: 14px; display: grid; gap: 7px; }
    .filter-title { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
    .filter { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
    .filter input { accent-color: var(--accent); }
    .group { width: 100%; border: 0; background: transparent; color: var(--ink); display: flex; align-items: center; justify-content: space-between; padding: 8px 10px; margin: 2px 0; border-radius: 6px; cursor: pointer; text-align: left; font: inherit; }
    .group:hover, .group.active { background: var(--accent-soft); }
    .group.child { padding-left: 28px; color: var(--muted); }
    .count { color: var(--muted); font-size: 12px; margin-left: 8px; }
    .toolbar { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
    .title { font-size: 28px; line-height: 1.1; margin: 0; }
    .meta { color: var(--muted); font-size: 14px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 14px; }
    .card { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); overflow: hidden; min-width: 0; }
    .thumb-link { display: block; }
    .thumb { display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover; background: linear-gradient(135deg, #24424a, #d98948); }
    .body { padding: 11px 12px 13px; }
    .title-row { display: flex; align-items: flex-start; gap: 8px; }
    .playlist-title { display: block; color: var(--ink); font-weight: 650; line-height: 1.25; text-decoration: none; overflow-wrap: anywhere; }
    .playlist-title:hover { color: var(--accent); }
    .title-row .playlist-title { flex: 1; min-width: 0; }
    .external-link { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; flex: 0 0 auto; border: 1px solid var(--line); border-radius: 6px; color: var(--accent); text-decoration: none; }
    .external-link:hover { background: var(--accent-soft); }
    .external-link svg { width: 15px; height: 15px; }
    .details { color: var(--muted); font-size: 13px; margin-top: 7px; display: flex; flex-wrap: wrap; gap: 6px; }
    .channel-avatar { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; vertical-align: middle; }
    .creator-link { color: var(--muted); text-decoration: none; display: inline-flex; align-items: center; }
    .creator-link:hover { color: var(--accent); text-decoration: underline; }
    .description { color: var(--muted); font-size: 13px; line-height: 1.35; margin-top: 8px; max-height: 5.4em; overflow: hidden; }
    .badge { color: var(--warn); font-weight: 650; }
    .refresh { border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 6px; padding: 7px 10px; font: inherit; cursor: pointer; }
    .refresh:hover { background: var(--accent-soft); }
    .top-link { display: inline-block; color: var(--accent); text-decoration: none; margin: -4px 0 14px; font-size: 13px; }
    .top-link:hover { text-decoration: underline; }
    .video-title { color: var(--ink); font-weight: 650; line-height: 1.25; overflow-wrap: anywhere; }
    .playlist-link { color: var(--accent); text-decoration: none; overflow-wrap: anywhere; }
    .playlist-link:hover { text-decoration: underline; }
    .result-kind { color: var(--muted); font-size: 12px; text-transform: uppercase; margin-bottom: 5px; }
    .position { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    .status { color: var(--warn); }
    .empty { color: var(--muted); padding: 36px 0; }
    @media (max-width: 760px) {
      .app { grid-template-columns: 1fr; }
      aside { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--line); }
      main { padding: 18px 14px; }
      .toolbar { display: block; }
      .title { font-size: 24px; margin-bottom: 6px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <h1><a class="home-title" href="/">YT Library</a></h1>
      <a class="top-link" href="/history">History search</a>
      <input id="search" class="search" type="search" placeholder="Search everything" autocomplete="off">
      <div class="filters" aria-label="Search filters">
        <div class="filter-title">Search In</div>
        <label class="filter"><input class="search-filter" type="checkbox" data-filter="playlists" checked> Playlists</label>
        <label class="filter"><input class="search-filter" type="checkbox" data-filter="videos" checked> Video titles</label>
        <label class="filter"><input class="search-filter" type="checkbox" data-filter="descriptions" checked> Descriptions</label>
        <label class="filter"><input class="search-filter" type="checkbox" data-filter="hidden" checked> Hidden/missing</label>
      </div>
      <nav id="groups"></nav>
    </aside>
    <main>
      <div class="toolbar">
        <h2 id="view-title" class="title">All playlists</h2>
        <div class="details">
          <button id="refresh" class="refresh" type="button">Refresh</button>
          <div id="view-meta" class="meta"></div>
        </div>
      </div>
      <section id="grid" class="grid"></section>
      <div id="empty" class="empty" hidden>No playlists match.</div>
    </main>
  </div>
  <script>
    let data = null;
    let playlists = new Map();
    let memberships = new Map();
    let children = new Map();
    let playlistVideos = new Map();

    let selected = '';
    const search = document.getElementById('search');
    const refresh = document.getElementById('refresh');
    const groupsEl = document.getElementById('groups');
    const searchFilters = [...document.querySelectorAll('.search-filter')];
    const grid = document.getElementById('grid');
    const empty = document.getElementById('empty');
    const title = document.getElementById('view-title');
    const meta = document.getElementById('view-meta');

    async function loadData() {
      refresh.disabled = true;
      refresh.textContent = 'Refreshing';
      const response = await fetch('/api/data', { cache: 'no-store' });
      if (!response.ok) throw new Error(`Data refresh failed: ${response.status}`);
      data = await response.json();
      playlists = new Map(data.playlists.map(p => [p.playlist_id, p]));
      playlistVideos = new Map();
      for (const video of data.playlistVideos || []) {
        if (!playlistVideos.has(video.playlist_id)) playlistVideos.set(video.playlist_id, []);
        playlistVideos.get(video.playlist_id).push(video);
      }
      memberships = new Map();
      for (const item of data.memberships) {
        if (!memberships.has(item.group_key)) memberships.set(item.group_key, []);
        memberships.get(item.group_key).push(item.playlist_id);
      }
      children = new Map();
      for (const group of data.groups) {
        const parent = group.parent_key || '';
        if (!children.has(parent)) children.set(parent, []);
        children.get(parent).push(group);
      }
      selected = selectionFromHash();
      renderGroups();
      render();
      refresh.disabled = false;
      refresh.textContent = 'Refresh';
    }

    function playlistSelection(playlistId) {
      return `__playlist__:${playlistId}`;
    }

    function localPlaylistHref(playlistId) {
      return `#playlist=${encodeURIComponent(playlistId)}`;
    }

    function selectionFromHash() {
      const hash = window.location.hash || '';
      if (hash.startsWith('#playlist=')) {
        const playlistId = decodeURIComponent(hash.slice('#playlist='.length));
        if (playlistId) return playlistSelection(playlistId);
      }
      return selected.startsWith('__playlist__:') ? '' : selected;
    }

    function setSelected(value) {
      selected = value;
      if (value.startsWith('__playlist__:')) {
        search.value = '';
        const playlistId = value.slice('__playlist__:'.length);
        if (window.location.hash !== localPlaylistHref(playlistId)) {
          window.location.hash = localPlaylistHref(playlistId);
          return;
        }
      } else if (window.location.hash) {
        history.pushState('', document.title, window.location.pathname + window.location.search);
      }
      renderGroups();
      render();
    }

    function groupCount(groupKey) {
      const own = memberships.get(groupKey) || [];
      const nested = (children.get(groupKey) || []).flatMap(child => memberships.get(child.group_key) || []);
      return new Set([...own, ...nested]).size;
    }

    function groupPlaylistIds(groupKey) {
      if (!groupKey) {
        return data.playlists.map(playlist => playlist.playlist_id);
      }
      const ids = [];
      for (const id of memberships.get(groupKey) || []) ids.push(id);
      for (const child of children.get(groupKey) || []) {
        for (const id of memberships.get(child.group_key) || []) ids.push(id);
      }
      return [...new Set(ids)];
    }

    function activeSearchFilters() {
      return new Set(searchFilters.filter(input => input.checked).map(input => input.dataset.filter));
    }

    function includesQuery(value, query) {
      return String(value || '').toLowerCase().includes(query);
    }

    function usefulMetadataTitle(video) {
      const title = String(video.metadata_title || '').trim();
      if (!title || title === '- YouTube' || title === 'YouTube') return '';
      return title;
    }

    function displayVideoTitle(video) {
      return usefulMetadataTitle(video) || video.title || video.video_id;
    }

    function displayVideoChannel(video) {
      return video.metadata_channel || video.channel || '';
    }

    function displayVideoChannelUrl(video) {
      return video.metadata_channel_url || '';
    }

    function displayVideoDuration(video) {
      return video.metadata_duration || video.duration_text || '';
    }

    function sourceQualityLabel(video) {
      const labels = {
        inferred_hidden_slot: 'restored from Takeout',
        ambiguous_hidden_slot: 'hidden slot',
        ambiguous_hidden_candidate: 'Takeout candidate',
        current_unknown: 'current'
      };
      return labels[video.source_quality] || '';
    }

    function unavailableLabel(video) {
      if (!video.is_playable) return video.availability || 'Hidden';
      const status = String(video.recovered_status || '');
      if (status === 'NOT_FOUND' || status.startsWith('DELETED_')) return 'Unavailable';
      const title = String(video.title || '').trim().toLowerCase().replace(/^[\\[\\(]+|[\\]\\)]+$/g, '');
      if (title === 'private video' || title === 'deleted video') return video.title || 'Unavailable';
      return '';
    }

    function archivarixStatusLabel(video) {
      const status = String(video.recovered_status || '');
      if (status === 'NOT_FOUND' || status.startsWith('DELETED_')) return `Archivarix: ${status}`;
      return '';
    }

    function archivarixVideoUrl(video) {
      return video.video_id ? `https://tube.archivarix.net/?q=${encodeURIComponent(video.video_id)}` : '';
    }

    function shouldShowArchivarixLink(video) {
      return Boolean(video.video_id && (unavailableLabel(video) || archivarixStatusLabel(video)));
    }

    function archivarixLinkHtml(video) {
      const url = archivarixVideoUrl(video);
      if (!url || !shouldShowArchivarixLink(video)) return '';
      return `<a class="playlist-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">Archivarix</a>`;
    }

    function creatorAvatarHtml(path, url) {
      if (!path) return '';
      const img = `<img class="channel-avatar" src="/${escapeHtml(path)}" alt="">`;
      return url ? `<a class="creator-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${img}</a>` : img;
    }

    function creatorNameHtml(name, url) {
      if (!name) return '';
      return url
        ? `<a class="creator-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(name)}</a>`
        : `<span>${escapeHtml(name)}</span>`;
    }

    function buildOmniResults(query) {
      const filters = activeSearchFilters();
      const results = [];
      const seenVideoKeys = new Set();
      if (filters.has('playlists')) {
        for (const playlist of data.playlists) {
          const haystack = `${playlist.title} ${playlist.owner} ${playlist.description} ${playlist.playlist_id}`;
          if (includesQuery(haystack, query)) {
            results.push({ kind: 'playlist', score: includesQuery(playlist.title, query) ? 0 : 2, item: playlist });
          }
        }
      }
      if (filters.has('videos') || filters.has('descriptions') || filters.has('hidden')) {
        for (const video of data.playlistVideos || []) {
          const titleHit = filters.has('videos') && includesQuery(`${displayVideoTitle(video)} ${displayVideoChannel(video)} ${video.video_id} ${video.playlist_title}`, query);
          const descriptionHit = filters.has('descriptions') && includesQuery(video.metadata_description, query);
          const hiddenHit = filters.has('hidden') && !video.is_playable && includesQuery(`${displayVideoTitle(video)} ${video.availability} ${video.video_id} ${video.playlist_title}`, query);
          if (titleHit || descriptionHit || hiddenHit) {
            const key = `${video.playlist_id}:${video.position}:${video.video_id}`;
            if (!seenVideoKeys.has(key)) {
              seenVideoKeys.add(key);
              results.push({ kind: 'video', score: titleHit ? 1 : 3, item: video, matchedDescription: descriptionHit && !titleHit });
            }
          }
        }
      }
      if (filters.has('hidden')) {
        for (const video of data.snapshotMissing || []) {
          const haystack = `${video.video_id} ${video.playlist_title} ${video.current_title} ${video.recovered_title} ${video.recovered_description} ${video.recovered_channel} ${video.recovered_status}`;
          if (includesQuery(haystack, query)) {
            results.push({ kind: 'missing', score: 4, item: video });
          }
        }
      }
      return results.sort((a, b) => a.score - b.score).slice(0, 500);
    }

    function buttonFor(group, child=false) {
      const button = document.createElement('button');
      button.className = `group ${child ? 'child' : ''}`;
      button.dataset.key = group.group_key;
      button.innerHTML = `<span>${escapeHtml(group.name)}</span><span class="count">${groupCount(group.group_key)}</span>`;
      button.addEventListener('click', () => setSelected(group.group_key));
      return button;
    }

    function renderGroups() {
      if (!data) return;
      groupsEl.replaceChildren();
      const all = document.createElement('button');
      all.className = 'group';
      all.dataset.key = '';
      all.innerHTML = `<span>All playlists</span><span class="count">${data.playlists.length}</span>`;
      all.addEventListener('click', () => setSelected(''));
      groupsEl.appendChild(all);
      const hidden = document.createElement('button');
      hidden.className = 'group';
      hidden.dataset.key = '__hidden__';
      hidden.innerHTML = `<span>Hidden videos</span><span class="count">${data.hiddenVideos.length}</span>`;
      hidden.addEventListener('click', () => setSelected('__hidden__'));
      groupsEl.appendChild(hidden);
      const hiddenPlaylists = data.playlists.filter(playlist => (playlist.hidden_count || 0) > 0);
      const hiddenPlaylistButton = document.createElement('button');
      hiddenPlaylistButton.className = 'group';
      hiddenPlaylistButton.dataset.key = '__hidden_playlists__';
      hiddenPlaylistButton.innerHTML = `<span>Playlists with hidden</span><span class="count">${hiddenPlaylists.length}</span>`;
      hiddenPlaylistButton.addEventListener('click', () => setSelected('__hidden_playlists__'));
      groupsEl.appendChild(hiddenPlaylistButton);
      const snapshot = document.createElement('button');
      snapshot.className = 'group';
      snapshot.dataset.key = '__snapshot__';
      snapshot.innerHTML = `<span>Takeout snapshot</span><span class="count">${data.snapshotPlaylists.length}</span>`;
      snapshot.addEventListener('click', () => setSelected('__snapshot__'));
      groupsEl.appendChild(snapshot);
      const missing = document.createElement('button');
      missing.className = 'group';
      missing.dataset.key = '__snapshot_missing__';
      missing.innerHTML = `<span>Snapshot missing</span><span class="count">${data.snapshotMissing.length}</span>`;
      missing.addEventListener('click', () => setSelected('__snapshot_missing__'));
      groupsEl.appendChild(missing);
      const likelyHidden = document.createElement('button');
      likelyHidden.className = 'group';
      likelyHidden.dataset.key = '__snapshot_likely_hidden__';
      likelyHidden.innerHTML = `<span>Likely hidden IDs</span><span class="count">${data.snapshotLikelyHidden.length}</span>`;
      likelyHidden.addEventListener('click', () => setSelected('__snapshot_likely_hidden__'));
      groupsEl.appendChild(likelyHidden);
      for (const group of children.get('') || []) {
        groupsEl.appendChild(buttonFor(group));
        for (const child of children.get(group.group_key) || []) {
          groupsEl.appendChild(buttonFor(child, true));
        }
      }
    }

    function render() {
      if (!data) {
        title.textContent = 'Loading';
        meta.textContent = '';
        return;
      }
      const query = search.value.trim().toLowerCase();
      empty.textContent = 'No playlists match.';
      for (const button of groupsEl.querySelectorAll('.group')) {
        button.classList.toggle('active', button.dataset.key === selected);
      }
      if (query) {
        const rows = buildOmniResults(query);
        title.textContent = 'Search results';
        meta.textContent = `${rows.length} shown`;
        grid.replaceChildren(...rows.map(searchResultCardFor));
        empty.textContent = 'No results match.';
        empty.hidden = rows.length !== 0;
        return;
      }
      if (selected.startsWith('__playlist__:')) {
        const playlistId = selected.slice('__playlist__:'.length);
        const playlist = playlists.get(playlistId);
        if (!playlist) {
          title.textContent = 'Playlist not found';
          meta.textContent = playlistId;
          grid.replaceChildren();
          empty.hidden = false;
          return;
        }
        const rows = (playlistVideos.get(playlistId) || []).filter(video => {
          const haystack = `${video.title} ${video.channel} ${video.availability} ${video.video_id}`.toLowerCase();
          return !query || haystack.includes(query);
        });
        title.textContent = playlist.title;
        meta.innerHTML = `
          <a class="playlist-link" href="${playlist.url}" target="_blank" rel="noreferrer">YouTube</a>
          <span>${rows.length} videos</span>
          ${playlist.hidden_count ? `<span class="badge">${playlist.hidden_count} hidden</span>` : ''}
        `;
        grid.replaceChildren(...rows.map(playlistVideoCardFor));
        empty.hidden = rows.length !== 0;
        empty.textContent = playlist.scanned_at ? 'No videos match.' : 'This playlist has not been scanned yet.';
        return;
      }
      if (selected === '__hidden__') {
        title.textContent = 'Hidden videos';
        const rows = data.hiddenVideos.filter(video => {
          const haystack = `${video.title} ${video.channel} ${video.availability} ${video.playlist_title} ${video.video_id}`.toLowerCase();
          return !query || haystack.includes(query);
        });
        meta.textContent = `${rows.length} shown`;
        grid.replaceChildren(...rows.map(hiddenVideoCardFor));
        empty.hidden = rows.length !== 0;
        return;
      }
      if (selected === '__hidden_playlists__') {
        title.textContent = 'Playlists with hidden videos';
        const rows = data.playlists
          .filter(playlist => (playlist.hidden_count || 0) > 0)
          .filter(playlist => {
            const haystack = `${playlist.title} ${playlist.owner} ${playlist.description} ${playlist.playlist_id}`.toLowerCase();
            return !query || haystack.includes(query);
          })
          .sort((a, b) =>
            (b.hidden_count || 0) - (a.hidden_count || 0)
            || a.title.localeCompare(b.title, undefined, { sensitivity: 'base' })
          );
        meta.textContent = `${rows.length} playlists`;
        grid.replaceChildren(...rows.map(cardFor));
        empty.hidden = rows.length !== 0;
        return;
      }
      if (selected === '__snapshot__') {
        const label = data.snapshots[0]?.label || 'Takeout snapshot';
        title.textContent = label;
        const rows = data.snapshotPlaylists.filter(playlist => {
          const haystack = `${playlist.title} ${playlist.playlist_id} ${playlist.visibility}`.toLowerCase();
          return !query || haystack.includes(query);
        });
        meta.textContent = `${rows.length} playlists`;
        grid.replaceChildren(...rows.map(snapshotPlaylistCardFor));
        empty.hidden = rows.length !== 0;
        return;
      }
      if (selected === '__snapshot_missing__') {
        title.textContent = 'Snapshot missing';
        const rows = data.snapshotMissing.filter(video => {
          const haystack = `${video.video_id} ${video.playlist_title} ${video.current_title} ${video.added_at}`.toLowerCase();
          return !query || haystack.includes(query);
        });
        meta.textContent = `${rows.length} video IDs`;
        grid.replaceChildren(...rows.map(snapshotMissingCardFor));
        empty.hidden = rows.length !== 0;
        return;
      }
      if (selected === '__snapshot_likely_hidden__') {
        title.textContent = 'Likely hidden IDs';
        const rows = data.snapshotLikelyHidden.filter(video => {
          const haystack = `${video.video_id} ${video.playlist_title} ${video.current_title} ${video.added_at} ${video.recovered_title}`.toLowerCase();
          return !query || haystack.includes(query);
        });
        meta.textContent = `${rows.length} video IDs`;
        grid.replaceChildren(...rows.map(snapshotMissingCardFor));
        empty.hidden = rows.length !== 0;
        return;
      }
      const group = data.groups.find(g => g.group_key === selected);
      title.textContent = group ? group.name : 'All playlists';
      const ids = groupPlaylistIds(selected);
      const rows = ids.map(id => playlists.get(id)).filter(Boolean).filter(p => {
        const haystack = `${p.title} ${p.owner} ${p.description} ${p.playlist_id}`.toLowerCase();
        return !query || haystack.includes(query);
      });
      meta.textContent = `${rows.length} shown`;
      grid.replaceChildren(...rows.map(cardFor));
      empty.hidden = rows.length !== 0;
    }

    function cardFor(playlist) {
      const article = document.createElement('article');
      article.className = 'card';
      const localHref = localPlaylistHref(playlist.playlist_id);
      const img = document.createElement('img');
      img.className = 'thumb';
      img.loading = 'lazy';
      img.alt = '';
      img.src = playlist.thumbnail_path ? `/${playlist.thumbnail_path}` : '';
      const thumbLink = document.createElement('a');
      thumbLink.className = 'thumb-link';
      thumbLink.href = localHref;
      thumbLink.append(img);
      const body = document.createElement('div');
      body.className = 'body';
      body.innerHTML = `
        <div class="title-row">
          <a class="playlist-title" href="${localHref}">${escapeHtml(playlist.title)}</a>
          <a class="external-link" href="${playlist.url}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(playlist.title)} on YouTube">
            ${externalLinkSvg()}
          </a>
        </div>
        <div class="details">
          ${playlist.video_count_text ? `<span>${escapeHtml(playlist.video_count_text)}</span>` : ''}
          ${playlist.scanned_video_count ? `<span>${playlist.scanned_video_count} scanned</span>` : ''}
          ${playlist.hidden_count ? `<span class="badge">${playlist.hidden_count} hidden</span>` : ''}
          ${playlist.owner ? `<span>${escapeHtml(playlist.owner)}</span>` : ''}
          ${playlist.fetch_status === 'error' ? '<span class="status">Fetch failed</span>' : ''}
        </div>
      `;
      article.append(thumbLink, body);
      return article;
    }

    function playlistVideoCardFor(video) {
      const article = document.createElement('article');
      article.className = 'card';
      if (video.metadata_thumbnail_path) {
        const img = document.createElement('img');
        img.className = 'thumb';
        img.loading = 'lazy';
        img.alt = '';
        img.src = `/${video.metadata_thumbnail_path}`;
        article.append(img);
      }
      const body = document.createElement('div');
      body.className = 'body';
      const watchUrl = video.video_id ? `https://www.youtube.com/watch?v=${encodeURIComponent(video.video_id)}&list=${encodeURIComponent(video.playlist_id)}` : '';
      const channelName = displayVideoChannel(video);
      const channelUrl = displayVideoChannelUrl(video);
      body.innerHTML = `
        <div class="position">#${video.position}</div>
        ${watchUrl
          ? `<a class="playlist-title" href="${watchUrl}" target="_blank" rel="noreferrer">${escapeHtml(displayVideoTitle(video))}</a>`
          : `<div class="video-title">${escapeHtml(displayVideoTitle(video))}</div>`}
        <div class="details">
          ${unavailableLabel(video) ? `<span class="badge">${escapeHtml(unavailableLabel(video))}</span>` : ''}
          ${archivarixStatusLabel(video) ? `<span class="badge">${escapeHtml(archivarixStatusLabel(video))}</span>` : ''}
          ${sourceQualityLabel(video) ? `<span class="badge">${escapeHtml(sourceQualityLabel(video))}</span>` : ''}
          ${displayVideoDuration(video) ? `<span>${escapeHtml(displayVideoDuration(video))}</span>` : ''}
          ${creatorAvatarHtml(video.metadata_channel_thumbnail_path, channelUrl)}
          ${creatorNameHtml(channelName, channelUrl)}
          ${video.video_id ? `<span>${escapeHtml(video.video_id)}</span>` : ''}
          ${archivarixLinkHtml(video)}
        </div>
        ${video.metadata_description ? `<div class="description">${escapeHtml(video.metadata_description)}</div>` : ''}
      `;
      article.append(body);
      return article;
    }

    function searchResultCardFor(result) {
      if (result.kind === 'playlist') {
        return cardFor(result.item);
      }
      if (result.kind === 'missing') {
        return snapshotMissingCardFor(result.item);
      }
      const video = result.item;
      const article = playlistVideoCardFor(video);
      const body = article.querySelector('.body');
      if (body) {
        const kind = document.createElement('div');
        kind.className = 'result-kind';
        kind.textContent = result.matchedDescription ? 'Description match' : 'Video';
        body.prepend(kind);
        const source = document.createElement('div');
        source.className = 'details';
        source.innerHTML = `<a class="playlist-link" href="${localPlaylistHref(video.playlist_id)}">${escapeHtml(video.playlist_title)}</a>`;
        body.append(source);
      }
      return article;
    }

    function externalLinkSvg() {
      return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7"></path><path d="M8 7h9v9"></path><path d="M7 7H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-2"></path></svg>';
    }

    function hiddenVideoCardFor(video) {
      const article = document.createElement('article');
      article.className = 'card';
      const body = document.createElement('div');
      body.className = 'body';
      body.innerHTML = `
        <div class="position">#${video.position}</div>
        <div class="video-title">${escapeHtml(video.title)}</div>
        <div class="details">
          ${video.availability ? `<span class="badge">${escapeHtml(video.availability)}</span>` : '<span class="badge">Hidden</span>'}
          ${video.channel ? `<span>${escapeHtml(video.channel)}</span>` : ''}
          ${video.video_id ? `<span>${escapeHtml(video.video_id)}</span>` : ''}
        </div>
        <div class="details">
          <a class="playlist-link" href="${video.playlist_url}" target="_blank" rel="noreferrer">${escapeHtml(video.playlist_title)}</a>
        </div>
      `;
      article.append(body);
      return article;
    }

    function candidateCardFor(video) {
      const article = document.createElement('article');
      article.className = 'card';
      const img = document.createElement('img');
      img.className = 'thumb';
      img.loading = 'lazy';
      img.alt = '';
      img.src = video.thumbnail_path ? `/${video.thumbnail_path}` : '';
      const body = document.createElement('div');
      body.className = 'body';
      const watchUrl = `https://www.youtube.com/watch?v=${encodeURIComponent(video.video_id)}`;
      body.innerHTML = `
        <a class="playlist-title" href="${watchUrl}" target="_blank" rel="noreferrer">${escapeHtml(video.title)}</a>
        <div class="details">
          <span class="badge">${escapeHtml(video.status)}</span>
          ${video.duration_text ? `<span>${escapeHtml(video.duration_text)}</span>` : ''}
          ${video.upload_date ? `<span>${escapeHtml(video.upload_date)}</span>` : ''}
          ${video.channel ? `<span>${escapeHtml(video.channel)}</span>` : ''}
          <span>${escapeHtml(video.video_id)}</span>
        </div>
        <div class="details">
          <a class="playlist-link" href="${video.playlist_url}" target="_blank" rel="noreferrer">${escapeHtml(video.playlist_title)}</a>
        </div>
      `;
      article.append(img, body);
      return article;
    }

    function snapshotPlaylistCardFor(playlist) {
      const article = document.createElement('article');
      article.className = 'card';
      const body = document.createElement('div');
      body.className = 'body';
      const currentName = playlist.current_title || '';
      body.innerHTML = `
        <div class="playlist-title">${escapeHtml(playlist.title)}</div>
        <div class="details">
          <span>${playlist.video_count} videos</span>
          ${playlist.current_video_count ? `<span>${playlist.current_video_count} current</span>` : ''}
          ${playlist.current_hidden_count ? `<span class="badge">${playlist.current_hidden_count} hidden now</span>` : ''}
          ${playlist.visibility ? `<span>${escapeHtml(playlist.visibility)}</span>` : ''}
        </div>
        <div class="details">
          <span>${escapeHtml(playlist.playlist_id)}</span>
          ${currentName && currentName !== playlist.title ? `<span>Current: ${escapeHtml(currentName)}</span>` : ''}
        </div>
      `;
      article.append(body);
      return article;
    }

    function snapshotMissingCardFor(video) {
      const article = document.createElement('article');
      article.className = 'card';
      if (video.recovered_thumbnail_path) {
        const img = document.createElement('img');
        img.className = 'thumb';
        img.loading = 'lazy';
        img.alt = '';
        img.src = `/${video.recovered_thumbnail_path}`;
        article.append(img);
      }
      const body = document.createElement('div');
      body.className = 'body';
      const watchUrl = `https://www.youtube.com/watch?v=${encodeURIComponent(video.video_id)}`;
      const archivarixUrl = `https://tube.archivarix.net/?q=${encodeURIComponent(video.video_id)}`;
      const displayTitle = video.recovered_title || video.video_id;
      body.innerHTML = `
        <div class="position">#${video.position}</div>
        <a class="playlist-title" href="${watchUrl}" target="_blank" rel="noreferrer">${escapeHtml(displayTitle)}</a>
        <div class="details">
          ${video.recovered_status ? `<span class="badge">${escapeHtml(video.recovered_status)}</span>` : ''}
          ${video.recovered_duration ? `<span>${escapeHtml(video.recovered_duration)}</span>` : ''}
          ${video.recovered_upload_date ? `<span>${escapeHtml(video.recovered_upload_date)}</span>` : ''}
          ${video.recovered_channel ? `<span>${escapeHtml(video.recovered_channel)}</span>` : ''}
          ${video.added_at ? `<span>Added ${escapeHtml(video.added_at.slice(0, 10))}</span>` : ''}
          <span>${escapeHtml(video.video_id)}</span>
          <a class="playlist-link" href="${archivarixUrl}" target="_blank" rel="noreferrer">Archivarix</a>
        </div>
        ${video.recovered_description ? `<div class="description">${escapeHtml(video.recovered_description)}</div>` : ''}
        <div class="details">
          <a class="playlist-link" href="${video.playlist_url}" target="_blank" rel="noreferrer">${escapeHtml(video.current_title || video.playlist_title)}</a>
        </div>
      `;
      article.append(body);
      return article;
    }

    function escapeHtml(value) {
      return String(value || '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    search.addEventListener('input', render);
    for (const input of searchFilters) input.addEventListener('change', render);
    window.addEventListener('hashchange', () => {
      selected = selectionFromHash();
      if (selected.startsWith('__playlist__:')) search.value = '';
      renderGroups();
      render();
    });
    refresh.addEventListener('click', () => loadData().catch(error => {
      meta.textContent = error.message;
      refresh.disabled = false;
      refresh.textContent = 'Refresh';
    }));
    loadData().catch(error => {
      title.textContent = 'Unable to load data';
      meta.textContent = error.message;
      refresh.disabled = false;
      refresh.textContent = 'Refresh';
    });
  </script>
</body>
</html>
"""


HISTORY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YT Library History</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f4ef;
      --panel: #fffdf8;
      --ink: #25231f;
      --muted: #6b655c;
      --line: #ded6cb;
      --accent: #0b7285;
      --accent-soft: #d7f2f4;
      --warn: #9a3412;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #161513;
        --panel: #211f1b;
        --ink: #f4efe7;
        --muted: #b8afa3;
        --line: #39342d;
        --accent: #67d8e6;
        --accent-soft: #173b40;
        --warn: #f4a261;
      }
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--ink); }
    .page { max-width: 1240px; margin: 0 auto; padding: 24px; }
    header { display: flex; align-items: baseline; justify-content: space-between; gap: 18px; margin-bottom: 18px; }
    h1 { font-size: 30px; line-height: 1.1; margin: 0; }
    nav { display: flex; gap: 12px; flex-wrap: wrap; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .searchbar { display: grid; grid-template-columns: 1fr auto; gap: 10px; margin-bottom: 12px; }
    input[type="search"] { width: 100%; border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 6px; padding: 11px 12px; font: inherit; }
    button { border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 6px; padding: 0 14px; font: inherit; cursor: pointer; }
    button:hover { background: var(--accent-soft); }
    .meta { color: var(--muted); font-size: 14px; margin-bottom: 18px; display: flex; gap: 10px; flex-wrap: wrap; }
    .pager { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 0 14px; color: var(--muted); font-size: 14px; }
    .pager-controls { display: flex; gap: 8px; }
    .pager button { min-height: 36px; }
    .pager button:disabled { cursor: default; opacity: .5; background: var(--panel); }
    .tabs { display: flex; gap: 8px; margin-bottom: 14px; }
    .tab { padding: 8px 11px; border: 1px solid var(--line); border-radius: 6px; color: var(--muted); cursor: pointer; }
    .tab.active { background: var(--accent-soft); color: var(--ink); }
    .grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; }
    .card { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); overflow: hidden; min-width: 0; display: grid; grid-template-columns: 220px minmax(0, 1fr); }
    .card.no-thumb .body { grid-column: 1 / -1; }
    .thumb { display: block; width: 100%; height: 100%; min-height: 124px; aspect-ratio: 16 / 9; object-fit: cover; background: linear-gradient(135deg, #24424a, #d98948); }
    .body { padding: 11px 12px 13px; }
    .title { display: block; color: var(--ink); font-weight: 650; line-height: 1.25; overflow-wrap: anywhere; }
    .details { color: var(--muted); font-size: 13px; margin-top: 7px; display: flex; flex-wrap: wrap; gap: 6px; }
    .channel-avatar { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; vertical-align: middle; }
    .creator-link { color: var(--muted); text-decoration: none; display: inline-flex; align-items: center; }
    .creator-link:hover { color: var(--accent); text-decoration: underline; }
    .description { color: var(--muted); font-size: 13px; line-height: 1.35; margin-top: 8px; max-height: 5.4em; overflow: hidden; }
    .badge { color: var(--warn); font-weight: 650; }
    .empty { color: var(--muted); padding: 36px 0; }
    @media (max-width: 720px) {
      .page { padding: 18px 14px; }
      header, .searchbar { display: block; }
      nav { margin-top: 10px; }
      button { margin-top: 8px; height: 40px; }
      .pager { align-items: flex-start; flex-direction: column; }
      .card { grid-template-columns: 1fr; }
      .thumb { height: auto; min-height: 0; }
    }
  </style>
</head>
<body>
  <div class="page">
    <header>
      <h1>YT Library History</h1>
      <nav>
        <a href="/">Playlists</a>
        <a href="/admin">Admin</a>
      </nav>
    </header>
    <div class="searchbar">
      <input id="search" type="search" placeholder="Search watch history and metadata descriptions" autocomplete="off" autofocus>
      <button id="refresh" type="button">Search</button>
    </div>
    <div id="meta" class="meta"></div>
    <div class="pager">
      <div id="pageInfo"></div>
      <div class="pager-controls">
        <button id="prevPage" type="button">Previous</button>
        <button id="nextPage" type="button">Next</button>
      </div>
    </div>
    <section id="results" class="grid"></section>
    <div id="empty" class="empty" hidden>No history matches.</div>
  </div>
  <script>
    const input = document.getElementById('search');
    const refresh = document.getElementById('refresh');
    const meta = document.getElementById('meta');
    const pageInfo = document.getElementById('pageInfo');
    const prevPage = document.getElementById('prevPage');
    const nextPage = document.getElementById('nextPage');
    const results = document.getElementById('results');
    const empty = document.getElementById('empty');
    let latest = { watch: [], totals: {} };
    const pageSize = 250;
    let offset = 0;
    let timer = null;
    let requestId = 0;

    function escapeHtml(value) {
      return String(value || '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    function displayTitle(row) {
      return row.metadata_title || row.title || row.video_id;
    }

    function creatorAvatarHtml(path, url) {
      if (!path) return '';
      const img = `<img class="channel-avatar" src="/${escapeHtml(path)}" alt="">`;
      return url ? `<a class="creator-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${img}</a>` : img;
    }

    function creatorNameHtml(name, url) {
      if (!name) return '';
      return url
        ? `<a class="creator-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(name)}</a>`
        : `<span>${escapeHtml(name)}</span>`;
    }

    function watchCard(row) {
      const article = document.createElement('article');
      article.className = row.metadata_thumbnail_path ? 'card' : 'card no-thumb';
      if (row.metadata_thumbnail_path) {
        const img = document.createElement('img');
        img.className = 'thumb';
        img.loading = 'lazy';
        img.alt = '';
        img.src = `/${row.metadata_thumbnail_path}`;
        article.append(img);
      }
      const body = document.createElement('div');
      body.className = 'body';
      const watchUrl = row.video_id ? `https://www.youtube.com/watch?v=${encodeURIComponent(row.video_id)}` : row.url;
      const channelName = row.metadata_channel || row.channel || '';
      const channelUrl = row.metadata_channel_url || row.channel_url || '';
      body.innerHTML = `
        <a class="title" href="${watchUrl}" target="_blank" rel="noreferrer">${escapeHtml(displayTitle(row))}</a>
        <div class="details">
          ${row.watched_at ? `<span>${escapeHtml(row.watched_at)}</span>` : ''}
          ${row.source_quality ? `<span class="badge">${escapeHtml(row.source_quality)}</span>` : ''}
          ${creatorAvatarHtml(row.metadata_channel_thumbnail_path, channelUrl)}
          ${creatorNameHtml(channelName, channelUrl)}
          ${row.video_id ? `<span>${escapeHtml(row.video_id)}</span>` : ''}
          ${row.metadata_fetch_status === 'error' ? '<span class="badge">metadata error</span>' : ''}
        </div>
        ${row.metadata_description ? `<div class="description">${escapeHtml(row.metadata_description)}</div>` : ''}
      `;
      article.append(body);
      return article;
    }

    function render() {
      const rows = latest.watch || [];
      results.className = 'grid';
      results.replaceChildren(...rows.map(watchCard));
      empty.hidden = rows.length !== 0;
      const totals = latest.totals || {};
      const filtered = totals.filtered_watch_rows || 0;
      const start = filtered && rows.length ? (latest.offset || 0) + 1 : 0;
      const end = filtered && rows.length ? (latest.offset || 0) + rows.length : 0;
      meta.innerHTML = `
        <span>${latest.watch.length} watch results shown</span>
        <span>${filtered} matching watch rows</span>
        <span>${totals.watch_rows || 0} watch rows</span>
        <span>${totals.distinct_watch_videos || 0} distinct videos</span>
      `;
      pageInfo.textContent = filtered ? `${start}-${end} of ${filtered}` : '0 results';
      prevPage.disabled = (latest.offset || 0) <= 0;
      nextPage.disabled = ((latest.offset || 0) + rows.length) >= filtered;
    }

    async function load() {
      const myRequest = ++requestId;
      refresh.disabled = true;
      refresh.textContent = 'Searching';
      const params = new URLSearchParams({
        q: input.value.trim(),
        limit: String(pageSize),
        offset: String(offset)
      });
      const response = await fetch(`/api/history/search?${params}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`History search failed: ${response.status}`);
      const payload = await response.json();
      if (myRequest === requestId) {
        latest = payload;
        render();
      }
      refresh.disabled = false;
      refresh.textContent = 'Search';
    }

    function schedule() {
      clearTimeout(timer);
      offset = 0;
      timer = setTimeout(() => load().catch(error => {
        meta.textContent = error.message;
        refresh.disabled = false;
        refresh.textContent = 'Search';
      }), 250);
    }

    input.addEventListener('input', schedule);
    refresh.addEventListener('click', () => {
      offset = 0;
      load().catch(error => meta.textContent = error.message);
    });
    prevPage.addEventListener('click', () => {
      offset = Math.max(0, offset - pageSize);
      load().catch(error => meta.textContent = error.message);
    });
    nextPage.addEventListener('click', () => {
      offset += pageSize;
      load().catch(error => meta.textContent = error.message);
    });
    load().catch(error => {
      meta.textContent = error.message;
      refresh.disabled = false;
      refresh.textContent = 'Search';
    });
  </script>
</body>
</html>
"""


ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YT Library Admin</title>
  <style>
    :root {
      --bg: #f7f4ef;
      --panel: #fffaf2;
      --ink: #22201d;
      --muted: #6b655c;
      --line: #ded6cb;
      --accent: #0b7285;
      --accent-soft: #d7f2f4;
      --warn: #9a3412;
      --ok: #166534;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #161513;
        --panel: #211f1b;
        --ink: #f4efe7;
        --muted: #b8afa3;
        --line: #39342d;
        --accent: #67d8e6;
        --accent-soft: #173b40;
        --warn: #f4a261;
        --ok: #86efac;
      }
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--ink); }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; }
    header { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
    h1 { font-size: 28px; margin: 0; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 18px; }
    .panel { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: 14px; }
    .metric { color: var(--muted); font-size: 13px; }
    .value { font-size: 26px; font-weight: 700; margin-top: 4px; }
    .controls { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; margin: 12px 0 18px; }
    label { color: var(--muted); display: grid; gap: 5px; font-size: 13px; }
    input { border: 1px solid var(--line); background: var(--bg); color: var(--ink); border-radius: 6px; padding: 8px 10px; font: inherit; width: 120px; }
    label.checkbox { display: flex; flex-direction: row; align-items: center; gap: 7px; padding-bottom: 8px; }
    label.checkbox input { width: auto; }
    button { border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 6px; padding: 9px 12px; font: inherit; cursor: pointer; }
    button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    button:hover { background: var(--accent-soft); color: var(--ink); }
    .status { display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 14px; }
    .badge { border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; }
    .running { color: var(--ok); }
    .warn { color: var(--warn); }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { border-bottom: 1px solid var(--line); padding: 8px 6px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; }
    .time-col { width: 150px; }
    .level-col { width: 110px; }
    .video-col { width: 150px; }
    .time-cell { white-space: nowrap; }
    .logs { max-height: 430px; overflow: auto; }
    .message { overflow-wrap: anywhere; }
    @media (max-width: 700px) {
      main { padding: 16px; }
      header { display: block; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>YT Library Admin</h1>
      <nav><a href="/">Playlists</a> <a href="/history">History search</a></nav>
    </header>

    <section class="grid">
      <div class="panel"><div class="metric">Playlist video rows</div><div id="playlistRows" class="value">0</div></div>
      <div class="panel"><div class="metric">Distinct video IDs</div><div id="distinctVideos" class="value">0</div></div>
      <div class="panel"><div class="metric">History rows</div><div id="historyRows" class="value">0</div></div>
      <div class="panel"><div class="metric">History video IDs</div><div id="historyVideos" class="value">0</div></div>
      <div class="panel"><div class="metric">Fetched history rows</div><div id="liveHistoryRows" class="value">0</div></div>
      <div class="panel"><div class="metric">Playlist scan queue</div><div id="playlistQueueCount" class="value">0</div></div>
      <div class="panel"><div class="metric">Metadata queue</div><div id="metadataQueueCount" class="value">0</div></div>
      <div class="panel"><div class="metric">Placeholder recovery queue</div><div id="placeholderQueueCount" class="value">0</div></div>
      <div class="panel"><div class="metric">Playlist scanner</div><div id="playlistWorkerState" class="value">idle</div></div>
      <div class="panel"><div class="metric">Metadata worker</div><div id="metadataWorkerState" class="value">idle</div></div>
      <div class="panel"><div class="metric">History fetch</div><div id="liveHistoryWorkerState" class="value">idle</div></div>
      <div class="panel"><div class="metric">Placeholder recovery</div><div id="placeholderWorkerState" class="value">idle</div></div>
    </section>

    <section class="panel">
      <div class="controls">
        <label>Limit<input id="limit" type="number" min="0" step="1" value="10"></label>
        <label>Playlist delay<input id="playlistDelay" type="number" min="1" step="1" value="3"></label>
        <label>Metadata delay<input id="metadataDelay" type="number" min="1" step="1" value="12"></label>
        <label>Playlist stale days<input id="playlistStaleDays" type="number" min="0" step="1" value="7"></label>
        <label>Metadata stale days<input id="metadataStaleDays" type="number" min="0" step="1" value="30"></label>
        <label class="checkbox"><input id="force" type="checkbox">Refresh already fetched</label>
        <button id="scanPlaylists" class="primary" type="button">Scan playlists</button>
        <button id="fetchMetadata" class="primary" type="button">Fetch video metadata</button>
        <button id="startLiveHistory" class="primary" type="button">Fetch history</button>
        <button id="verifyLiveHistory" class="primary" type="button">Verify history</button>
        <button id="importTakeoutHistory" type="button">Import Takeout history</button>
        <button id="reconcileHistory" type="button">Reconcile history</button>
        <button id="reconcilePlaylists" type="button">Reconcile playlists</button>
        <button id="recoverPlaceholders" type="button">Recover deleted playlist videos</button>
        <button id="stopPlaylists" type="button">Stop playlist scan</button>
        <button id="stopMetadata" type="button">Stop metadata</button>
        <button id="stopLiveHistory" type="button">Stop history fetch</button>
        <button id="stopPlaceholders" type="button">Stop placeholder recovery</button>
        <button id="refresh" type="button">Refresh status</button>
      </div>
      <div id="playlistRunStatus" class="status"></div>
      <div id="metadataRunStatus" class="status" style="margin-top:8px"></div>
      <div id="liveHistoryRunStatus" class="status" style="margin-top:8px"></div>
      <div id="placeholderRunStatus" class="status" style="margin-top:8px"></div>
    </section>

    <section class="grid" id="metadataCounts"></section>

    <section class="panel logs">
      <table>
        <colgroup>
          <col class="time-col">
          <col class="level-col">
          <col class="video-col">
          <col>
        </colgroup>
        <thead><tr><th>Time</th><th>Level</th><th>Video</th><th>Message</th></tr></thead>
        <tbody id="logs"></tbody>
      </table>
    </section>
  </main>
  <script>
    const fields = {
      playlistRows: document.getElementById('playlistRows'),
      distinctVideos: document.getElementById('distinctVideos'),
      historyRows: document.getElementById('historyRows'),
      historyVideos: document.getElementById('historyVideos'),
      liveHistoryRows: document.getElementById('liveHistoryRows'),
      queueCount: document.getElementById('queueCount'),
      workerState: document.getElementById('workerState'),
      runStatus: document.getElementById('runStatus'),
      metadataCounts: document.getElementById('metadataCounts'),
      logs: document.getElementById('logs'),
      limit: document.getElementById('limit'),
      playlistDelay: document.getElementById('playlistDelay'),
      metadataDelay: document.getElementById('metadataDelay'),
      playlistStaleDays: document.getElementById('playlistStaleDays'),
      metadataStaleDays: document.getElementById('metadataStaleDays'),
      force: document.getElementById('force'),
      playlistQueueCount: document.getElementById('playlistQueueCount'),
      metadataQueueCount: document.getElementById('metadataQueueCount'),
      placeholderQueueCount: document.getElementById('placeholderQueueCount'),
      playlistWorkerState: document.getElementById('playlistWorkerState'),
      metadataWorkerState: document.getElementById('metadataWorkerState'),
      liveHistoryWorkerState: document.getElementById('liveHistoryWorkerState'),
      placeholderWorkerState: document.getElementById('placeholderWorkerState'),
      playlistRunStatus: document.getElementById('playlistRunStatus'),
      metadataRunStatus: document.getElementById('metadataRunStatus'),
      liveHistoryRunStatus: document.getElementById('liveHistoryRunStatus'),
      placeholderRunStatus: document.getElementById('placeholderRunStatus'),
    };

    function escapeHtml(value) {
      return String(value || '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    function fmtTime(value) {
      if (!value) return '';
      return new Date(value * 1000).toLocaleString();
    }

    function render(data) {
      fields.playlistRows.textContent = data.counts.playlist_video_rows || 0;
      fields.distinctVideos.textContent = data.counts.distinct_playlist_videos || 0;
      fields.historyRows.textContent = data.counts.history_rows || 0;
      fields.historyVideos.textContent = data.counts.distinct_history_videos || 0;
      fields.liveHistoryRows.textContent = data.liveHistoryCounts?.live_rows || 0;
      fields.playlistQueueCount.textContent = data.playlistScanQueueCount || 0;
      fields.metadataQueueCount.textContent = data.metadataQueueCount || 0;
      fields.placeholderQueueCount.textContent = data.placeholderRecoveryQueueCount || 0;
      fields.playlistWorkerState.textContent = data.playlistScanRunning ? 'running' : 'idle';
      fields.playlistWorkerState.className = `value ${data.playlistScanRunning ? 'running' : ''}`;
      fields.metadataWorkerState.textContent = data.metadataRunning ? 'running' : 'idle';
      fields.metadataWorkerState.className = `value ${data.metadataRunning ? 'running' : ''}`;
      fields.liveHistoryWorkerState.textContent = data.liveHistoryRunning ? 'running' : 'idle';
      fields.liveHistoryWorkerState.className = `value ${data.liveHistoryRunning ? 'running' : ''}`;
      fields.placeholderWorkerState.textContent = data.placeholderRecoveryRunning ? 'running' : 'idle';
      fields.placeholderWorkerState.className = `value ${data.placeholderRecoveryRunning ? 'running' : ''}`;

      function runHtml(run, label) {
        return run ? `
          <span class="badge">${escapeHtml(label)}</span>
          <span class="badge">${escapeHtml(run.status)}</span>
          <span class="badge">${run.processed}/${run.total} processed</span>
          <span class="badge">${run.found} ok</span>
          <span class="badge ${run.failed ? 'warn' : ''}">${run.failed} failed</span>
          <span class="badge">${escapeHtml(run.message)}</span>
          <span class="badge">Started ${fmtTime(run.started_at)}</span>
        ` : `<span class="badge">${escapeHtml(label)}: no runs yet</span>`;
      }

      fields.playlistRunStatus.innerHTML = runHtml(data.latestPlaylistScanRun, 'Playlist scan');
      fields.metadataRunStatus.innerHTML = runHtml(data.latestMetadataRun, 'Metadata');
      fields.liveHistoryRunStatus.innerHTML = runHtml(data.latestLiveHistoryRun, 'History');
      fields.placeholderRunStatus.innerHTML = runHtml(data.latestPlaceholderRecoveryRun, 'Placeholder recovery');

      fields.metadataCounts.replaceChildren(...(data.metadataCounts || []).map(row => {
        const div = document.createElement('div');
        div.className = 'panel';
        div.innerHTML = `<div class="metric">${escapeHtml(row.fetch_status || 'blank')}</div><div class="value">${row.count}</div>`;
        return div;
      }));

      const logs = [
        ...(data.playlistScanLogs || []).map(log => ({ ...log, subject_id: log.playlist_id, source: 'playlist' })),
        ...(data.metadataLogs || []).map(log => ({ ...log, subject_id: log.video_id, source: 'metadata' })),
        ...(data.liveHistoryLogs || []).map(log => ({ ...log, subject_id: log.video_id, source: 'history' })),
        ...(data.placeholderRecoveryLogs || []).map(log => ({ ...log, subject_id: log.video_id, source: 'placeholder' })),
      ].sort((a, b) => (b.created_at - a.created_at) || ((b.id || 0) - (a.id || 0))).slice(0, 120);
      fields.logs.innerHTML = logs.map(log => `
        <tr>
          <td class="time-cell">${fmtTime(log.created_at)}</td>
          <td>${escapeHtml(log.source)} ${escapeHtml(log.level)}</td>
          <td>${escapeHtml(log.subject_id || '')}</td>
          <td class="message">${escapeHtml(log.message)}</td>
        </tr>
      `).join('');
    }

    async function loadStatus() {
      const response = await fetch('/api/admin/status', { cache: 'no-store' });
      if (!response.ok) throw new Error(`Status failed: ${response.status}`);
      render(await response.json());
    }

    async function post(path, params = {}) {
      const response = await fetch(`${path}?${new URLSearchParams(params)}`, { method: 'POST' });
      if (!response.ok) throw new Error(`Request failed: ${response.status}`);
      await loadStatus();
    }

    document.getElementById('scanPlaylists').addEventListener('click', () => post('/api/admin/playlists/start', {
      limit: fields.limit.value,
      delay: fields.playlistDelay.value,
      stale_days: fields.playlistStaleDays.value,
      force: fields.force.checked ? '1' : '0',
    }).catch(error => alert(error.message)));
    document.getElementById('fetchMetadata').addEventListener('click', () => post('/api/admin/metadata/start', {
      limit: fields.limit.value,
      delay: fields.metadataDelay.value,
      stale_days: fields.metadataStaleDays.value,
      force: fields.force.checked ? '1' : '0',
    }).catch(error => alert(error.message)));
    document.getElementById('startLiveHistory').addEventListener('click', () => post('/api/admin/live-history/start', {
    }).catch(error => alert(error.message)));
    document.getElementById('verifyLiveHistory').addEventListener('click', () => {
      if (!confirm('Verify the full YouTube history? This may run for a long time, but existing fetched history will be kept.')) return;
      post('/api/admin/live-history/verify').catch(error => alert(error.message));
    });
    document.getElementById('importTakeoutHistory').addEventListener('click', () => {
      if (!confirm('Import Takeout watch history and rebuild reconciliation? Existing Takeout rows for this history key will be replaced.')) return;
      post('/api/admin/history/import-takeout').catch(error => alert(error.message));
    });
    document.getElementById('reconcileHistory').addEventListener('click', () => {
      post('/api/admin/history/reconcile').catch(error => alert(error.message));
    });
    document.getElementById('reconcilePlaylists').addEventListener('click', () => {
      post('/api/admin/playlists/reconcile').catch(error => alert(error.message));
    });
    document.getElementById('recoverPlaceholders').addEventListener('click', () => post('/api/admin/placeholders/start', {
      limit: fields.limit.value,
      delay: fields.playlistDelay.value,
      force: fields.force.checked ? '1' : '0',
    }).catch(error => alert(error.message)));
    document.getElementById('stopPlaylists').addEventListener('click', () => post('/api/admin/playlists/stop').catch(error => alert(error.message)));
    document.getElementById('stopMetadata').addEventListener('click', () => post('/api/admin/metadata/stop').catch(error => alert(error.message)));
    document.getElementById('stopLiveHistory').addEventListener('click', () => post('/api/admin/live-history/stop').catch(error => alert(error.message)));
    document.getElementById('stopPlaceholders').addEventListener('click', () => post('/api/admin/placeholders/stop').catch(error => alert(error.message)));
    document.getElementById('refresh').addEventListener('click', () => loadStatus().catch(error => alert(error.message)));
    loadStatus().catch(error => { fields.playlistRunStatus.textContent = error.message; });
    setInterval(loadStatus, 5000);
  </script>
</body>
</html>
"""


class LibraryHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(
        self,
        *args,
        db_path: Path,
        cookie_file: Path,
        video_thumbs: Path,
        takeout_dir: Path,
        directory: str | None = None,
        **kwargs,
    ):
        self.db_path = db_path
        self.cookie_file = cookie_file
        self.video_thumbs = video_thumbs
        self.takeout_dir = takeout_dir
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/admin":
            body = ADMIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/history":
            body = HISTORY_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/data":
            conn = connect(self.db_path)
            try:
                data = fetch_app_data(conn)
            finally:
                conn.close()
            self.send_json(data)
            return
        if parsed.path == "/api/history/search":
            params = urllib.parse.parse_qs(parsed.query)
            query = (params.get("q") or [""])[0]
            try:
                limit = max(1, int((params.get("limit") or ["200"])[0] or 200))
            except ValueError:
                limit = 200
            try:
                offset = max(0, int((params.get("offset") or ["0"])[0] or 0))
            except ValueError:
                offset = 0
            conn = connect(self.db_path)
            try:
                data = history_search_data(conn, query, limit=limit, offset=offset)
            finally:
                conn.close()
            self.send_json(data)
            return
        if parsed.path == "/api/admin/status":
            self.send_json(
                admin_status(
                    self.db_path,
                    METADATA_WORKER,
                    PLAYLIST_SCAN_WORKER,
                    LIVE_HISTORY_WORKER,
                    PLACEHOLDER_RECOVERY_WORKER,
                )
            )
            return
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path in {"/api/admin/worker/start", "/api/admin/metadata/start"}:
            limit = max(0, int((params.get("limit") or ["25"])[0] or 0))
            delay = max(1.0, float((params.get("delay") or ["12"])[0] or 12))
            stale_days = max(0, int((params.get("stale_days") or ["30"])[0] or 30))
            force = (params.get("force") or ["0"])[0] in {"1", "true", "yes"}
            result = METADATA_WORKER.start(
                self.db_path,
                self.cookie_file,
                self.video_thumbs,
                delay=delay,
                limit=limit,
                force=force,
                stale_days=stale_days,
            )
            self.send_json(result)
            return
        if parsed.path in {"/api/admin/worker/stop", "/api/admin/metadata/stop"}:
            self.send_json(METADATA_WORKER.stop())
            return
        if parsed.path == "/api/admin/playlists/start":
            limit = max(0, int((params.get("limit") or ["25"])[0] or 0))
            delay = max(1.0, float((params.get("delay") or ["3"])[0] or 3))
            stale_days = max(0, int((params.get("stale_days") or ["7"])[0] or 7))
            force = (params.get("force") or ["0"])[0] in {"1", "true", "yes"}
            result = PLAYLIST_SCAN_WORKER.start(
                self.db_path,
                self.cookie_file,
                delay=delay,
                limit=limit,
                force=force,
                stale_days=stale_days,
            )
            self.send_json(result)
            return
        if parsed.path == "/api/admin/playlists/stop":
            self.send_json(PLAYLIST_SCAN_WORKER.stop())
            return
        if parsed.path == "/api/admin/placeholders/start":
            limit = max(0, int((params.get("limit") or ["25"])[0] or 0))
            delay = max(1.0, float((params.get("delay") or ["3"])[0] or 3))
            force = (params.get("force") or ["0"])[0] in {"1", "true", "yes"}
            result = PLACEHOLDER_RECOVERY_WORKER.start(
                self.db_path,
                ARCHIVARIX_COOKIE_FILE,
                DEFAULT_ARCHIVARIX_THUMB_DIR,
                delay=delay,
                limit=limit,
                force=force,
            )
            self.send_json(result)
            return
        if parsed.path == "/api/admin/placeholders/stop":
            self.send_json(PLACEHOLDER_RECOVERY_WORKER.stop())
            return
        if parsed.path == "/api/admin/playlists/reconcile":
            conn = connect(self.db_path)
            run_id = uuid.uuid4().hex
            started_at = int(time.time())
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlist_scan_worker_runs(
                          run_id, status, started_at, requested_limit, message
                        )
                        VALUES (?, 'running', ?, 0, ?)
                        """,
                        (run_id, started_at, "Playlist reconciliation started"),
                    )
                    log_playlist_scan_event(conn, run_id, "info", "Playlist reconciliation started")
                    stats = rebuild_playlist_reconciliation(conn)
                    message = (
                        f"Playlist reconciliation complete: {stats['rows']} rows, "
                        f"{stats['inferred']} inferred, {stats['ambiguous']} ambiguous"
                    )
                    conn.execute(
                        """
                        UPDATE playlist_scan_worker_runs
                        SET status = 'complete',
                            finished_at = ?,
                            total = ?,
                            processed = ?,
                            found = ?,
                            failed = 0,
                            message = ?
                        WHERE run_id = ?
                        """,
                        (
                            int(time.time()),
                            stats["playlists"],
                            stats["playlists"],
                            stats["inferred"],
                            message,
                            run_id,
                        ),
                    )
                    log_playlist_scan_event(conn, run_id, "info", message)
            finally:
                conn.close()
            self.send_json({"ok": True, "run_id": run_id, **stats})
            return
        if parsed.path == "/api/admin/live-history/start":
            result = LIVE_HISTORY_WORKER.start(
                self.db_path,
                self.cookie_file,
                mode="recent",
            )
            self.send_json(result)
            return
        if parsed.path in {"/api/admin/live-history/verify", "/api/admin/live-history/rebuild"}:
            result = LIVE_HISTORY_WORKER.start(
                self.db_path,
                self.cookie_file,
                mode="verify",
            )
            self.send_json(result)
            return
        if parsed.path == "/api/admin/live-history/stop":
            self.send_json(LIVE_HISTORY_WORKER.stop())
            return
        if parsed.path == "/api/admin/history/import-takeout":
            import_history(
                argparse.Namespace(
                    db=str(self.db_path),
                    takeout=str(self.takeout_dir),
                    history_key="",
                )
            )
            self.send_json({"ok": True, "message": "Takeout history imported and reconciled"})
            return
        if parsed.path == "/api/admin/history/reconcile":
            conn = connect(self.db_path)
            try:
                with conn:
                    stats = rebuild_history_reconciliation(conn)
            finally:
                conn.close()
            self.send_json({"ok": True, **stats})
            return
        self.send_error(404, "Not found")

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def translate_path(self, path: str) -> str:
        path = urllib.parse.urlparse(path).path
        path = posixpath.normpath(urllib.parse.unquote(path))
        parts = [part for part in path.split("/") if part and part not in {".", ".."}]
        result = ROOT
        for part in parts:
            result /= part
        return str(result)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), format % args))


def serve(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}. Run import first.")
    reconcile_worker_runs(db_path, METADATA_WORKER, PLAYLIST_SCAN_WORKER, LIVE_HISTORY_WORKER, PLACEHOLDER_RECOVERY_WORKER)

    def handler(*handler_args, **handler_kwargs):
        return LibraryHandler(
            *handler_args,
            db_path=db_path,
            cookie_file=Path(args.cookies),
            video_thumbs=Path(args.video_thumbs),
            takeout_dir=Path(args.takeout),
            directory=str(ROOT),
            **handler_kwargs,
        )

    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="Import playlists and cache thumbnails")
    import_parser.add_argument("--db", default=str(DEFAULT_DB))
    import_parser.add_argument("--thumbs", default=str(DEFAULT_THUMB_DIR))
    import_parser.add_argument("--cookies", default=str(COOKIE_FILE))
    import_parser.add_argument("--pockettube", default=str(POCKETTUBE_EXPORT))
    import_parser.set_defaults(func=import_playlists)

    discover_parser = subparsers.add_parser(
        "discover-current",
        help="Discover current signed-in YouTube playlists and add ungrouped ones",
    )
    discover_parser.add_argument("--db", default=str(DEFAULT_DB))
    discover_parser.add_argument("--thumbs", default=str(DEFAULT_THUMB_DIR))
    discover_parser.add_argument("--cookies", default=str(COOKIE_FILE))
    discover_parser.add_argument("--browse-id", default="FEplaylist_aggregation")
    discover_parser.add_argument("--group-key", default="youtube-ungrouped")
    discover_parser.add_argument("--group-name", default="Ungrouped / YouTube")
    discover_parser.add_argument("--include-system", action="store_true")
    discover_parser.set_defaults(func=discover_current_playlists)

    scan_parser = subparsers.add_parser("scan-hidden", help="Scan playlists for hidden videos")
    scan_parser.add_argument("--db", default=str(DEFAULT_DB))
    scan_parser.add_argument("--cookies", default=str(COOKIE_FILE))
    scan_parser.add_argument("--limit", type=int, default=0, help="Scan only the first N playlists")
    scan_parser.set_defaults(func=scan_hidden)

    archivarix_parser = subparsers.add_parser(
        "archivarix-thumbnails",
        help="Search Archivarix for deleted video thumbnail candidates",
    )
    archivarix_parser.add_argument("--db", default=str(DEFAULT_DB))
    archivarix_parser.add_argument("--thumbs", default=str(DEFAULT_ARCHIVARIX_THUMB_DIR))
    archivarix_parser.add_argument("--limit", type=int, default=0, help="Search only the first N affected playlists")
    archivarix_parser.add_argument("--page-size", type=int, default=50)
    archivarix_parser.set_defaults(func=recover_archivarix_thumbnails)

    takeout_parser = subparsers.add_parser("import-takeout", help="Import a Google Takeout YouTube snapshot")
    takeout_parser.add_argument("--db", default=str(DEFAULT_DB))
    takeout_parser.add_argument("--takeout", default=str(TAKEOUT_DIR))
    takeout_parser.add_argument("--snapshot-key", default="takeout-2025-11-09")
    takeout_parser.add_argument("--label", default="Takeout 2025-11-09")
    takeout_parser.set_defaults(func=import_takeout_snapshot)

    history_parser = subparsers.add_parser("import-history", help="Import YouTube Takeout watch/search history")
    history_parser.add_argument("--db", default=str(DEFAULT_DB))
    history_parser.add_argument("--takeout", default=str(ROOT))
    history_parser.add_argument("--history-key", default="")
    history_parser.set_defaults(func=import_history)

    recover_missing_parser = subparsers.add_parser(
        "recover-missing-thumbnails",
        help="Recover Archivarix thumbnails for exact missing snapshot video IDs",
    )
    recover_missing_parser.add_argument("--db", default=str(DEFAULT_DB))
    recover_missing_parser.add_argument("--thumbs", default=str(DEFAULT_ARCHIVARIX_THUMB_DIR))
    recover_missing_parser.add_argument("--snapshot-key", default="takeout-2025-11-09")
    recover_missing_parser.add_argument("--archivarix-cookies", default=str(ARCHIVARIX_COOKIE_FILE))
    recover_missing_parser.add_argument("--video-id", default="")
    recover_missing_parser.add_argument("--limit", type=int, default=0)
    recover_missing_parser.add_argument("--only-missing", action="store_true")
    recover_missing_parser.add_argument("--likely-hidden-only", action="store_true")
    recover_missing_parser.add_argument("--no-api", action="store_true", help="Only try direct Archivarix thumbnail URLs")
    recover_missing_parser.add_argument("--delay", type=float, default=3.0, help="Seconds to wait before each Archivarix API search")
    recover_missing_parser.add_argument("--refresh-metadata", action="store_true", help="Use Archivarix API even when a thumbnail is already cached")
    recover_missing_parser.set_defaults(func=recover_snapshot_missing)

    serve_parser = subparsers.add_parser("serve", help="Serve the library manager")
    serve_parser.add_argument("--db", default=str(DEFAULT_DB))
    serve_parser.add_argument("--cookies", default=str(COOKIE_FILE))
    serve_parser.add_argument("--video-thumbs", default=str(DEFAULT_VIDEO_THUMB_DIR))
    serve_parser.add_argument("--takeout", default=str(ROOT))
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.set_defaults(func=serve)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
