"""SQLite connection, schema bootstrap, and migration support."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .history import history_match_type_for_identity, history_source_type_for_identity
from .schema import load_schema
from .time_utils import utc_now


VIDEO_VISIBILITY_CAPTURE_START = "2026-07-30T17:54:45Z"
PLAYLIST_METADATA_CAPTURE_START = "2026-07-30T20:26:38Z"
CHANNEL_SUBSCRIPTION_CAPTURE_START = "2026-07-30T20:34:50Z"
CHANNEL_NOTIFICATION_CAPTURE_START = "2026-07-30T20:55:56Z"

SCHEMA = load_schema()
SCHEMA_VERSION = 14


_DATABASE_BOOTSTRAP_LOCK = threading.Lock()


def worker_queue_order_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}priority, "
        f"{prefix}updated_at DESC, "
        f"{prefix}queue_id DESC"
    )


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


def _detach_duplicate_my_activity_history(
    conn: sqlite3.Connection,
    history_row: sqlite3.Row,
    updated_at: str,
) -> None:
    has_takeout = bool(history_row["takeout_history_key"])
    has_youtube = history_row["youtube_ordinal"] is not None
    if not has_takeout and not has_youtube:
        conn.execute(
            "DELETE FROM history_events WHERE event_id = ?",
            (history_row["event_id"],),
        )
        return

    source_type = history_source_type_for_identity(
        None,
        history_row["takeout_history_key"],
        history_row["youtube_ordinal"],
    )
    match_type = history_match_type_for_identity(
        None,
        history_row["takeout_history_key"],
        history_row["youtube_ordinal"],
    )
    conn.execute(
        """
        UPDATE history_events
        SET my_activity_event_id=NULL,
            watched_at=CASE WHEN takeout_history_key IS NULL THEN NULL ELSE watched_at END,
            time_precision=CASE
              WHEN takeout_history_key IS NOT NULL THEN 'exact'
              WHEN watch_date IS NOT NULL THEN 'date_only'
              ELSE 'unknown'
            END,
            source_type=?, match_type=?, updated_at=?
        WHERE event_id=?
        """,
        (source_type, match_type, updated_at, history_row["event_id"]),
    )


def _deduplicate_my_activity_occurrences(conn: sqlite3.Connection) -> dict[str, int]:
    """Collapse repeated Google representations of the same exact occurrence."""

    now = utc_now()
    watch_removed = 0
    history_removed = 0
    history_detached = 0
    watch_groups = conn.execute(
        """
        SELECT video_id, watched_at
        FROM my_activity_watch_events
        GROUP BY video_id, watched_at
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in watch_groups:
        source_rows = conn.execute(
            """
            SELECT source.*,
                   history.event_id AS history_event_id,
                   history.youtube_ordinal,
                   history.takeout_history_key,
                   history.takeout_row_key,
                   history.watch_progress_percent,
                   history.watch_resume_seconds
            FROM my_activity_watch_events source
            LEFT JOIN history_events history
              ON history.my_activity_event_id = source.event_id
            WHERE source.video_id=? AND source.watched_at=?
            ORDER BY
              CASE
                WHEN history.takeout_history_key IS NOT NULL
                 AND history.youtube_ordinal IS NOT NULL THEN 3
                WHEN history.takeout_history_key IS NOT NULL THEN 2
                WHEN history.youtube_ordinal IS NOT NULL THEN 1
                ELSE 0
              END DESC,
              COALESCE(history.watch_progress_percent, 0) DESC,
              COALESCE(history.watch_resume_seconds, 0) DESC,
              source.collected_at ASC,
              source.event_id ASC
            """,
            (group["video_id"], group["watched_at"]),
        ).fetchall()
        canonical = source_rows[0]
        observed_title = next(
            (row["observed_title"] for row in source_rows if row["observed_title"]),
            "",
        )
        observed_url = next(
            (row["observed_url"] for row in source_rows if row["observed_url"]),
            "",
        )
        collected_at = min(row["collected_at"] for row in source_rows)
        updated_at = max(row["updated_at"] for row in source_rows)
        conn.execute(
            """
            UPDATE my_activity_watch_events
            SET observed_title=?, observed_url=?, collected_at=?, updated_at=?
            WHERE event_id=?
            """,
            (
                observed_title,
                observed_url,
                collected_at,
                updated_at,
                canonical["event_id"],
            ),
        )
        for duplicate in source_rows[1:]:
            history_row = conn.execute(
                "SELECT * FROM history_events WHERE my_activity_event_id = ?",
                (duplicate["event_id"],),
            ).fetchone()
            if history_row:
                had_other_evidence = bool(history_row["takeout_history_key"]) or (
                    history_row["youtube_ordinal"] is not None
                )
                _detach_duplicate_my_activity_history(conn, history_row, now)
                if had_other_evidence:
                    history_detached += 1
                else:
                    history_removed += 1
            conn.execute(
                "DELETE FROM my_activity_watch_events WHERE event_id = ?",
                (duplicate["event_id"],),
            )
            watch_removed += 1

    legacy_history_rows = conn.execute(
        """
        SELECT history.*
        FROM history_events history
        WHERE history.event_id LIKE 'my_activity:%'
          AND history.my_activity_event_id IS NULL
          AND COALESCE(history.watched_at, '') <> ''
          AND EXISTS (
            SELECT 1
            FROM my_activity_watch_events source
            WHERE source.video_id = history.video_id
              AND source.watched_at = history.watched_at
          )
        """
    ).fetchall()
    for history_row in legacy_history_rows:
        had_other_evidence = bool(history_row["takeout_history_key"]) or (
            history_row["youtube_ordinal"] is not None
        )
        _detach_duplicate_my_activity_history(conn, history_row, now)
        if had_other_evidence:
            history_detached += 1
        else:
            history_removed += 1

    subscription_removed = 0
    subscription_groups = conn.execute(
        """
        SELECT channel_id, subscribed_at
        FROM my_activity_subscription_events
        GROUP BY channel_id, subscribed_at
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in subscription_groups:
        source_rows = conn.execute(
            """
            SELECT *
            FROM my_activity_subscription_events
            WHERE channel_id=? AND subscribed_at=?
            ORDER BY collected_at ASC, event_id ASC
            """,
            (group["channel_id"], group["subscribed_at"]),
        ).fetchall()
        canonical = source_rows[0]
        observed_title = next(
            (row["observed_title"] for row in source_rows if row["observed_title"]),
            "",
        )
        observed_url = next(
            (row["observed_url"] for row in source_rows if row["observed_url"]),
            "",
        )
        conn.execute(
            """
            UPDATE my_activity_subscription_events
            SET observed_title=?, observed_url=?, collected_at=?, updated_at=?
            WHERE event_id=?
            """,
            (
                observed_title,
                observed_url,
                min(row["collected_at"] for row in source_rows),
                max(row["updated_at"] for row in source_rows),
                canonical["event_id"],
            ),
        )
        for duplicate in source_rows[1:]:
            conn.execute(
                "DELETE FROM my_activity_subscription_events WHERE event_id = ?",
                (duplicate["event_id"],),
            )
            subscription_removed += 1

    return {
        "watch_removed": watch_removed,
        "subscription_removed": subscription_removed,
        "history_removed": history_removed,
        "history_detached": history_detached,
    }


def _migrate_database(conn: sqlite3.Connection) -> None:
    current_version = _schema_version(conn)
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current_version} is newer than this application supports ({SCHEMA_VERSION})"
        )
    if current_version == SCHEMA_VERSION:
        return
    if current_version == 13:
        _deduplicate_my_activity_occurrences(conn)
    if 0 < current_version < 13:
        existing_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "channels" in existing_tables:
            channel_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(channels)")
            }
            if "subscribed_at" not in channel_columns:
                conn.execute("ALTER TABLE channels ADD COLUMN subscribed_at TEXT")
            if "subscribed_at_source" not in channel_columns:
                conn.execute(
                    "ALTER TABLE channels ADD COLUMN subscribed_at_source TEXT NOT NULL DEFAULT ''"
                )
        if "playlists" in existing_tables:
            playlist_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(playlists)")
            }
            if "created_at" not in playlist_columns:
                conn.execute("ALTER TABLE playlists ADD COLUMN created_at TEXT")
        if "history_events" in existing_tables:
            history_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(history_events)")
            }
            if "my_activity_event_id" not in history_columns:
                conn.execute(
                    "ALTER TABLE history_events ADD COLUMN my_activity_event_id TEXT"
                )
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
    if current_version < 6:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(placeholder_recovery_worker_runs)")
        }
        if "request_count" not in columns:
            conn.execute(
                """
                ALTER TABLE placeholder_recovery_worker_runs
                ADD COLUMN request_count INTEGER NOT NULL DEFAULT 0
                """
            )
        conn.execute(
            """
            UPDATE placeholder_recovery_worker_runs
            SET request_count = 1
            WHERE request_started_at IS NOT NULL
              AND request_count = 0
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (6, utc_now()),
        )
    if current_version < 7:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(playlists)")
        }
        if "is_library_playlist" not in columns:
            conn.execute(
                """
                ALTER TABLE playlists
                ADD COLUMN is_library_playlist INTEGER NOT NULL DEFAULT 0
                CHECK (is_library_playlist IN (0, 1))
                """
            )
        conn.execute(
            """
            UPDATE playlists
            SET is_library_playlist = 1
            WHERE EXISTS (
              SELECT 1
              FROM playlist_items pi
              WHERE pi.playlist_id = playlists.playlist_id
                AND pi.source_quality = 'takeout'
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (7, utc_now()),
        )
    if current_version < 8:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(channels)")
        }
        if "first_seen_at" not in columns:
            conn.execute("ALTER TABLE channels ADD COLUMN first_seen_at TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (8, utc_now()),
        )
    if current_version < 9:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(channels)")
        }
        if "notification_level" not in columns:
            conn.execute(
                """
                ALTER TABLE channels
                ADD COLUMN notification_level TEXT NOT NULL DEFAULT ''
                CHECK (notification_level IN ('', 'all', 'personalized', 'none'))
                """
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (9, utc_now()),
        )
    if current_version < 10:
        conn.execute(
            """
            UPDATE videos
            SET title = ''
            WHERE TRIM(title) = video_id
            """
        )
        conn.execute(
            """
            UPDATE worker_queue
            SET current_title = ''
            WHERE video_id <> ''
              AND TRIM(current_title) = video_id
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (10, utc_now()),
        )
    if current_version < 11:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(videos)")
        }
        if "watch_progress_percent" in columns:
            conn.execute("ALTER TABLE videos DROP COLUMN watch_progress_percent")
        if "watch_resume_seconds" in columns:
            conn.execute("ALTER TABLE videos DROP COLUMN watch_resume_seconds")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (11, utc_now()),
        )
    if current_version < 12:
        channel_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(channels)")
        }
        if "subscription_checked_at" not in channel_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN subscription_checked_at TEXT")
        if "notification_checked_at" not in channel_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN notification_checked_at TEXT")

        playlist_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(playlists)")
        }
        if "metadata_checked_at" not in playlist_columns:
            conn.execute("ALTER TABLE playlists ADD COLUMN metadata_checked_at TEXT")

        video_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(videos)")
        }
        if "visibility_checked_at" not in video_columns:
            conn.execute("ALTER TABLE videos ADD COLUMN visibility_checked_at TEXT")

        conn.execute(
            """
            UPDATE channels
            SET notification_level = ''
            WHERE subscribed = 0
            """
        )
        conn.execute(
            """
            UPDATE channels
            SET subscription_checked_at = fetched_at
            WHERE fetch_status = 'ok'
              AND fetched_at >= ?
            """,
            (CHANNEL_SUBSCRIPTION_CAPTURE_START,),
        )
        conn.execute(
            """
            UPDATE channels
            SET notification_checked_at = fetched_at
            WHERE fetch_status = 'ok'
              AND fetched_at >= ?
              AND (subscribed = 0 OR notification_level <> '')
            """,
            (CHANNEL_NOTIFICATION_CAPTURE_START,),
        )
        conn.execute(
            """
            UPDATE playlists
            SET metadata_checked_at = (
              SELECT ps.scanned_at
              FROM playlist_scans ps
              WHERE ps.playlist_id = playlists.playlist_id
                AND ps.scan_status = 'ok'
            )
            WHERE COALESCE(owner_channel_id, '') <> ''
              AND COALESCE(visibility, '') <> ''
              AND EXISTS (
                SELECT 1
                FROM playlist_scans ps
                WHERE ps.playlist_id = playlists.playlist_id
                  AND ps.scan_status = 'ok'
                  AND ps.scanned_at >= ?
              )
            """,
            (PLAYLIST_METADATA_CAPTURE_START,),
        )
        conn.execute(
            """
            UPDATE videos
            SET visibility_checked_at = fetched_at
            WHERE fetch_status = 'ok'
              AND fetched_at >= ?
              AND availability <> 'unknown'
              AND COALESCE(channel_id, '') <> ''
            """,
            (VIDEO_VISIBILITY_CAPTURE_START,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (12, utc_now()),
        )
    if current_version < 13:
        channel_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(channels)")
        }
        if "subscribed_at" not in channel_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN subscribed_at TEXT")
        if "subscribed_at_source" not in channel_columns:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN subscribed_at_source TEXT NOT NULL DEFAULT ''"
            )

        playlist_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(playlists)")
        }
        if "created_at" not in playlist_columns:
            conn.execute("ALTER TABLE playlists ADD COLUMN created_at TEXT")

        history_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(history_events)")
        }
        if "my_activity_event_id" not in history_columns:
            conn.execute(
                """
                ALTER TABLE history_events
                ADD COLUMN my_activity_event_id TEXT
                REFERENCES my_activity_watch_events(event_id)
                """
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO my_activity_watch_events(
              event_id, video_id, watched_at, observed_title, observed_url,
              collected_at, updated_at
            )
            SELECT h.event_id, h.video_id, h.watched_at, COALESCE(v.title, ''),
                   'https://www.youtube.com/watch?v=' || h.video_id,
                   COALESCE(h.imported_at, h.updated_at, ?),
                   COALESCE(h.updated_at, h.imported_at, ?)
            FROM history_events h
            LEFT JOIN videos v ON v.video_id = h.video_id
            WHERE h.event_id LIKE 'my_activity:%'
              AND COALESCE(h.watched_at, '') <> ''
            """,
            (utc_now(), utc_now()),
        )
        conn.execute(
            """
            UPDATE history_events
            SET my_activity_event_id = event_id
            WHERE event_id LIKE 'my_activity:%'
              AND my_activity_event_id IS NULL
              AND EXISTS (
                SELECT 1
                FROM my_activity_watch_events source
                WHERE source.event_id = history_events.event_id
              )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_history_my_activity_event
            ON history_events(my_activity_event_id)
            WHERE my_activity_event_id IS NOT NULL
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (13, utc_now()),
        )
    if current_version < 14:
        _deduplicate_my_activity_occurrences(conn)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_my_activity_watch_occurrence
            ON my_activity_watch_events(video_id, watched_at)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_my_activity_subscription_occurrence
            ON my_activity_subscription_events(channel_id, subscribed_at)
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (14, utc_now()),
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


