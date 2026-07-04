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
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "yt_playlists.sqlite3"
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
  channel TEXT NOT NULL DEFAULT '',
  duration_text TEXT NOT NULL DEFAULT '',
  is_playable INTEGER NOT NULL DEFAULT 1,
  availability TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (playlist_id, position)
);

CREATE TABLE IF NOT EXISTS archivarix_candidates (
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  video_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
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
  channel TEXT NOT NULL DEFAULT '',
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
  channel TEXT NOT NULL DEFAULT '',
  duration_text TEXT NOT NULL DEFAULT '',
  view_count TEXT NOT NULL DEFAULT '',
  upload_date TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  watch_url TEXT NOT NULL DEFAULT '',
  yt_status TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  fetched_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watch_history (
  history_key TEXT NOT NULL,
  position INTEGER NOT NULL,
  action TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  channel_url TEXT NOT NULL DEFAULT '',
  watched_at TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL DEFAULT '',
  imported_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (history_key, position)
);

CREATE TABLE IF NOT EXISTS search_history (
  history_key TEXT NOT NULL,
  position INTEGER NOT NULL,
  query TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  searched_at TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL DEFAULT '',
  imported_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (history_key, position)
);

CREATE TABLE IF NOT EXISTS youtube_history_occurrences (
  history_key TEXT NOT NULL DEFAULT 'youtube',
  ordinal INTEGER NOT NULL,
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  channel_url TEXT NOT NULL DEFAULT '',
  watch_date TEXT NOT NULL DEFAULT '',
  observed_at TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL DEFAULT 'youtube-live',
  run_id TEXT NOT NULL DEFAULT '',
  imported_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (history_key, ordinal)
);

CREATE TABLE IF NOT EXISTS takeout_history_occurrences (
  history_key TEXT NOT NULL,
  position INTEGER NOT NULL,
  action TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  channel_url TEXT NOT NULL DEFAULT '',
  watched_at TEXT NOT NULL DEFAULT '',
  watch_date TEXT NOT NULL DEFAULT '',
  source_file TEXT NOT NULL DEFAULT '',
  imported_at INTEGER NOT NULL DEFAULT 0,
  row_hash TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (history_key, position)
);

CREATE TABLE IF NOT EXISTS history_reconciled (
  reconciled_id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  channel_url TEXT NOT NULL DEFAULT '',
  best_watch_time TEXT NOT NULL DEFAULT '',
  watch_date TEXT NOT NULL DEFAULT '',
  source_quality TEXT NOT NULL DEFAULT '',
  youtube_history_key TEXT NOT NULL DEFAULT '',
  youtube_ordinal INTEGER NOT NULL DEFAULT 0,
  takeout_history_key TEXT NOT NULL DEFAULT '',
  takeout_position INTEGER NOT NULL DEFAULT 0,
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

CREATE INDEX IF NOT EXISTS idx_groups_parent_position ON groups(parent_key, position);
CREATE INDEX IF NOT EXISTS idx_group_playlists_position ON group_playlists(group_key, position);
CREATE INDEX IF NOT EXISTS idx_playlist_videos_hidden ON playlist_videos(is_playable, playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_archivarix_candidates_playlist ON archivarix_candidates(playlist_id, title);
CREATE INDEX IF NOT EXISTS idx_snapshot_videos_video ON snapshot_videos(snapshot_key, video_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_videos_playlist ON snapshot_videos(snapshot_key, playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_snapshot_video_recovery_status ON snapshot_video_recovery(snapshot_key, search_status);
CREATE INDEX IF NOT EXISTS idx_video_metadata_status ON video_metadata(fetch_status, fetched_at);
CREATE INDEX IF NOT EXISTS idx_watch_history_video ON watch_history(video_id);
CREATE INDEX IF NOT EXISTS idx_watch_history_search ON watch_history(title, channel, watched_at);
CREATE INDEX IF NOT EXISTS idx_search_history_search ON search_history(query, searched_at);
CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_video ON youtube_history_occurrences(video_id);
CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_search ON youtube_history_occurrences(title, channel, ordinal);
CREATE INDEX IF NOT EXISTS idx_youtube_history_occurrences_date ON youtube_history_occurrences(watch_date, video_id);
CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_video ON takeout_history_occurrences(video_id);
CREATE INDEX IF NOT EXISTS idx_takeout_history_occurrences_date ON takeout_history_occurrences(watch_date, video_id);
CREATE INDEX IF NOT EXISTS idx_history_reconciled_video ON history_reconciled(video_id);
CREATE INDEX IF NOT EXISTS idx_history_reconciled_date ON history_reconciled(watch_date, source_quality);
CREATE INDEX IF NOT EXISTS idx_metadata_worker_log_run ON metadata_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_playlist_scan_worker_log_run ON playlist_scan_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_live_history_worker_log_run ON live_history_worker_log(run_id, created_at);
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
        "snapshot_video_recovery",
        {"description": "TEXT NOT NULL DEFAULT ''"},
    )
    ensure_columns(
        conn,
        "youtube_history_occurrences",
        {
            "watch_date": "TEXT NOT NULL DEFAULT ''",
        },
    )
    ensure_columns(
        conn,
        "takeout_history_occurrences",
        {
            "watch_date": "TEXT NOT NULL DEFAULT ''",
            "row_hash": "TEXT NOT NULL DEFAULT ''",
        },
    )
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
    return str(target.relative_to(ROOT)).replace("\\", "/")


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
    return str(target.relative_to(ROOT)).replace("\\", "/")


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
) -> dict[str, Any] | None:
    opener = opener or urllib.request.build_opener()
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
            if event == "search:video":
                payload = json.loads(payload_text)
                if payload.get("videoId") == video_id:
                    return payload
            if event in {"search:complete", "search:error"}:
                return None
    return None


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
    return str(target.relative_to(ROOT)).replace("\\", "/")


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
    if lower_title in {"deleted video", "private video"}:
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
    if lower_title in {"deleted video", "private video"}:
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


def takeout_watch_date(watched_at: str) -> str:
    cleaned = re.sub(r"\s+", " ", watched_at).strip()
    without_tz = re.sub(r"\s+[A-Z]{2,5}$", "", cleaned)
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%B %d, %Y, %I:%M:%S %p"):
        try:
            return datetime.strptime(without_tz, fmt).date().isoformat()
        except ValueError:
            continue
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


def extract_watch_metadata(html_text: str, video_id: str) -> dict[str, str]:
    player = extract_json_assignment(html_text, "ytInitialPlayerResponse")
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
    status = str(playability.get("status") or "").strip()
    reason = text_from_runs(playability.get("reason")).strip()
    if reason and status and reason not in status:
        status = f"{status}: {reason}"
    return {
        "video_id": video_id,
        "title": title,
        "description": str(details.get("shortDescription") or "").strip(),
        "channel": str(details.get("author") or "").strip(),
        "duration_text": format_duration(details.get("lengthSeconds")),
        "view_count": str(details.get("viewCount") or ""),
        "upload_date": str(microformat.get("uploadDate") or microformat.get("publishDate") or ""),
        "thumbnail_url": thumbnail_url,
        "watch_url": f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}",
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


def migrate_live_youtube_history_to_occurrences(conn: sqlite3.Connection) -> None:
    occurrence_count = conn.execute(
        "SELECT COUNT(1) AS count FROM youtube_history_occurrences WHERE history_key = 'youtube'",
    ).fetchone()["count"]
    if occurrence_count:
        return
    rows = conn.execute(
        """
        SELECT position, video_id, title, url, channel, channel_url, watched_at, source_file, imported_at
        FROM watch_history
        WHERE history_key = 'live-youtube'
        ORDER BY position
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO youtube_history_occurrences(
              history_key, ordinal, video_id, title, url, channel, channel_url,
              watch_date, observed_at, source_file, run_id, imported_at, updated_at
            )
            VALUES ('youtube', ?, ?, ?, ?, ?, ?, '', ?, ?, 'migrated-live-youtube', ?, ?)
            """,
            (
                row["position"],
                row["video_id"],
                row["title"],
                row["url"],
                row["channel"],
                row["channel_url"],
                row["watched_at"],
                row["source_file"],
                row["imported_at"],
                row["imported_at"],
            ),
        )


def youtube_occurrence_sequence(
    conn: sqlite3.Connection,
    start: int,
    limit: int,
    history_key: str = "live-youtube",
) -> list[str]:
    del history_key
    return [
        row["video_id"]
        for row in conn.execute(
            """
            SELECT video_id
            FROM youtube_history_occurrences
            WHERE history_key = 'youtube'
              AND ordinal >= ?
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
    run_id: str,
) -> tuple[int, int, str]:
    now = int(time.time())
    observed_at = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(now))
    inserted = 0
    existing = 0
    last_video_id = ""
    migrate_live_youtube_history_to_occurrences(conn)
    for index, row in enumerate(rows, start=start):
        video_id = row.get("video_id") or ""
        if not video_id:
            continue
        previous = conn.execute(
            """
            SELECT video_id
            FROM youtube_history_occurrences
            WHERE history_key = 'youtube' AND ordinal = ?
            """,
            (index,),
        ).fetchone()
        if previous and previous["video_id"] == video_id:
            existing += 1
        else:
            inserted += 1
        last_video_id = video_id
        conn.execute(
            """
            INSERT INTO youtube_history_occurrences(
              history_key, ordinal, video_id, title, url, channel, channel_url,
              watch_date, observed_at, source_file, run_id, imported_at, updated_at
            )
            VALUES ('youtube', ?, ?, ?, ?, ?, ?, ?, ?, 'youtube-live', ?, ?, ?)
            ON CONFLICT(history_key, ordinal) DO UPDATE SET
              video_id=excluded.video_id,
              title=excluded.title,
              url=excluded.url,
              channel=excluded.channel,
              channel_url=excluded.channel_url,
              watch_date=excluded.watch_date,
              observed_at=excluded.observed_at,
              source_file=excluded.source_file,
              run_id=excluded.run_id,
              updated_at=excluded.updated_at
            """,
            (
                index,
                video_id,
                row.get("title") or video_id,
                row.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                row.get("channel") or "",
                row.get("channel_url") or "",
                row.get("watch_date") or "",
                observed_at,
                run_id,
                now,
                now,
            ),
        )
    return inserted, existing, last_video_id


def history_row_hash(row: dict[str, str]) -> str:
    payload = "\x1f".join(
        row.get(key, "")
        for key in ("action", "video_id", "title", "url", "channel", "channel_url", "watched_at")
    )
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()


def rebuild_history_reconciliation(conn: sqlite3.Connection) -> dict[str, int]:
    now = int(time.time())
    youtube_rows = conn.execute(
        """
        SELECT *
        FROM youtube_history_occurrences
        WHERE video_id <> ''
        ORDER BY history_key, ordinal
        """
    ).fetchall()
    takeout_rows = conn.execute(
        """
        SELECT *
        FROM takeout_history_occurrences
        WHERE video_id <> ''
        ORDER BY history_key, position
        """
    ).fetchall()

    takeout_by_video_date: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in takeout_rows:
        takeout_by_video_date.setdefault((row["video_id"], row["watch_date"]), []).append(row)

    matched_youtube: dict[tuple[str, int], sqlite3.Row] = {}
    takeout_to_youtube: dict[tuple[str, int], tuple[str, int]] = {}
    matched_takeout: set[tuple[str, int]] = set()
    for youtube in youtube_rows:
        key = (youtube["video_id"], youtube["watch_date"])
        if not key[0] or not key[1]:
            continue
        candidates = takeout_by_video_date.get(key, [])
        for takeout in candidates:
            takeout_key = (takeout["history_key"], takeout["position"])
            if takeout_key in matched_takeout:
                continue
            matched_takeout.add(takeout_key)
            youtube_key = (youtube["history_key"], youtube["ordinal"])
            matched_youtube[youtube_key] = takeout
            takeout_to_youtube[takeout_key] = youtube_key
            break

    conn.execute("DELETE FROM history_reconciled")
    inserted = 0
    matched = 0
    for takeout in takeout_rows:
        takeout_key = (takeout["history_key"], takeout["position"])
        youtube_match = takeout_to_youtube.get(takeout_key)
        source_quality = "matched" if youtube_match else "takeout_exact"
        if youtube_match:
            matched += 1
        conn.execute(
            """
            INSERT INTO history_reconciled(
              reconciled_id, video_id, title, url, channel, channel_url,
              best_watch_time, watch_date, source_quality,
              youtube_history_key, youtube_ordinal, takeout_history_key, takeout_position,
              match_confidence, match_notes, imported_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"takeout:{takeout['history_key']}:{takeout['position']}",
                takeout["video_id"],
                takeout["title"],
                takeout["url"],
                takeout["channel"],
                takeout["channel_url"],
                takeout["watched_at"],
                takeout["watch_date"],
                source_quality,
                youtube_match[0] if youtube_match else "",
                youtube_match[1] if youtube_match else 0,
                takeout["history_key"],
                takeout["position"],
                "video_id_date" if youtube_match else "takeout_only",
                "same video_id and watch_date" if youtube_match else "",
                takeout["imported_at"],
                now,
            ),
        )
        inserted += 1

    for youtube in youtube_rows:
        youtube_key = (youtube["history_key"], youtube["ordinal"])
        if youtube_key in matched_youtube:
            continue
        conn.execute(
            """
            INSERT INTO history_reconciled(
              reconciled_id, video_id, title, url, channel, channel_url,
              best_watch_time, watch_date, source_quality,
              youtube_history_key, youtube_ordinal, takeout_history_key, takeout_position,
              match_confidence, match_notes, imported_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'youtube_date_only', ?, ?, '', 0, 'youtube_only', '', ?, ?)
            """,
            (
                f"youtube:{youtube['history_key']}:{youtube['ordinal']}",
                youtube["video_id"],
                youtube["title"],
                youtube["url"],
                youtube["channel"],
                youtube["channel_url"],
                youtube["watch_date"] or youtube["observed_at"],
                youtube["watch_date"],
                youtube["history_key"],
                youtube["ordinal"],
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
    hidden_count = sum(1 for video in videos if not video["is_playable"])
    now = int(time.time())
    conn.execute("DELETE FROM playlist_videos WHERE playlist_id = ?", (playlist_id,))
    for video in videos:
        conn.execute(
            """
            INSERT INTO playlist_videos(
              playlist_id, position, video_id, title, channel, duration_text,
              is_playable, availability, url, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video["playlist_id"],
                video["position"],
                video["video_id"],
                video["title"],
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
    return len(videos), hidden_count


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
                 MIN(pv.title) AS current_title
          FROM playlist_videos pv
          WHERE pv.video_id <> ''
          GROUP BY pv.video_id
          UNION ALL
          SELECT wh.video_id,
                 1 AS source_priority,
                 0 AS playlist_count,
                 MIN(wh.title) AS current_title
          FROM watch_history wh
          WHERE wh.video_id <> ''
          GROUP BY wh.video_id
          UNION ALL
          SELECT yo.video_id,
                 1 AS source_priority,
                 0 AS playlist_count,
                 MIN(yo.title) AS current_title
          FROM youtube_history_occurrences yo
          WHERE yo.video_id <> ''
          GROUP BY yo.video_id
        ),
        q AS (
          SELECT video_id,
                 MIN(source_priority) AS source_priority,
                 SUM(playlist_count) AS playlist_count,
                 MIN(current_title) AS current_title
          FROM queue_sources
          GROUP BY video_id
        )
        SELECT q.video_id,
               q.playlist_count,
               q.current_title,
               CASE WHEN q.source_priority = 0 THEN 'playlist' ELSE 'history' END AS metadata_source
        FROM q
        LEFT JOIN video_metadata vm ON vm.video_id = q.video_id
        WHERE {" AND ".join(where)}
        ORDER BY q.source_priority, COALESCE(vm.fetched_at, 0), q.video_id
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def admin_status(
    db_path: Path,
    metadata_worker: "MetadataWorker | None" = None,
    playlist_worker: "PlaylistScanWorker | None" = None,
    live_history_worker: "LiveHistoryWorker | None" = None,
) -> dict[str, Any]:
    reconcile_worker_runs(db_path, metadata_worker, playlist_worker, live_history_worker)
    conn = connect(db_path)
    try:
        counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(DISTINCT video_id) AS distinct_playlist_videos,
                  COUNT(*) AS playlist_video_rows,
                  (SELECT COUNT(*) FROM history_reconciled) AS watch_history_rows,
                  (SELECT COUNT(DISTINCT video_id) FROM history_reconciled WHERE video_id <> '') AS distinct_history_videos,
                  (SELECT COUNT(*) FROM search_history) AS search_history_rows
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
                WHERE history_key = 'youtube'
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
        metadata_queue_count = len(metadata_queue_rows(conn, force=False, stale_days=30))
        playlist_queue_count = len(playlist_scan_queue_rows(conn, force=False, stale_days=7))
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
    finally:
        conn.close()
    return {
        "running": metadata_worker.is_running() if metadata_worker else False,
        "metadataRunning": metadata_worker.is_running() if metadata_worker else False,
        "playlistScanRunning": playlist_worker.is_running() if playlist_worker else False,
        "liveHistoryRunning": live_history_worker.is_running() if live_history_worker else False,
        "counts": counts,
        "liveHistoryCounts": live_history_counts,
        "playlistCounts": playlist_counts,
        "metadataCounts": metadata_counts,
        "queueCount": metadata_queue_count,
        "metadataQueueCount": metadata_queue_count,
        "playlistScanQueueCount": playlist_queue_count,
        "latestRun": dict(latest_metadata_run) if latest_metadata_run else None,
        "latestMetadataRun": dict(latest_metadata_run) if latest_metadata_run else None,
        "latestPlaylistScanRun": dict(latest_playlist_run) if latest_playlist_run else None,
        "latestLiveHistoryRun": dict(latest_live_history_run) if latest_live_history_run else None,
        "logs": metadata_logs,
        "metadataLogs": metadata_logs,
        "playlistScanLogs": playlist_logs,
        "liveHistoryLogs": live_history_logs,
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
) -> None:
    metadata_running = metadata_worker.is_running() if metadata_worker else False
    playlist_running = playlist_worker.is_running() if playlist_worker else False
    live_history_running = live_history_worker.is_running() if live_history_worker else False
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
                status = "ok"
                error = ""
                metadata: dict[str, str] = {
                    "video_id": video_id,
                    "title": "",
                    "description": "",
                    "channel": "",
                    "duration_text": "",
                    "view_count": "",
                    "upload_date": "",
                    "thumbnail_url": "",
                    "thumbnail_path": "",
                    "watch_url": f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}",
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
                    conn.execute(
                        """
                        INSERT INTO video_metadata(
                          video_id, title, description, channel, duration_text, view_count,
                          upload_date, thumbnail_url, thumbnail_path, watch_url,
                          yt_status, fetch_status, fetch_error, fetched_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(video_id) DO UPDATE SET
                          title=excluded.title,
                          description=excluded.description,
                          channel=excluded.channel,
                          duration_text=excluded.duration_text,
                          view_count=excluded.view_count,
                          upload_date=excluded.upload_date,
                          thumbnail_url=excluded.thumbnail_url,
                          thumbnail_path=excluded.thumbnail_path,
                          watch_url=excluded.watch_url,
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
                            metadata.get("channel", ""),
                            metadata.get("duration_text", ""),
                            metadata.get("view_count", ""),
                            metadata.get("upload_date", ""),
                            metadata.get("thumbnail_url", ""),
                            metadata.get("thumbnail_path", ""),
                            metadata.get("watch_url", ""),
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
                        log_worker_event(conn, run_id, "error", error, video_id)
                    else:
                        found += 1
                        title = metadata.get("title") or video_id
                        log_worker_event(conn, run_id, "info", f"{status}: {title}", video_id)
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
                    migrate_live_youtube_history_to_occurrences(conn)
                    existing_ids = youtube_occurrence_sequence(conn, start, len(rows))
                    overlap_offset = find_feed_overlap(fetched_ids, existing_ids) if mode == "recent" else None
                    inserted, existing, batch_last_video_id = save_youtube_history_occurrences(conn, rows, start, run_id)
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
                conn.execute(
                    """
                    INSERT INTO playlist_videos(
                      playlist_id, position, video_id, title, channel, duration_text,
                      is_playable, availability, url, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        video["playlist_id"],
                        video["position"],
                        video["video_id"],
                        video["title"],
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
                conn.execute(
                    """
                    INSERT INTO archivarix_candidates(
                      playlist_id, video_id, title, channel, status, duration_text,
                      upload_date, view_count, thumbnail_url, thumbnail_path,
                      archive_url, video_file_url, query, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                      title=excluded.title,
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

    print(
        f"Imported {len(playlist_rows)} snapshot playlists and "
        f"{imported_video_rows} snapshot video rows into {snapshot_key}."
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


def parse_takeout_watch_history(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
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
                "action": strip_html_fragment(match.group(1)),
                "url": url,
                "video_id": video_id,
                "title": strip_html_fragment(match.group(3)),
                "channel_url": html.unescape(match.group(4) or ""),
                "channel": strip_html_fragment(match.group(5) or ""),
                "watched_at": strip_html_fragment(match.group(6)),
            }
        )
    return rows


def parse_takeout_search_history(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        r"Searched for(?:\s|&nbsp;|\xa0)*<a href=\"([^\"]+)\">(.*?)</a><br>([^<]+)<br>",
        re.IGNORECASE | re.DOTALL,
    )
    rows: list[dict[str, str]] = []
    for match in pattern.finditer(text):
        rows.append(
            {
                "url": html.unescape(match.group(1)),
                "query": strip_html_fragment(match.group(2)),
                "searched_at": strip_html_fragment(match.group(3)),
            }
        )
    return rows


def import_history(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    takeout_dir = Path(args.takeout)
    history_dir = takeout_dir / "history"
    watch_file = history_dir / "watch-history.html"
    search_file = history_dir / "search-history.html"
    if not watch_file.exists() and not search_file.exists():
        raise SystemExit(f"Takeout history files not found in {history_dir}")

    conn = connect(db_path)
    imported_at = int(time.time())
    watch_rows = parse_takeout_watch_history(watch_file) if watch_file.exists() else []
    search_rows = parse_takeout_search_history(search_file) if search_file.exists() else []
    with conn:
        conn.execute("DELETE FROM watch_history WHERE history_key = ?", (args.history_key,))
        conn.execute("DELETE FROM takeout_history_occurrences WHERE history_key = ?", (args.history_key,))
        conn.execute("DELETE FROM search_history WHERE history_key = ?", (args.history_key,))
        for position, row in enumerate(watch_rows, start=1):
            watch_date = takeout_watch_date(row["watched_at"])
            source_file = display_source_path(watch_file)
            conn.execute(
                """
                INSERT INTO watch_history(
                  history_key, position, action, video_id, title, url, channel,
                  channel_url, watched_at, source_file, imported_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    args.history_key,
                    position,
                    row["action"],
                    row["video_id"],
                    row["title"],
                    row["url"],
                    row["channel"],
                    row["channel_url"],
                    row["watched_at"],
                    source_file,
                    imported_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO takeout_history_occurrences(
                  history_key, position, action, video_id, title, url, channel,
                  channel_url, watched_at, watch_date, source_file, imported_at, row_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    args.history_key,
                    position,
                    row["action"],
                    row["video_id"],
                    row["title"],
                    row["url"],
                    row["channel"],
                    row["channel_url"],
                    row["watched_at"],
                    watch_date,
                    source_file,
                    imported_at,
                    history_row_hash(row),
                ),
            )
        for position, row in enumerate(search_rows, start=1):
            conn.execute(
                """
                INSERT INTO search_history(
                  history_key, position, query, url, searched_at, source_file, imported_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    args.history_key,
                    position,
                    row["query"],
                    row["url"],
                    row["searched_at"],
                    display_source_path(search_file),
                    imported_at,
                ),
            )
        stats = rebuild_history_reconciliation(conn)
    conn.close()
    distinct_videos = len({row["video_id"] for row in watch_rows if row["video_id"]})
    print(
        f"Imported {len(watch_rows)} watch history rows "
        f"({distinct_videos} distinct videos) and {len(search_rows)} searches. "
        f"Reconciled {stats['rows']} rows ({stats['matched']} matched)."
    )


def recover_snapshot_missing(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    thumb_dir = Path(args.thumbs)
    conn = connect(db_path)
    archivarix_opener = load_cookie_opener(Path(args.archivarix_cookies))
    where_clauses = [
        "sv.snapshot_key = ?",
        "pv.video_id IS NULL",
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
         AND pv.is_playable = 1
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
    for index, row in enumerate(rows, start=1):
        snapshot_key = row["snapshot_key"]
        video_id = row["video_id"]
        status = "not_found"
        error = ""
        video: dict[str, Any] | None = None
        thumbnail_path = ""
        thumbnail_url = ""
        thumbnail_path = cache_archivarix_thumbnail(
            video_id,
            "",
            thumb_dir,
            archivarix_opener,
        )
        if thumbnail_path and not args.refresh_metadata:
            status = "thumbnail_only"
            cached += 1
        elif not args.no_api:
            try:
                if args.delay:
                    time.sleep(args.delay)
                video = archivarix_lookup_video(video_id, archivarix_opener)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                status = "error"
                error = str(exc)
        if video:
            found += 1
            status = "found"
            thumbnail_url = video.get("thumbnailArchiveUrl") or video.get("thumbnailUrl") or ""
            thumbnail_path = thumbnail_path or cache_archivarix_thumbnail(
                video_id,
                thumbnail_url,
                thumb_dir,
                archivarix_opener,
            )
            if thumbnail_path:
                cached += 1
        recovered_status = (video or {}).get("status") or ""
        if status == "not_found":
            recovered_status = "NOT_FOUND"
        with conn:
            conn.execute(
                """
                INSERT INTO snapshot_video_recovery(
                  snapshot_key, video_id, title, description, channel, status, duration_text,
                  upload_date, view_count, thumbnail_url, thumbnail_path,
                  archive_url, video_file_url, searched_at, search_status, search_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_key, video_id) DO UPDATE SET
                  title=excluded.title,
                  description=excluded.description,
                  channel=excluded.channel,
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
                    (video or {}).get("channelTitle") or "",
                    recovered_status,
                    format_duration((video or {}).get("duration")),
                    (video or {}).get("uploadDate") or "",
                    str((video or {}).get("viewCount") or ""),
                    thumbnail_url,
                    thumbnail_path,
                    (video or {}).get("archiveUrl") or "",
                    (video or {}).get("videoFileUrl") or "",
                    int(time.time()),
                    status,
                    error,
                ),
            )
        label = (video or {}).get("title") or video_id
        suffix = "thumbnail" if thumbnail_path else status
        print(f"[{index:03d}/{len(rows):03d}] {suffix} - {label}")
    print(f"Found {found} Archivarix records and cached {cached} thumbnails.")


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
                   COALESCE(vm.title, '') AS metadata_title,
                   COALESCE(vm.description, '') AS metadata_description,
                   COALESCE(vm.channel, '') AS metadata_channel,
                   COALESCE(vm.duration_text, '') AS metadata_duration,
                   COALESCE(vm.upload_date, '') AS metadata_upload_date,
                   COALESCE(vm.thumbnail_path, '') AS metadata_thumbnail_path,
                   COALESCE(vm.fetch_status, '') AS metadata_fetch_status
            FROM playlist_videos v
            JOIN playlists p ON p.playlist_id = v.playlist_id
            LEFT JOIN video_metadata vm ON vm.video_id = v.video_id
            ORDER BY p.title COLLATE NOCASE, v.position
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
                   COALESCE(r.channel, '') AS recovered_channel,
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
    history_summary.update(
        dict(
            conn.execute(
                "SELECT COUNT(*) AS search_rows FROM search_history"
            ).fetchone()
        )
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


def search_history_data(conn: sqlite3.Connection, query: str, limit: int = 200) -> dict[str, Any]:
    query = query.strip()
    limit = max(1, min(limit, 1000))
    like = f"%{query.lower()}%"
    if query:
        watch_where = """
            WHERE lower(hr.title || ' ' || hr.channel || ' ' || hr.video_id || ' ' || COALESCE(vm.description, '')) LIKE ?
        """
        search_where = "WHERE lower(sh.query) LIKE ?"
        watch_params: list[Any] = [like]
        search_params: list[Any] = [like]
    else:
        watch_where = ""
        search_where = ""
        watch_params = []
        search_params = []
    watch_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT hr.reconciled_id,
                   COALESCE(NULLIF(hr.takeout_history_key, ''), hr.youtube_history_key) AS history_key,
                   CASE WHEN hr.takeout_position > 0 THEN hr.takeout_position ELSE hr.youtube_ordinal END AS position,
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
                   hr.takeout_position,
                   hr.imported_at,
                   COALESCE(vm.title, '') AS metadata_title,
                   COALESCE(vm.description, '') AS metadata_description,
                   COALESCE(vm.channel, '') AS metadata_channel,
                   COALESCE(vm.duration_text, '') AS metadata_duration,
                   COALESCE(vm.thumbnail_path, '') AS metadata_thumbnail_path,
                   COALESCE(vm.fetch_status, '') AS metadata_fetch_status
            FROM history_reconciled hr
            LEFT JOIN video_metadata vm ON vm.video_id = hr.video_id
            {watch_where}
            ORDER BY hr.watch_date DESC, hr.imported_at DESC, position
            LIMIT ?
            """,
            [*watch_params, limit],
        )
    ]
    search_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM search_history sh
            {search_where}
            ORDER BY sh.position
            LIMIT ?
            """,
            [*search_params, limit],
        )
    ]
    total = dict(
        conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM history_reconciled) AS watch_rows,
              (SELECT COUNT(DISTINCT video_id) FROM history_reconciled WHERE video_id <> '') AS distinct_watch_videos,
              (SELECT COUNT(*) FROM search_history) AS search_rows
            """
        ).fetchone()
    )
    return {
        "query": query,
        "watch": watch_rows,
        "searches": search_rows,
        "totals": total,
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
      <h1>YT Library</h1>
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
        const ids = [];
        for (const group of children.get('') || []) {
          for (const id of groupPlaylistIds(group.group_key)) ids.push(id);
        }
        return [...new Set(ids)];
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

    function displayVideoTitle(video) {
      return video.metadata_title || video.title || video.video_id;
    }

    function displayVideoChannel(video) {
      return video.metadata_channel || video.channel || '';
    }

    function displayVideoDuration(video) {
      return video.metadata_duration || video.duration_text || '';
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
      body.innerHTML = `
        <div class="position">#${video.position}</div>
        ${watchUrl
          ? `<a class="playlist-title" href="${watchUrl}" target="_blank" rel="noreferrer">${escapeHtml(displayVideoTitle(video))}</a>`
          : `<div class="video-title">${escapeHtml(displayVideoTitle(video))}</div>`}
        <div class="details">
          ${video.is_playable ? '' : `<span class="badge">${escapeHtml(video.availability || 'Hidden')}</span>`}
          ${displayVideoDuration(video) ? `<span>${escapeHtml(displayVideoDuration(video))}</span>` : ''}
          ${displayVideoChannel(video) ? `<span>${escapeHtml(displayVideoChannel(video))}</span>` : ''}
          ${video.video_id ? `<span>${escapeHtml(video.video_id)}</span>` : ''}
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
    .tabs { display: flex; gap: 8px; margin-bottom: 14px; }
    .tab { padding: 8px 11px; border: 1px solid var(--line); border-radius: 6px; color: var(--muted); cursor: pointer; }
    .tab.active { background: var(--accent-soft); color: var(--ink); }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }
    .list { display: grid; gap: 10px; }
    .card { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); overflow: hidden; min-width: 0; }
    .thumb { display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover; background: linear-gradient(135deg, #24424a, #d98948); }
    .body { padding: 11px 12px 13px; }
    .title { display: block; color: var(--ink); font-weight: 650; line-height: 1.25; overflow-wrap: anywhere; }
    .details { color: var(--muted); font-size: 13px; margin-top: 7px; display: flex; flex-wrap: wrap; gap: 6px; }
    .description { color: var(--muted); font-size: 13px; line-height: 1.35; margin-top: 8px; max-height: 5.4em; overflow: hidden; }
    .badge { color: var(--warn); font-weight: 650; }
    .empty { color: var(--muted); padding: 36px 0; }
    @media (max-width: 720px) {
      .page { padding: 18px 14px; }
      header, .searchbar { display: block; }
      nav { margin-top: 10px; }
      button { margin-top: 8px; height: 40px; }
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
      <input id="search" type="search" placeholder="Search watch history, metadata descriptions, and searches" autocomplete="off" autofocus>
      <button id="refresh" type="button">Search</button>
    </div>
    <div id="meta" class="meta"></div>
    <div class="tabs">
      <div id="watchTab" class="tab active">Watch history</div>
      <div id="searchTab" class="tab">Search history</div>
    </div>
    <section id="results" class="grid"></section>
    <div id="empty" class="empty" hidden>No history matches.</div>
  </div>
  <script>
    const input = document.getElementById('search');
    const refresh = document.getElementById('refresh');
    const meta = document.getElementById('meta');
    const results = document.getElementById('results');
    const empty = document.getElementById('empty');
    const watchTab = document.getElementById('watchTab');
    const searchTab = document.getElementById('searchTab');
    let activeTab = 'watch';
    let latest = { watch: [], searches: [], totals: {} };
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

    function watchCard(row) {
      const article = document.createElement('article');
      article.className = 'card';
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
      body.innerHTML = `
        <a class="title" href="${watchUrl}" target="_blank" rel="noreferrer">${escapeHtml(displayTitle(row))}</a>
        <div class="details">
          ${row.watched_at ? `<span>${escapeHtml(row.watched_at)}</span>` : ''}
          ${row.source_quality ? `<span class="badge">${escapeHtml(row.source_quality)}</span>` : ''}
          ${(row.metadata_channel || row.channel) ? `<span>${escapeHtml(row.metadata_channel || row.channel)}</span>` : ''}
          ${row.video_id ? `<span>${escapeHtml(row.video_id)}</span>` : ''}
          ${row.metadata_fetch_status === 'error' ? '<span class="badge">metadata error</span>' : ''}
        </div>
        ${row.metadata_description ? `<div class="description">${escapeHtml(row.metadata_description)}</div>` : ''}
      `;
      article.append(body);
      return article;
    }

    function searchRow(row) {
      const article = document.createElement('article');
      article.className = 'card';
      const body = document.createElement('div');
      body.className = 'body';
      body.innerHTML = `
        <a class="title" href="${row.url}" target="_blank" rel="noreferrer">${escapeHtml(row.query)}</a>
        <div class="details">${row.searched_at ? `<span>${escapeHtml(row.searched_at)}</span>` : ''}</div>
      `;
      article.append(body);
      return article;
    }

    function render() {
      watchTab.classList.toggle('active', activeTab === 'watch');
      searchTab.classList.toggle('active', activeTab === 'search');
      const rows = activeTab === 'watch' ? latest.watch : latest.searches;
      results.className = activeTab === 'watch' ? 'grid' : 'list';
      results.replaceChildren(...rows.map(activeTab === 'watch' ? watchCard : searchRow));
      empty.hidden = rows.length !== 0;
      const totals = latest.totals || {};
      meta.innerHTML = `
        <span>${latest.watch.length} watch results shown</span>
        <span>${latest.searches.length} search results shown</span>
        <span>${totals.watch_rows || 0} watch rows</span>
        <span>${totals.distinct_watch_videos || 0} distinct videos</span>
        <span>${totals.search_rows || 0} searches</span>
      `;
    }

    async function load() {
      const myRequest = ++requestId;
      refresh.disabled = true;
      refresh.textContent = 'Searching';
      const params = new URLSearchParams({ q: input.value.trim(), limit: '250' });
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
      timer = setTimeout(() => load().catch(error => {
        meta.textContent = error.message;
        refresh.disabled = false;
        refresh.textContent = 'Search';
      }), 250);
    }

    input.addEventListener('input', schedule);
    refresh.addEventListener('click', () => load().catch(error => meta.textContent = error.message));
    watchTab.addEventListener('click', () => { activeTab = 'watch'; render(); });
    searchTab.addEventListener('click', () => { activeTab = 'search'; render(); });
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
      <div class="panel"><div class="metric">Playlist scanner</div><div id="playlistWorkerState" class="value">idle</div></div>
      <div class="panel"><div class="metric">Metadata worker</div><div id="metadataWorkerState" class="value">idle</div></div>
      <div class="panel"><div class="metric">History fetch</div><div id="liveHistoryWorkerState" class="value">idle</div></div>
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
        <button id="stopPlaylists" type="button">Stop playlist scan</button>
        <button id="stopMetadata" type="button">Stop metadata</button>
        <button id="stopLiveHistory" type="button">Stop history fetch</button>
        <button id="refresh" type="button">Refresh status</button>
      </div>
      <div id="playlistRunStatus" class="status"></div>
      <div id="metadataRunStatus" class="status" style="margin-top:8px"></div>
      <div id="liveHistoryRunStatus" class="status" style="margin-top:8px"></div>
    </section>

    <section class="grid" id="metadataCounts"></section>

    <section class="panel logs">
      <table>
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
      playlistWorkerState: document.getElementById('playlistWorkerState'),
      metadataWorkerState: document.getElementById('metadataWorkerState'),
      liveHistoryWorkerState: document.getElementById('liveHistoryWorkerState'),
      playlistRunStatus: document.getElementById('playlistRunStatus'),
      metadataRunStatus: document.getElementById('metadataRunStatus'),
      liveHistoryRunStatus: document.getElementById('liveHistoryRunStatus'),
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
      fields.historyRows.textContent = data.counts.watch_history_rows || 0;
      fields.historyVideos.textContent = data.counts.distinct_history_videos || 0;
      fields.liveHistoryRows.textContent = data.liveHistoryCounts?.live_rows || 0;
      fields.playlistQueueCount.textContent = data.playlistScanQueueCount || 0;
      fields.metadataQueueCount.textContent = data.metadataQueueCount || 0;
      fields.playlistWorkerState.textContent = data.playlistScanRunning ? 'running' : 'idle';
      fields.playlistWorkerState.className = `value ${data.playlistScanRunning ? 'running' : ''}`;
      fields.metadataWorkerState.textContent = data.metadataRunning ? 'running' : 'idle';
      fields.metadataWorkerState.className = `value ${data.metadataRunning ? 'running' : ''}`;
      fields.liveHistoryWorkerState.textContent = data.liveHistoryRunning ? 'running' : 'idle';
      fields.liveHistoryWorkerState.className = `value ${data.liveHistoryRunning ? 'running' : ''}`;

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
      ].sort((a, b) => (b.created_at - a.created_at) || ((b.id || 0) - (a.id || 0))).slice(0, 120);
      fields.logs.innerHTML = logs.map(log => `
        <tr>
          <td>${fmtTime(log.created_at)}</td>
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
    document.getElementById('stopPlaylists').addEventListener('click', () => post('/api/admin/playlists/stop').catch(error => alert(error.message)));
    document.getElementById('stopMetadata').addEventListener('click', () => post('/api/admin/metadata/stop').catch(error => alert(error.message)));
    document.getElementById('stopLiveHistory').addEventListener('click', () => post('/api/admin/live-history/stop').catch(error => alert(error.message)));
    document.getElementById('refresh').addEventListener('click', () => loadStatus().catch(error => alert(error.message)));
    loadStatus().catch(error => { fields.playlistRunStatus.textContent = error.message; });
    setInterval(loadStatus, 5000);
  </script>
</body>
</html>
"""


class PlaylistHandler(http.server.SimpleHTTPRequestHandler):
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
            limit = max(1, int((params.get("limit") or ["200"])[0] or 200))
            conn = connect(self.db_path)
            try:
                data = search_history_data(conn, query, limit=limit)
            finally:
                conn.close()
            self.send_json(data)
            return
        if parsed.path == "/api/admin/status":
            self.send_json(admin_status(self.db_path, METADATA_WORKER, PLAYLIST_SCAN_WORKER, LIVE_HISTORY_WORKER))
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
                    history_key="takeout-2025-11-09",
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
    reconcile_worker_runs(db_path, METADATA_WORKER, PLAYLIST_SCAN_WORKER, LIVE_HISTORY_WORKER)

    def handler(*handler_args, **handler_kwargs):
        return PlaylistHandler(
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
    history_parser.add_argument("--takeout", default=str(TAKEOUT_DIR))
    history_parser.add_argument("--history-key", default="takeout-2025-11-09")
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

    serve_parser = subparsers.add_parser("serve", help="Serve the playlist browser")
    serve_parser.add_argument("--db", default=str(DEFAULT_DB))
    serve_parser.add_argument("--cookies", default=str(COOKIE_FILE))
    serve_parser.add_argument("--video-thumbs", default=str(DEFAULT_VIDEO_THUMB_DIR))
    serve_parser.add_argument("--takeout", default=str(TAKEOUT_DIR))
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.set_defaults(func=serve)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
