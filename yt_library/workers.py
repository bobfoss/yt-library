"""Background worker orchestration for library enrichment jobs."""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import (
    config_path,
    configured_archivarix_max_in_flight,
    configured_archivarix_request_timeout,
    configured_archivarix_retry_attempts,
    configured_archivarix_retry_backoff,
    configured_archivarix_stream_timeout,
    configured_dispatch_mode,
    configured_job_dispatch_delay,
    configured_youtube_max_in_flight,
    configured_proxy,
    effective_display_timezone,
)
from .core import (
    DEFAULT_DISPLAY_TIMEZONE,
    DEFAULT_THUMB_DIR,
    DEFAULT_VIDEO_THUMB_DIR,
    HISTORY_BATCH_DELAY_SECONDS,
    HISTORY_BATCH_SIZE,
    HistoryOccurrenceKey,
    HistoryOccurrenceSnapshot,
    LIKED_VIDEOS_PLAYLIST_ID,
    RECENT_HISTORY_BATCH_SIZE,
    RECENT_HISTORY_OVERLAP_DAYS,
    HistoryDayOverlapTracker,
    YouTubeAuthenticationError,
    archivarix_session_status,
    archivarix_timeout_error,
    cache_channel_thumbnail,
    cache_thumbnail,
    cache_video_thumbnail,
    clear_external_service_block,
    connect,
    enqueue_new_history_metadata_targets,
    enqueue_clip_item,
    enqueue_metadata_item,
    enqueue_placeholder_recovery_item,
    enqueue_placeholder_recovery_targets,
    enqueue_playlist_metadata_targets,
    enqueue_playlist_scan_item,
    external_service_block,
    extract_playlist_metadata,
    fetch_playlist_collaboration_metadata,
    fetch_channel_metadata,
    fetch_clip_metadata,
    fetch_current_youtube_clips,
    fetch_current_youtube_playlists,
    fetch_new_channel_metadata_if_needed,
    fetch_watch_metadata,
    fetch_youtube_history_web,
    history_upload_date_conflicts,
    is_system_playlist,
    load_cookie_opener,
    log_live_history_event,
    log_placeholder_recovery_event,
    log_playlist_scan_event,
    log_worker_event,
    log_worker_queue_event,
    metadata_queue_rows,
    placeholder_worker_queue_rows,
    playlist_duplicate_counts,
    playlist_missing_status,
    playlist_scan_is_incomplete,
    playlist_scan_queue_rows,
    playlist_scan_requires_exact_count,
    playlist_zero_result_is_suspicious,
    probe_youtube_authentication_ytdlp,
    rebuild_history_reconciliation,
    rebuild_playlist_reconciliation,
    recover_archivarix_video,
    remove_worker_queue_entry,
    request_text,
    save_discovered_playlists,
    save_discovered_clips,
    save_clip_metadata,
    save_liked_video_reactions,
    save_my_activity_events,
    save_playlist_missing_status,
    save_playlist_scan,
    save_playlist_scan_error,
    save_video_recovery,
    save_youtube_data_api_snapshot,
    save_youtube_history_events,
    scan_playlist_videos,
    scan_playlist_ytdlp,
    set_external_service_block,
    store_channel_metadata,
    store_video_metadata,
    synchronize_youtube_history_order,
    useful_video_metadata,
    utc_now,
    video_metadata_channel_id,
    worker_queue_count,
    worker_queue_order_sql,
    worker_queue_type_count,
    youtube_cookie_diagnostics,
    youtube_history_day_counts,
    youtube_history_occurrence_snapshot,
    youtube_history_order_shift,
    youtube_page_diagnostics,
    youtube_page_requires_login,
    youtube_playlist_is_missing,
    youtube_request_error_diagnostics,
    youtube_session_status,
    youtube_takeout_match_count,
)
from .my_activity import MyActivityError, fetch_my_activity_pages
from .network import ProxyUnavailableError, probe_socks5_proxy
from .plugins import PluginManager, PluginTaskWorker
from .request_pacing import pace_outbound_request
from .youtube_data_api import (
    YouTubeDataApiError,
    YouTubeDataApiNotConfigured,
    build_youtube_data_service,
    fetch_youtube_account_snapshot,
)


_YOUTUBE_AUTH_PROBE_LOCK = threading.Lock()
_YOUTUBE_AUTH_PROBE_CACHE_KEY: tuple[str, int, int, str] | None = None
_YOUTUBE_AUTH_PROBE_CACHE_TIME = 0.0
_YOUTUBE_AUTH_PROBE_CACHE_VALUE = ""
_YOUTUBE_AUTH_PROBE_CACHE_SECONDS = 300.0


def log_history_date_conflicts(
    conn: sqlite3.Connection,
    run_id: str,
    conflicts: list[dict[str, str]],
    *,
    worker_type: str,
) -> None:
    logger = log_live_history_event if worker_type == "history" else log_worker_event
    level = "warn" if worker_type == "history" else "video warn"
    seen: set[tuple[str, str, str]] = set()
    for conflict in conflicts:
        key = (
            conflict["video_id"],
            conflict["watch_date"],
            conflict["published_date"],
        )
        if key in seen:
            continue
        seen.add(key)
        message = (
            f"Watch date {conflict['watch_date']} predates published date "
            f"{conflict['published_date']}; retained because YouTube may republish videos."
        )
        prior = conn.execute(
            """
            SELECT 1
            FROM (
              SELECT video_id, message FROM metadata_worker_log
              UNION ALL
              SELECT video_id, message FROM live_history_worker_log
            )
            WHERE video_id = ? AND message = ?
            LIMIT 1
            """,
            (conflict["video_id"], message),
        ).fetchone()
        if not prior:
            logger(conn, run_id, level, message, conflict["video_id"])


def cached_youtube_authentication_probe(cookie_file: Path, proxy_url: str = "") -> str:
    global _YOUTUBE_AUTH_PROBE_CACHE_KEY
    global _YOUTUBE_AUTH_PROBE_CACHE_TIME
    global _YOUTUBE_AUTH_PROBE_CACHE_VALUE

    try:
        stat = cookie_file.stat()
        cache_key = (str(cookie_file.resolve()), stat.st_mtime_ns, stat.st_size, proxy_url)
    except OSError:
        cache_key = (str(cookie_file.resolve()), 0, 0, proxy_url)
    with _YOUTUBE_AUTH_PROBE_LOCK:
        now = time.monotonic()
        if (
            cache_key == _YOUTUBE_AUTH_PROBE_CACHE_KEY
            and now - _YOUTUBE_AUTH_PROBE_CACHE_TIME < _YOUTUBE_AUTH_PROBE_CACHE_SECONDS
        ):
            return _YOUTUBE_AUTH_PROBE_CACHE_VALUE
        result = probe_youtube_authentication_ytdlp(cookie_file, proxy_url)
        _YOUTUBE_AUTH_PROBE_CACHE_KEY = cache_key
        _YOUTUBE_AUTH_PROBE_CACHE_TIME = time.monotonic()
        _YOUTUBE_AUTH_PROBE_CACHE_VALUE = result
        return result


def youtube_authentication_debug_message(
    exc: YouTubeAuthenticationError,
    cookie_file: Path,
    proxy_url: str = "",
) -> str:
    parts = [
        exc.diagnostics,
        youtube_cookie_diagnostics(cookie_file),
        cached_youtube_authentication_probe(cookie_file, proxy_url),
    ]
    return "YouTube authentication diagnostics: " + " | ".join(part for part in parts if part)


def archivarix_retry_delay(base_seconds: float, retry_number: int) -> float:
    cap = min(30.0, max(0.0, base_seconds) * (2 ** max(0, retry_number - 1)))
    return random.uniform(cap / 2, cap) if cap else 0.0


def record_proxy_hold(
    conn: sqlite3.Connection,
    worker: "_ThreadWorkerLifecycle",
    exc: ProxyUnavailableError,
    *,
    run_id: str = "",
    queue_id: int = 0,
) -> str:
    message = str(exc) or "The configured SOCKS5 proxy is unavailable"
    existing_block = external_service_block(conn, "proxy")
    worker._set_proxy_block_reason(message)
    set_external_service_block(
        conn,
        "proxy",
        "proxy_unavailable",
        message,
        run_id=run_id,
        queue_id=queue_id,
    )
    if not existing_block["blocked"]:
        log_worker_queue_event(
            conn,
            "error",
            (
                "Worker queue paused because the configured proxy is unavailable; "
                f"pending items were retained. {message}"
            ),
            run_id=run_id,
        )
    return message


class _ThreadWorkerLifecycle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_id = ""
        self._blocked_reason = ""
        self._proxy_block_reason = ""

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive()) and not self._stop.is_set()

    def is_stopping(self) -> bool:
        with self._lock:
            return self._stop.is_set() and bool(self._thread and self._thread.is_alive())

    def is_alive(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def blocked_reason(self) -> str:
        with self._lock:
            return self._blocked_reason

    def _set_blocked_reason(self, reason: str) -> None:
        with self._lock:
            self._blocked_reason = reason

    def proxy_block_reason(self) -> str:
        with self._lock:
            return self._proxy_block_reason

    def _set_proxy_block_reason(self, reason: str) -> None:
        with self._lock:
            self._proxy_block_reason = reason

    def _start_background(
        self,
        target: Callable[..., None],
        args_factory: Callable[[str], tuple[Any, ...]],
        *,
        started_message: str,
        already_running_message: str,
        create_run_id: bool = True,
        reset_blocked_reason: bool = False,
        before_start: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                result = {"started": False, "message": already_running_message}
                if create_run_id:
                    result["run_id"] = self._run_id
                return result
            self._stop.clear()
            self._proxy_block_reason = ""
            if reset_blocked_reason:
                self._blocked_reason = ""
            if create_run_id:
                self._run_id = uuid.uuid4().hex
            if before_start:
                before_start()
            self._thread = threading.Thread(
                target=target,
                args=args_factory(self._run_id),
                daemon=True,
            )
            self._thread.start()
            result = {"started": True, "message": started_message}
            if create_run_id:
                result["run_id"] = self._run_id
            return result

    def _request_stop(
        self,
        *,
        not_running_message: str,
        requested_message: str,
        include_run_id: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return {"stopping": False, "message": not_running_message}
            self._stop.set()
            result = {"stopping": True, "message": requested_message}
            if include_run_id:
                result["run_id"] = self._run_id
            return result


class MetadataWorker(_ThreadWorkerLifecycle):
    def __init__(self) -> None:
        super().__init__()

    def start(
        self,
        db_path: Path,
        cookie_file: Path,
        thumb_dir: Path,
        delay: float,
        limit: int,
        force: bool,
        stale_days: int,
        record_summary: bool = True,
        queue_id: int = 0,
        proxy_url: str = "",
        timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
    ) -> dict[str, Any]:
        return self._start_background(
            self._run,
            lambda run_id: (
                run_id,
                db_path,
                cookie_file,
                thumb_dir,
                delay,
                limit,
                force,
                stale_days,
                record_summary,
                queue_id,
                timezone_name,
                proxy_url,
            ),
            started_message="Worker started",
            already_running_message="Worker already running",
            reset_blocked_reason=True,
        )

    def stop(self) -> dict[str, Any]:
        return self._request_stop(
            not_running_message="Worker is not running",
            requested_message="Stop requested",
        )

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
        record_summary: bool,
        target_queue_id: int = 0,
        timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
        proxy_url: str = "",
    ) -> None:
        conn = connect(db_path)
        current_queue_id = 0
        current_subject_id = ""
        try:
            initial_total = 1 if target_queue_id else worker_queue_type_count(conn, "metadata")
            run_total = min(initial_total, limit) if limit else initial_total
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
                        utc_now(),
                        run_total,
                        delay,
                        limit,
                        1 if force else 0,
                        stale_days,
                        "Metadata worker started",
                    ),
                )
                if record_summary:
                    log_worker_event(conn, run_id, "info", f"Queued {initial_total} metadata items")

            processed = 0
            found = 0
            failed = 0
            while True:
                rows = metadata_queue_rows(
                    conn,
                    queue_id=target_queue_id,
                )
                if not rows:
                    break
                if limit and processed >= limit:
                    break
                row = rows[0]
                if self._stop.is_set():
                    with conn:
                        conn.execute(
                            """
                            UPDATE metadata_worker_runs
                            SET status = 'stopped', finished_at = ?, message = ?
                            WHERE run_id = ?
                            """,
                            (utc_now(), "Stop requested", run_id),
                        )
                        log_worker_event(conn, run_id, "warn", "Worker stopped by request")
                    return
                if cookie_file.exists():
                    session_valid, session_message = youtube_session_status(
                        cookie_file,
                        verify_remote=False,
                        proxy_url=proxy_url,
                    )
                    if not session_valid:
                        authentication_error = f"Metadata worker stopped: {session_message}"
                        self._set_blocked_reason(authentication_error)
                        with conn:
                            conn.execute(
                                """
                                UPDATE metadata_worker_runs
                                SET status = 'error', finished_at = ?, message = ?
                                WHERE run_id = ?
                                """,
                                (utc_now(), authentication_error, run_id),
                            )
                            log_worker_event(conn, run_id, "error", authentication_error)
                            log_worker_event(
                                conn,
                                run_id,
                                "debug",
                                f"YouTube cookie diagnostics: {youtube_cookie_diagnostics(cookie_file)}",
                            )
                        return
                opener = load_cookie_opener(cookie_file, proxy_url)
                row_queue_id = int(row["queue_id"]) if "queue_id" in row.keys() else 0
                video_id = row["video_id"]
                metadata_source = row["metadata_source"] if "metadata_source" in row.keys() else "history"
                queued_channel_id = row["channel_id"] if "channel_id" in row.keys() else ""
                queued_channel_title = row["channel_title"] if "channel_title" in row.keys() else ""
                current_queue_id = row_queue_id
                current_subject_id = queued_channel_id if metadata_source == "channel" else video_id
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
                    "reaction": "",
                    "yt_status": "",
                }
                try:
                    if metadata_source == "channel" and queued_channel_id:
                        metadata = fetch_channel_metadata(
                            opener,
                            queued_channel_id,
                            thumb_dir,
                            require_authenticated=cookie_file.exists(),
                            proxy_url=proxy_url,
                        )
                        if not (
                            metadata.get("channel")
                            or metadata.get("channel_url")
                            or metadata.get("channel_thumbnail_path")
                        ):
                            status = "no_metadata"
                    else:
                        metadata = fetch_watch_metadata(
                            opener,
                            video_id,
                            thumb_dir,
                            require_authenticated=cookie_file.exists(),
                        )
                        if not useful_video_metadata(metadata):
                            status = "no_metadata"
                except YouTubeAuthenticationError as exc:
                    authentication_error = f"Metadata worker stopped: {exc}"
                    self._set_blocked_reason(authentication_error)
                    debug_message = youtube_authentication_debug_message(
                        exc,
                        cookie_file,
                        proxy_url,
                    )
                    with conn:
                        conn.execute(
                            """
                            UPDATE metadata_worker_runs
                            SET status = 'error', finished_at = ?, message = ?
                            WHERE run_id = ?
                            """,
                            (utc_now(), authentication_error, run_id),
                        )
                        log_worker_event(conn, run_id, "error", authentication_error)
                        log_worker_event(
                            conn,
                            run_id,
                            "debug",
                            debug_message,
                            queued_channel_id if metadata_source == "channel" else video_id,
                        )
                    return
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                    status = "error"
                    operation = "channel metadata" if metadata_source == "channel" else "watch metadata"
                    error = youtube_request_error_diagnostics(exc, operation)
                channel_metadata: dict[str, str] = {}
                channel_status = ""
                channel_error = ""
                placeholder_queue_message = ""
                if metadata_source != "channel" and status == "ok" and not self._stop.is_set():
                    try:
                        channel_metadata, channel_status, channel_error = fetch_new_channel_metadata_if_needed(
                            conn,
                            opener,
                            thumb_dir,
                            metadata,
                            require_authenticated=cookie_file.exists(),
                            proxy_url=proxy_url,
                        )
                    except YouTubeAuthenticationError as exc:
                        authentication_error = f"Metadata worker stopped: {exc}"
                        self._set_blocked_reason(authentication_error)
                        debug_message = youtube_authentication_debug_message(
                            exc,
                            cookie_file,
                            proxy_url,
                        )
                        with conn:
                            conn.execute(
                                """
                                UPDATE metadata_worker_runs
                                SET status = 'error', finished_at = ?, message = ?
                                WHERE run_id = ?
                                """,
                                (utc_now(), authentication_error, run_id),
                            )
                            log_worker_event(conn, run_id, "error", authentication_error)
                            log_worker_event(
                                conn,
                                run_id,
                                "debug",
                                debug_message,
                                video_metadata_channel_id(metadata) or video_id,
                            )
                        return
                now = utc_now()
                with conn:
                    if channel_status:
                        store_channel_metadata(conn, channel_metadata, channel_status, channel_error, updated_at=now)
                    if metadata_source == "channel":
                        store_channel_metadata(conn, metadata, status, error, updated_at=now)
                    else:
                        store_video_metadata(conn, metadata, status, error, updated_at=now)
                        log_history_date_conflicts(
                            conn,
                            run_id,
                            history_upload_date_conflicts(conn, video_id, timezone_name),
                            worker_type="metadata",
                        )
                        if status == "no_metadata":
                            placeholder_was_queued = enqueue_placeholder_recovery_item(
                                conn,
                                video_id=video_id,
                                current_title=row["current_title"] or "",
                                source_key=row["source_key"] or "",
                                playlist_count=int(row["playlist_count"] or 0),
                                priority=int(row["priority"] or 0),
                                updated_at=now,
                            )
                            placeholder_queue_message = (
                                "placeholder recovery queued"
                                if placeholder_was_queued
                                else "placeholder recovery already queued"
                            )
                    processed += 1
                    channel_subject_id = (
                        str(metadata.get("channel_id") or "").strip()
                        or queued_channel_id
                        or video_id
                    )
                    channel_label = metadata.get("channel") or queued_channel_title or queued_channel_id or video_id
                    if status == "error":
                        failed += 1
                        subject_id = channel_subject_id if metadata_source == "channel" else video_id
                        log_worker_event(conn, run_id, f"{metadata_source} error", error, subject_id)
                    else:
                        found += 1
                        title = metadata.get("title") or video_id
                        if metadata_source == "channel":
                            log_worker_event(
                                conn,
                                run_id,
                                metadata_source,
                                f"{status}: {channel_label}",
                                channel_subject_id,
                            )
                        else:
                            message = (
                                f"no metadata from YouTube; {placeholder_queue_message}"
                                if status == "no_metadata" and placeholder_queue_message
                                else f"{status}: {title}"
                            )
                            log_worker_event(conn, run_id, metadata_source, message, video_id)
                            if channel_status:
                                discovered_channel_label = (
                                    channel_metadata.get("channel")
                                    or metadata.get("channel")
                                    or video_metadata_channel_id(metadata)
                                )
                                discovered_channel_id = (
                                    str(channel_metadata.get("channel_id") or "").strip()
                                    or video_metadata_channel_id(metadata)
                                    or discovered_channel_label
                                )
                                log_worker_event(
                                    conn,
                                    run_id,
                                    "channel",
                                    f"{channel_status}: {discovered_channel_label} (discovered via {title})",
                                    discovered_channel_id,
                                )
                    if row_queue_id:
                        conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (row_queue_id,))
                    remaining = worker_queue_type_count(conn, "metadata")
                    conn.execute(
                        """
                        UPDATE metadata_worker_runs
                        SET total = ?, processed = ?, found = ?, failed = ?, last_video_id = ?, message = ?
                        WHERE run_id = ?
                        """,
                        (
                            run_total,
                            processed,
                            found,
                            failed,
                            video_id,
                            f"Processed {processed} of {run_total}; {remaining} metadata jobs remain queued",
                            run_id,
                        ),
                    )
                if self._stop.is_set():
                    with conn:
                        conn.execute(
                            """
                            UPDATE metadata_worker_runs
                            SET status = 'stopped', finished_at = ?, message = ?
                            WHERE run_id = ?
                            """,
                            (utc_now(), "Stop requested", run_id),
                        )
                        log_worker_event(conn, run_id, "warn", "Worker stopped by request")
                    return
                if delay and worker_queue_type_count(conn, "metadata") > 0:
                    time.sleep(delay)
            with conn:
                conn.execute(
                    """
                    UPDATE metadata_worker_runs
                    SET status = 'complete', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (utc_now(), f"Completed {processed} items", run_id),
                )
                if record_summary:
                    log_worker_event(conn, run_id, "info", f"Worker complete: {processed} processed")
        except ProxyUnavailableError as exc:
            with conn:
                proxy_message = record_proxy_hold(
                    conn,
                    self,
                    exc,
                    run_id=run_id,
                    queue_id=current_queue_id,
                )
                message = f"Metadata worker paused: {proxy_message}"
                conn.execute(
                    """
                    UPDATE metadata_worker_runs
                    SET status = 'blocked', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (utc_now(), message, run_id),
                )
                log_worker_event(
                    conn,
                    run_id,
                    "proxy error",
                    message,
                    current_subject_id,
                )
        except Exception as exc:
            with conn:
                conn.execute(
                    """
                    UPDATE metadata_worker_runs
                    SET status = 'error', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (utc_now(), str(exc), run_id),
                )
                log_worker_event(conn, run_id, "error", f"Worker crashed: {exc}")
        finally:
            conn.close()


METADATA_WORKER = MetadataWorker()


class ClipWorker(_ThreadWorkerLifecycle):
    def start(
        self,
        db_path: Path,
        cookie_file: Path,
        row: dict[str, Any],
        *,
        proxy_url: str = "",
    ) -> dict[str, Any]:
        return self._start_background(
            self._run,
            lambda run_id: (run_id, db_path, cookie_file, dict(row), proxy_url),
            started_message="Clip worker started",
            already_running_message="Clip worker already running",
            reset_blocked_reason=True,
        )

    def stop(self) -> dict[str, Any]:
        return self._request_stop(
            not_running_message="Clip worker is not running",
            requested_message="Stop requested",
        )

    def _run(
        self,
        run_id: str,
        db_path: Path,
        cookie_file: Path,
        row: dict[str, Any],
        proxy_url: str,
    ) -> None:
        queue_id = int(row.get("queue_id") or 0)
        clip_id = str(row.get("clip_id") or "").strip()
        task_type = str(row.get("task_type") or "scan").strip()
        conn = connect(db_path)
        try:
            if cookie_file.exists():
                session_valid, session_message = youtube_session_status(
                    cookie_file,
                    verify_remote=False,
                    proxy_url=proxy_url,
                )
                if not session_valid:
                    message = f"Clip worker stopped: {session_message}"
                    self._set_blocked_reason(message)
                    with conn:
                        log_worker_event(conn, run_id, "clip error", message, clip_id)
                    return
            if task_type == "discover":
                _opener, records = fetch_current_youtube_clips(cookie_file, proxy_url)
                with conn:
                    saved = save_discovered_clips(conn, records)
                    scan_ids = (
                        [str(record.get("clip_id") or "") for record in records]
                        if str(row.get("source_key") or "") == "all"
                        else list(saved["new_ids"])
                    )
                    titles = {
                        str(record.get("clip_id") or ""): str(record.get("title") or "")
                        for record in records
                    }
                    for index, discovered_clip_id in enumerate(scan_ids, start=1):
                        enqueue_clip_item(
                            conn,
                            clip_id=discovered_clip_id,
                            task_type="scan",
                            title=titles.get(discovered_clip_id, ""),
                            priority=100_000 + index,
                            manual=False,
                        )
                    remove_worker_queue_entry(conn, queue_id)
                    log_worker_event(
                        conn,
                        run_id,
                        "clip info",
                        (
                            f"Clip discovery complete: {saved['discovered']} found, "
                            f"{saved['inserted']} new, {len(scan_ids)} metadata queued"
                        ),
                    )
                return

            opener = load_cookie_opener(cookie_file, proxy_url)
            metadata = fetch_clip_metadata(opener, clip_id)
            source_video_id = str(metadata.get("source_video_id") or "").strip()
            source_thumbnail_url = str(metadata.get("source_thumbnail_url") or "").strip()
            if source_video_id and source_thumbnail_url:
                metadata["source_thumbnail_path"] = cache_video_thumbnail(
                    opener,
                    source_video_id,
                    source_thumbnail_url,
                    DEFAULT_VIDEO_THUMB_DIR,
                )
            owner_thumbnail_url = str(metadata.get("owner_thumbnail_url") or "").strip()
            if owner_thumbnail_url:
                metadata["owner_thumbnail_path"] = cache_channel_thumbnail(
                    opener,
                    str(metadata.get("owner_channel_id") or f"clip-{clip_id}"),
                    owner_thumbnail_url,
                    DEFAULT_VIDEO_THUMB_DIR,
                    referer_url=f"https://www.youtube.com/clip/{clip_id}",
                )
            source_channel_thumbnail_url = str(
                metadata.get("source_channel_thumbnail_url") or ""
            ).strip()
            if source_channel_thumbnail_url:
                metadata["source_channel_thumbnail_path"] = cache_channel_thumbnail(
                    opener,
                    str(metadata.get("source_channel_id") or f"clip-source-{clip_id}"),
                    source_channel_thumbnail_url,
                    DEFAULT_VIDEO_THUMB_DIR,
                    referer_url=f"https://www.youtube.com/clip/{clip_id}",
                )
            with conn:
                existing = conn.execute(
                    "SELECT source_video_id FROM clips WHERE clip_id = ?",
                    (clip_id,),
                ).fetchone()
                if not source_video_id and existing:
                    metadata["source_video_id"] = existing["source_video_id"] or ""
                    source_video_id = str(metadata["source_video_id"] or "")
                save_clip_metadata(conn, metadata, fetched=True)
                if source_video_id:
                    source_video = conn.execute(
                        "SELECT title, fetch_status FROM videos WHERE video_id = ?",
                        (source_video_id,),
                    ).fetchone()
                    if source_video and not str(source_video["fetch_status"] or "").strip():
                        enqueue_metadata_item(
                            conn,
                            video_id=source_video_id,
                            current_title=source_video["title"] or "",
                            metadata_source="clip",
                            source_key=clip_id,
                            priority=200_000,
                            manual=False,
                        )
                remove_worker_queue_entry(conn, queue_id)
                log_worker_event(
                    conn,
                    run_id,
                    "clip info",
                    f"ok: {metadata.get('title') or clip_id}",
                    clip_id,
                )
        except YouTubeAuthenticationError as exc:
            message = f"Clip worker stopped: {exc}"
            self._set_blocked_reason(message)
            with conn:
                log_worker_event(conn, run_id, "clip error", message, clip_id)
        except ProxyUnavailableError as exc:
            with conn:
                message = record_proxy_hold(
                    conn,
                    self,
                    exc,
                    run_id=run_id,
                    queue_id=queue_id,
                )
                log_worker_event(
                    conn,
                    run_id,
                    "clip proxy error",
                    f"Clip worker paused: {message}",
                    clip_id,
                )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
            with conn:
                if clip_id:
                    existing = conn.execute(
                        "SELECT source_video_id FROM clips WHERE clip_id = ?",
                        (clip_id,),
                    ).fetchone()
                    save_clip_metadata(
                        conn,
                        {
                            "clip_id": clip_id,
                            "source_video_id": (
                                existing["source_video_id"] or "" if existing else ""
                            ),
                            "availability": "unavailable",
                            "fetch_status": "error",
                            "fetch_error": str(exc),
                        },
                        fetched=True,
                    )
                remove_worker_queue_entry(conn, queue_id)
                log_worker_event(
                    conn,
                    run_id,
                    "clip error",
                    youtube_request_error_diagnostics(exc, "clip metadata"),
                    clip_id,
                )
        finally:
            conn.close()


CLIP_WORKER = ClipWorker()


class PlaylistScanWorker(_ThreadWorkerLifecycle):
    def start(
        self,
        db_path: Path,
        cookie_file: Path,
        delay: float,
        limit: int,
        force: bool,
        stale_days: int,
        record_summary: bool = True,
        proxy_url: str = "",
    ) -> dict[str, Any]:
        return self._start_background(
            self._run,
            lambda run_id: (
                run_id,
                db_path,
                cookie_file,
                delay,
                limit,
                force,
                stale_days,
                record_summary,
                proxy_url,
            ),
            started_message="Playlist scan started",
            already_running_message="Playlist scan already running",
        )

    def stop(self) -> dict[str, Any]:
        return self._request_stop(
            not_running_message="Playlist scan is not running",
            requested_message="Playlist scan stop requested",
        )

    def _run(
        self,
        run_id: str,
        db_path: Path,
        cookie_file: Path,
        delay: float,
        limit: int,
        force: bool,
        stale_days: int,
        record_summary: bool,
        proxy_url: str = "",
    ) -> None:
        conn = connect(db_path)
        opener = load_cookie_opener(cookie_file, proxy_url)
        current_queue_id = 0
        current_playlist_id = ""
        try:
            initial_total = worker_queue_type_count(conn, "playlist")
            run_total = min(initial_total, limit) if limit else initial_total
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
                        utc_now(),
                        run_total,
                        delay,
                        limit,
                        1 if force else 0,
                        stale_days,
                        "Playlist scan worker started",
                    ),
                )
                if record_summary:
                    log_playlist_scan_event(conn, run_id, "info", f"Queued {initial_total} playlists")

            processed = 0
            found = 0
            failed = 0
            while True:
                rows = playlist_scan_queue_rows(conn)
                if not rows:
                    break
                if limit and processed >= limit:
                    break
                row = rows[0]
                if self._stop.is_set():
                    with conn:
                        conn.execute(
                            """
                            UPDATE playlist_scan_worker_runs
                            SET status = 'stopped', finished_at = ?, message = ?
                            WHERE run_id = ?
                            """,
                            (utc_now(), "Stop requested", run_id),
                        )
                        log_playlist_scan_event(conn, run_id, "warn", "Playlist scan stopped by request")
                    return

                queue_id = int(row["queue_id"]) if "queue_id" in row.keys() else 0
                playlist_id = row["playlist_id"]
                title = row["title"] or playlist_id
                current_queue_id = queue_id
                current_playlist_id = playlist_id
                if row["task_type"] == "discover":
                    try:
                        discovery_mode = (
                            str(row["source_key"] or "all").strip().lower()
                            if "source_key" in row.keys()
                            else "all"
                        )
                        if discovery_mode not in {"all", "new"}:
                            discovery_mode = "all"
                        _opener, discovered_records = fetch_current_youtube_playlists(
                            cookie_file,
                            proxy_url=proxy_url,
                        )
                        discovered_records = [
                            record
                            for record in discovered_records
                            if not is_system_playlist(record.get("playlist_id", ""))
                        ]
                        with conn:
                            known_playlist_ids = {
                                existing["playlist_id"]
                                for existing in conn.execute(
                                    "SELECT playlist_id FROM playlists WHERE playlist_id <> ''"
                                )
                            }
                            discovery_stats = save_discovered_playlists(
                                conn,
                                discovered_records,
                            )
                            scan_records = (
                                [
                                    record
                                    for record in discovered_records
                                    if record.get("playlist_id", "") not in known_playlist_ids
                                ]
                                if discovery_mode == "new"
                                else discovered_records
                            )
                            for index, record in enumerate(scan_records, start=1):
                                is_new = record.get("playlist_id", "") not in known_playlist_ids
                                enqueue_playlist_scan_item(
                                    conn,
                                    record.get("playlist_id", ""),
                                    title=record.get("title", ""),
                                    source_key="update" if discovery_mode == "new" else "",
                                    priority=index,
                                    manual=is_new,
                                )
                            if queue_id:
                                conn.execute(
                                    "DELETE FROM worker_queue WHERE queue_id = ?",
                                    (queue_id,),
                                )
                            processed += 1
                            found += int(discovery_stats["inserted"])
                            remaining = worker_queue_type_count(conn, "playlist")
                            message = (
                                "Playlist discovery: "
                                f"{discovery_stats['discovered']} current, "
                                f"{discovery_stats['inserted']} new, "
                                f"{discovery_stats['updated']} existing; "
                                f"{len(scan_records)} scans added, "
                                f"{remaining} playlist scans queued"
                            )
                            log_playlist_scan_event(conn, run_id, "info", message)
                            conn.execute(
                                """
                                UPDATE playlist_scan_worker_runs
                                SET total = ?, processed = ?, found = ?, failed = ?,
                                    last_playlist_id = '', message = ?
                                WHERE run_id = ?
                                """,
                                (
                                    run_total,
                                    processed,
                                    found,
                                    failed,
                                    message,
                                    run_id,
                                ),
                            )
                    except ProxyUnavailableError:
                        raise
                    except Exception as exc:
                        with conn:
                            processed += 1
                            failed += 1
                            message = f"Playlist discovery failed: {exc}"
                            log_playlist_scan_event(conn, run_id, "error", message)
                            if queue_id:
                                conn.execute(
                                    "DELETE FROM worker_queue WHERE queue_id = ?",
                                    (queue_id,),
                                )
                            conn.execute(
                                """
                                UPDATE playlist_scan_worker_runs
                                SET total = ?, processed = ?, found = ?, failed = ?,
                                    last_playlist_id = '', message = ?
                                WHERE run_id = ?
                                """,
                                (
                                    run_total,
                                    processed,
                                    found,
                                    failed,
                                    message,
                                    run_id,
                                ),
                            )
                    if delay and worker_queue_type_count(conn, "playlist") > 0:
                        time.sleep(delay)
                    continue
                status = "ok"
                error = ""
                ytdlp_error = ""
                web_error = ""
                web_attempted = False
                ytdlp_count = 0
                web_count = 0
                videos: list[dict[str, Any]] = []
                playlist_metadata: dict[str, Any] = {}
                header_metadata: dict[str, Any] = {}
                header_page = ""
                header_page_requires_login = False
                missing_status = ""
                youtube_debug = ""
                collaboration_debug = ""
                try:
                    playlist_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
                    header_page = request_text(opener, playlist_url)
                    header_page_requires_login = youtube_page_requires_login(header_page)
                    header_metadata = extract_playlist_metadata(header_page, playlist_id)
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                    header_metadata = {}
                    youtube_debug = youtube_request_error_diagnostics(exc, "playlist header")
                if header_page:
                    try:
                        collaboration_metadata = fetch_playlist_collaboration_metadata(
                            opener,
                            cookie_file,
                            header_page,
                            playlist_url,
                        )
                        header_metadata.update(collaboration_metadata)
                    except ProxyUnavailableError:
                        raise
                    except Exception as exc:
                        collaboration_debug = youtube_request_error_diagnostics(
                            exc,
                            "playlist collaborators",
                        )
                header_count_available = bool(header_metadata.get("has_video_count"))
                if not header_count_available and header_page_requires_login:
                    status = "error"
                    error = "skipping: YouTube login session is not accepted by YouTube"
                    youtube_debug = (
                        youtube_page_diagnostics(header_page, "playlist header")
                        + " | "
                        + youtube_cookie_diagnostics(cookie_file)
                    )
                else:
                    try:
                        videos, playlist_metadata = scan_playlist_ytdlp(
                            playlist_id,
                            cookie_file,
                            proxy_url,
                        )
                    except ProxyUnavailableError:
                        raise
                    except Exception as exc:
                        ytdlp_error = str(exc)
                ytdlp_count = len(videos)
                ytdlp_expected_count = int(playlist_metadata.get("video_count") or 0)
                if (
                    playlist_id != LIKED_VIDEOS_PLAYLIST_ID
                    and youtube_playlist_is_missing(
                        header_page,
                        header_metadata,
                        ytdlp_error,
                    )
                ):
                    missing_status = playlist_missing_status(conn, playlist_id)
                    status = missing_status
                    error = (
                        "authenticated YouTube playlist request returned 404 "
                        "(Requested entity was not found)"
                    )
                if header_metadata.get("video_count"):
                    playlist_metadata["video_count"] = header_metadata["video_count"]
                for key in (
                    "title",
                    "description",
                    "owner",
                    "owner_channel_id",
                    "owner_thumbnail_url",
                    "collaborators",
                    "collaborators_authoritative",
                    "thumbnail_url",
                    "url",
                ):
                    if header_metadata.get(key):
                        playlist_metadata[key] = header_metadata[key]
                if header_metadata.get("visibility"):
                    playlist_metadata["visibility"] = header_metadata["visibility"]
                owner_channel_id = str(playlist_metadata.get("owner_channel_id") or "").strip()
                owner_thumbnail_url = str(playlist_metadata.get("owner_thumbnail_url") or "").strip()
                if owner_channel_id and owner_thumbnail_url:
                    playlist_metadata["owner_thumbnail_path"] = cache_channel_thumbnail(
                        opener,
                        owner_channel_id,
                        owner_thumbnail_url,
                        DEFAULT_VIDEO_THUMB_DIR,
                        referer_url=playlist_url,
                    )
                collaborators = playlist_metadata.get("collaborators") or []
                for collaborator in collaborators:
                    if not isinstance(collaborator, dict):
                        continue
                    collaborator_id = str(collaborator.get("channel_id") or "").strip()
                    collaborator_thumbnail_url = str(
                        collaborator.get("thumbnail_url") or ""
                    ).strip()
                    if collaborator_id and collaborator_thumbnail_url:
                        collaborator["thumbnail_path"] = cache_channel_thumbnail(
                            opener,
                            collaborator_id,
                            collaborator_thumbnail_url,
                            DEFAULT_VIDEO_THUMB_DIR,
                            referer_url=playlist_url,
                        )
                thumbnail_url = str(playlist_metadata.get("thumbnail_url") or "").strip()
                if thumbnail_url:
                    playlist_metadata["thumbnail_path"] = cache_thumbnail(
                        opener,
                        playlist_id,
                        thumbnail_url,
                        DEFAULT_THUMB_DIR,
                    )
                header_expected_count = int(header_metadata.get("video_count") or 0)
                expected_count = header_expected_count or ytdlp_expected_count
                expected_source = (
                    "YouTube playlist header"
                    if header_expected_count
                    else "yt-dlp playlist metadata"
                )
                exact_count_required = (
                    playlist_id != LIKED_VIDEOS_PLAYLIST_ID
                    and playlist_scan_requires_exact_count(
                        header_metadata,
                        known_owner_channel_id=row["owner_channel_id"] if "owner_channel_id" in row.keys() else "",
                        known_visibility=row["visibility"] if "visibility" in row.keys() else "",
                    )
                )
                previous_scan_count = int(row["video_count"] or 0)
                if status == "ok" and not header_count_available and not ytdlp_expected_count:
                    status = "error"
                    error = "skipping: YouTube playlist count unavailable"
                    if ytdlp_error:
                        error += f"; yt-dlp failed: {ytdlp_error[:500]}"
                    elif ytdlp_count:
                        error += (
                            f"; yt-dlp returned {ytdlp_count} entries without "
                            "a reported playlist count"
                        )
                    if youtube_debug:
                        error += "; request diagnostics logged at debug level"
                if status == "ok" and (ytdlp_error or playlist_scan_is_incomplete(ytdlp_count, expected_count)):
                    session_valid, session_message = youtube_session_status(
                        cookie_file,
                        verify_remote=True,
                        proxy_url=proxy_url,
                    )
                    if not session_valid:
                        status = "error"
                        error = f"skipping: {session_message}"
                        youtube_debug = youtube_cookie_diagnostics(cookie_file)
                    else:
                        web_attempted = True
                        try:
                            web_videos = scan_playlist_videos(
                                opener,
                                playlist_id,
                                cookie_file,
                            )
                            web_count = len(web_videos)
                            if web_count >= ytdlp_count:
                                videos = web_videos
                        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as web_exc:
                            web_error = str(web_exc)
                            if ytdlp_error:
                                status = "error"
                                error = f"yt-dlp failed: {ytdlp_error[:500]}; web fallback failed: {web_error[:500]}"
                if status == "ok" and playlist_zero_result_is_suspicious(
                    len(videos),
                    ytdlp_error,
                    previous_scan_count,
                ):
                    status = "error"
                    error = (
                        "Web fallback parsed 0 videos after yt-dlp failed; "
                        f"preserving the previous scan of {previous_scan_count} videos"
                    )
                if status == "ok" and expected_count and not videos:
                    status = "error"
                    error = (
                        "Parsed 0 visible videos, but YouTube playlist header says "
                        f"{expected_count} videos"
                    )
                if (
                    status == "ok"
                    and exact_count_required
                    and playlist_scan_is_incomplete(len(videos), expected_count)
                ):
                    status = "error"
                    if ytdlp_error:
                        error = (
                            f"yt-dlp failed: {ytdlp_error[:500]}; web fallback parsed {web_count} videos, "
                            f"but {expected_source} says {expected_count} videos"
                        )
                    elif web_attempted:
                        error = (
                            f"yt-dlp parsed {ytdlp_count} videos; web fallback parsed {web_count} videos, "
                            f"but {expected_source} says {expected_count} videos"
                        )
                    else:
                        error = f"Parsed {len(videos)} videos, but {expected_source} says {expected_count} videos"
                    if web_error:
                        error += f"; web fallback failed: {web_error[:500]}"
                with conn:
                    metadata_queued = 0
                    placeholder_queued = 0
                    liked_partial_note = ""
                    duplicate_note = ""
                    if status in {"removed", "unavailable"}:
                        video_count, unavailable_count = save_playlist_missing_status(
                            conn,
                            playlist_id,
                            status,
                            error,
                        )
                    elif status == "error" and playlist_id == LIKED_VIDEOS_PLAYLIST_ID:
                        video_count = int(
                            conn.execute("SELECT COUNT(*) FROM videos WHERE reaction = 'L'").fetchone()[0]
                            or 0
                        )
                        unavailable_count = 0
                    elif status == "error":
                        video_count, unavailable_count = save_playlist_scan_error(conn, playlist_id, error)
                    elif playlist_id == LIKED_VIDEOS_PLAYLIST_ID:
                        replace_likes = not expected_count or len(videos) >= expected_count
                        video_count, unavailable_count = save_liked_video_reactions(
                            conn,
                            videos,
                            replace=replace_likes,
                        )
                        if not replace_likes:
                            liked_total = int(
                                conn.execute(
                                    "SELECT COUNT(*) FROM videos WHERE reaction = 'L'"
                                ).fetchone()[0]
                                or 0
                            )
                            liked_partial_note = (
                                f"; partial result merged, {liked_total} canonical likes retained"
                            )
                    else:
                        video_count, unavailable_count = save_playlist_scan(
                            conn,
                            playlist_id,
                            videos,
                            status,
                            error,
                            playlist_metadata=playlist_metadata,
                        )
                        duplicate_video_count, duplicate_occurrence_count = (
                            playlist_duplicate_counts(videos)
                        )
                        if duplicate_occurrence_count:
                            duplicate_note = (
                                f"; {duplicate_occurrence_count} duplicate "
                                f"occurrence{'s' if duplicate_occurrence_count != 1 else ''} "
                                f"across {duplicate_video_count} "
                                f"video{'s' if duplicate_video_count != 1 else ''}"
                            )
                        if bool(row["manual"]) and video_count:
                            metadata_result = enqueue_playlist_metadata_targets(conn, playlist_id)
                            metadata_queued = int(metadata_result["queued_count"])
                        if playlist_id != LIKED_VIDEOS_PLAYLIST_ID:
                            placeholder_result = enqueue_placeholder_recovery_targets(
                                conn,
                                playlist_id,
                            )
                            placeholder_queued = int(placeholder_result["inserted"])
                    processed += 1
                    if status == "error":
                        failed += 1
                        log_playlist_scan_event(conn, run_id, "error", f"{title}: {error}", playlist_id)
                        if youtube_debug:
                            log_playlist_scan_event(
                                conn,
                                run_id,
                                "debug",
                                f"{title}: YouTube diagnostics: {youtube_debug}",
                                playlist_id,
                            )
                    elif status in {"removed", "unavailable"}:
                        found += 1
                        log_playlist_scan_event(
                            conn,
                            run_id,
                            "info",
                            (
                                f"{title}: marked {status} after authenticated YouTube 404; "
                                f"preserved {video_count} videos"
                            ),
                            playlist_id,
                        )
                    else:
                        found += 1
                        reported_note = ""
                        count_change_note = ""
                        if (
                            expected_count
                            and not exact_count_required
                            and video_count < expected_count
                        ):
                            reported_note = f"; {video_count} exposed of {expected_count} reported"
                        if row["scanned_at"] and video_count != previous_scan_count:
                            count_delta = video_count - previous_scan_count
                            count_change_note = (
                                f"; count changed {previous_scan_count} -> {video_count} "
                                f"({count_delta:+d})"
                            )
                        log_playlist_scan_event(
                            conn,
                            run_id,
                            "info",
                            (
                                f"{title}: {video_count} videos, {unavailable_count} unavailable"
                                + reported_note
                                + count_change_note
                                + duplicate_note
                                + liked_partial_note
                                + (f"; queued {metadata_queued} metadata items" if metadata_queued else "")
                                + (f"; queued {placeholder_queued} placeholder recoveries" if placeholder_queued else "")
                            ),
                            playlist_id,
                        )
                    if collaboration_debug:
                        log_playlist_scan_event(
                            conn,
                            run_id,
                            "debug",
                            f"{title}: YouTube collaborator diagnostics: {collaboration_debug}",
                            playlist_id,
                        )
                    if queue_id:
                        conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (queue_id,))
                    remaining = worker_queue_type_count(conn, "playlist")
                    conn.execute(
                        """
                        UPDATE playlist_scan_worker_runs
                        SET total = ?, processed = ?, found = ?, failed = ?, last_playlist_id = ?, message = ?
                        WHERE run_id = ?
                        """,
                        (
                            run_total,
                            processed,
                            found,
                            failed,
                            playlist_id,
                            f"Processed {processed} of {run_total}; {remaining} playlist jobs remain queued",
                            run_id,
                        ),
                    )
                if delay and worker_queue_type_count(conn, "playlist") > 0:
                    time.sleep(delay)
            with conn:
                conn.execute(
                    """
                    UPDATE playlist_scan_worker_runs
                    SET status = 'complete', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (utc_now(), f"Completed {processed} playlists", run_id),
                )
                if record_summary:
                    log_playlist_scan_event(conn, run_id, "info", f"Playlist scan complete: {processed} processed")
        except ProxyUnavailableError as exc:
            with conn:
                proxy_message = record_proxy_hold(
                    conn,
                    self,
                    exc,
                    run_id=run_id,
                    queue_id=current_queue_id,
                )
                message = f"Playlist scan paused: {proxy_message}"
                conn.execute(
                    """
                    UPDATE playlist_scan_worker_runs
                    SET status = 'blocked', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (utc_now(), message, run_id),
                )
                log_playlist_scan_event(
                    conn,
                    run_id,
                    "proxy error",
                    message,
                    current_playlist_id,
                )
        except Exception as exc:
            with conn:
                conn.execute(
                    """
                    UPDATE playlist_scan_worker_runs
                    SET status = 'error', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (utc_now(), str(exc), run_id),
                )
                log_playlist_scan_event(
                    conn,
                    run_id,
                    "error",
                    f"Playlist scan crashed: {exc}",
                    current_playlist_id,
                )
        finally:
            conn.close()


PLAYLIST_SCAN_WORKER = PlaylistScanWorker()


class LiveHistoryWorker(_ThreadWorkerLifecycle):
    def start(
        self,
        db_path: Path,
        cookie_file: Path,
        mode: str,
        timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
        proxy_url: str = "",
    ) -> dict[str, Any]:
        label = "Verify history" if mode == "verify" else "History fetch"
        return self._start_background(
            self._run,
            lambda run_id: (
                run_id,
                db_path,
                cookie_file,
                mode,
                timezone_name,
                proxy_url,
            ),
            started_message=f"{label} started",
            already_running_message="History fetch already running",
        )

    def stop(self) -> dict[str, Any]:
        return self._request_stop(
            not_running_message="History fetch is not running",
            requested_message="History fetch stop requested",
        )

    def _run(
        self,
        run_id: str,
        db_path: Path,
        cookie_file: Path,
        mode: str,
        timezone_name: str,
        proxy_url: str = "",
    ) -> None:
        conn = connect(db_path)
        mode = "verify" if mode == "verify" else "recent"
        label = "Verify history" if mode == "verify" else "History fetch"
        batch_size = HISTORY_BATCH_SIZE if mode == "verify" else RECENT_HISTORY_BATCH_SIZE
        occurrence_snapshot: HistoryOccurrenceSnapshot = {}
        seen_occurrences: Counter[HistoryOccurrenceKey] = Counter()
        assigned_event_ids: set[str] = set()
        assignments: list[dict[str, Any]] = []
        overlap_tracker: HistoryDayOverlapTracker | None = None
        processed = 0
        inserted_total = 0
        skipped_total = 0
        takeout_matches_total = 0
        metadata_queued_ids: set[str] = set()
        queue_row = conn.execute(
            """
            SELECT queue_id
            FROM worker_queue
            WHERE worker_type = 'history' AND task_type = ?
            ORDER BY priority, queue_id
            LIMIT 1
            """,
            (mode,),
        ).fetchone()
        current_queue_id = int(queue_row["queue_id"] or 0) if queue_row else 0
        try:
            occurrence_snapshot = youtube_history_occurrence_snapshot(conn)
            overlap_tracker = (
                HistoryDayOverlapTracker(youtube_history_day_counts(occurrence_snapshot))
                if mode == "recent"
                else None
            )
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
                        utc_now(),
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
                        (utc_now(), "Stopped before fetch", run_id),
                    )
                    log_live_history_event(conn, run_id, "warn", "History fetch stopped before fetch")
                return

            start = 1
            last_video_id = ""
            final_message = ""
            completion_reason = ""
            while not self._stop.is_set():
                end = start + batch_size - 1
                rows = fetch_youtube_history_web(
                    cookie_file,
                    limit=batch_size,
                    start=start,
                    timezone_name=timezone_name,
                    proxy_url=proxy_url,
                )
                seen = len(rows)
                overlap_reached = overlap_tracker.add_rows(rows) if overlap_tracker else False
                if overlap_tracker and seen < batch_size:
                    overlap_reached = overlap_tracker.finish()
                with conn:
                    save_stats = save_youtube_history_events(
                        conn,
                        rows,
                        start,
                        occurrence_snapshot,
                        seen_occurrences,
                        timezone_name,
                    )
                    log_history_date_conflicts(
                        conn,
                        run_id,
                        save_stats["date_conflicts"],
                        worker_type="history",
                    )
                    for guard in save_stats["progress_guards"]:
                        log_live_history_event(
                            conn,
                            run_id,
                            "info",
                            (
                                f"watch percentage: {guard['reported']}% reported by YouTube; "
                                f"{guard['retained']}% retained"
                            ),
                            guard["video_id"],
                        )
                    processed += seen
                    inserted_total += save_stats["new"]
                    skipped_total += save_stats["existing"]
                    assignments.extend(save_stats["assignments"])
                    assigned_event_ids.update(
                        assignment["event_id"] for assignment in save_stats["assignments"]
                    )
                    matching_days = (
                        set(overlap_tracker.confirmed_days)
                        if overlap_tracker and overlap_reached
                        else None
                    )
                    shift = youtube_history_order_shift(
                        assignments,
                        matching_days=matching_days,
                        fallback=inserted_total,
                    )
                    complete_scan = seen < batch_size and bool(processed)
                    synchronize_youtube_history_order(
                        conn,
                        occurrence_snapshot,
                        assigned_event_ids,
                        processed=processed,
                        shift=shift,
                        final=complete_scan or overlap_reached,
                        complete_scan=complete_scan,
                    )
                    rebuild_history_reconciliation(conn, timezone_name)
                    batch_metadata_ids = enqueue_new_history_metadata_targets(
                        conn,
                        save_stats["assignments"],
                        excluded_video_ids=metadata_queued_ids,
                    )
                    metadata_queued_ids.update(batch_metadata_ids)
                    batch_takeout_matches = youtube_takeout_match_count(conn, start, seen)
                    takeout_matches_total += batch_takeout_matches
                    batch_last_video_id = save_stats["last_video_id"]
                    if batch_last_video_id:
                        last_video_id = batch_last_video_id
                    final_message = (
                        f"{label}: entries {start}-{end}; {seen} fetched, "
                        f"{save_stats['new']} new watches, "
                        f"{save_stats['existing']} existing watches, "
                        f"{batch_takeout_matches} Takeout matches, "
                        f"{len(batch_metadata_ids)} metadata queued"
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
                    log_live_history_event(conn, run_id, "info", final_message)
                if seen < batch_size:
                    completion_reason = "reached the end of available history"
                    break
                if mode == "recent" and overlap_reached:
                    completion_reason = (
                        f"reached {RECENT_HISTORY_OVERLAP_DAYS} matching complete days "
                        f"({', '.join(overlap_tracker.confirmed_days)})"
                    )
                    break
                if self._stop.wait(HISTORY_BATCH_DELAY_SECONDS):
                    break
                start += batch_size

            takeout_matches_total = youtube_takeout_match_count(conn, 1, processed)
            status = "stopped" if self._stop.is_set() else "complete"
            if not final_message:
                final_message = f"{label}: no history rows fetched"
            elif status == "complete":
                final_message = (
                    f"{label} complete"
                    f"{f' ({completion_reason})' if completion_reason else ''}: "
                    f"{processed} fetched, {inserted_total} new watches, "
                    f"{skipped_total} existing watches, "
                    f"{takeout_matches_total} Takeout matches, "
                    f"{len(metadata_queued_ids)} metadata queued"
                )
            else:
                final_message = (
                    f"{label} stopped: {processed} fetched, "
                    f"{inserted_total} new watches, {skipped_total} existing watches, "
                    f"{takeout_matches_total} Takeout matches, "
                    f"{len(metadata_queued_ids)} metadata queued"
                )
            with conn:
                if status == "complete":
                    conn.execute(
                        """
                        DELETE FROM worker_queue
                        WHERE worker_type = 'history' AND task_type = ?
                        """,
                        (mode,),
                    )
                conn.execute(
                    """
                    UPDATE live_history_worker_runs
                    SET status = ?, finished_at = ?, total = ?, processed = ?,
                        found = ?, skipped = ?, last_video_id = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (
                        status,
                        utc_now(),
                        processed,
                        processed,
                        inserted_total,
                        skipped_total,
                        last_video_id,
                        final_message,
                        run_id,
                    ),
                )
                log_live_history_event(conn, run_id, "info" if status == "complete" else "warn", final_message)
        except ProxyUnavailableError as exc:
            with conn:
                proxy_message = record_proxy_hold(
                    conn,
                    self,
                    exc,
                    run_id=run_id,
                    queue_id=current_queue_id,
                )
                message = f"{label} paused: {proxy_message}"
                conn.execute(
                    """
                    UPDATE live_history_worker_runs
                    SET status = 'blocked', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (utc_now(), message, run_id),
                )
                log_live_history_event(
                    conn,
                    run_id,
                    "proxy error",
                    message,
                )
        except Exception as exc:
            if isinstance(exc, YouTubeAuthenticationError):
                error_message = str(exc)
                debug_message = youtube_authentication_debug_message(
                    exc,
                    cookie_file,
                    proxy_url,
                )
            elif isinstance(
                exc,
                (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError),
            ):
                error_message = youtube_request_error_diagnostics(exc, "history fetch")
                debug_message = f"YouTube cookie diagnostics: {youtube_cookie_diagnostics(cookie_file)}"
            else:
                error_message = str(exc)
                debug_message = ""
            with conn:
                conn.execute(
                    """
                    UPDATE live_history_worker_runs
                    SET status = 'error', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (utc_now(), error_message, run_id),
                )
                log_live_history_event(conn, run_id, "error", f"History fetch crashed: {error_message}")
                if debug_message:
                    log_live_history_event(conn, run_id, "debug", debug_message)
        finally:
            conn.close()


LIVE_HISTORY_WORKER = LiveHistoryWorker()


class PlaceholderRecoveryWorker(_ThreadWorkerLifecycle):
    def start(
        self,
        db_path: Path,
        archivarix_cookie_file: Path,
        thumb_dir: Path,
        queue_id: int = 0,
        request_timeout: float = 15.0,
        stream_timeout: float = 30.0,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 2.0,
        proxy_url: str = "",
    ) -> dict[str, Any]:
        return self._start_background(
            self._run,
            lambda run_id: (
                run_id,
                db_path,
                archivarix_cookie_file,
                thumb_dir,
                queue_id,
                request_timeout,
                stream_timeout,
                retry_attempts,
                retry_backoff_seconds,
                proxy_url,
            ),
            started_message="Placeholder recovery started",
            already_running_message="Placeholder recovery already running",
            reset_blocked_reason=True,
        )

    def stop(self) -> dict[str, Any]:
        return self._request_stop(
            not_running_message="Placeholder recovery is not running",
            requested_message="Placeholder recovery stop requested",
        )

    @staticmethod
    def _finish_run(
        conn: sqlite3.Connection,
        run_id: str,
        *,
        status: str,
        message: str,
        recovery_status: str = "",
        processed: int = 0,
        found: int = 0,
        failed: int = 0,
    ) -> None:
        conn.execute(
            """
            UPDATE placeholder_recovery_worker_runs
            SET status = ?, finished_at = ?, processed = ?, found = ?, failed = ?,
                recovery_status = ?, message = ?
            WHERE run_id = ?
            """,
            (status, utc_now(), processed, found, failed, recovery_status, message, run_id),
        )

    def _run(
        self,
        run_id: str,
        db_path: Path,
        archivarix_cookie_file: Path,
        thumb_dir: Path,
        queue_id: int = 0,
        request_timeout: float = 15.0,
        stream_timeout: float = 30.0,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 2.0,
        proxy_url: str = "",
    ) -> None:
        conn = connect(db_path)
        video_id = ""
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO placeholder_recovery_worker_runs(
                      run_id, status, started_at, total, queue_id, message
                    )
                    VALUES (?, 'running', ?, 1, ?, ?)
                    """,
                    (run_id, utc_now(), queue_id, "Placeholder recovery started"),
                )
            rows = placeholder_worker_queue_rows(conn, limit=1, queue_id=queue_id)
            if not rows:
                with conn:
                    conn.execute(
                        "UPDATE placeholder_recovery_worker_runs SET total = 0 WHERE run_id = ?",
                        (run_id,),
                    )
                    self._finish_run(
                        conn,
                        run_id,
                        status="complete",
                        message="No placeholder recovery item queued",
                    )
                    log_placeholder_recovery_event(
                        conn,
                        run_id,
                        "info",
                        "No placeholder recovery item queued",
                    )
                return
            row = rows[0]
            queue_id = int(row["queue_id"] or 0)
            playlist_id = row["playlist_id"] or ""
            video_id = row["video_id"]
            title = row["current_title"] or video_id
            with conn:
                conn.execute(
                    """
                    UPDATE placeholder_recovery_worker_runs
                    SET queue_id = ?, video_id = ?, playlist_id = ?
                    WHERE run_id = ?
                    """,
                    (queue_id, video_id, playlist_id, run_id),
                )
            if self._stop.is_set():
                with conn:
                    self._finish_run(conn, run_id, status="stopped", message="Stop requested")
                    log_placeholder_recovery_event(conn, run_id, "warn", "Stop requested", video_id)
                return
            session_valid, session_message = archivarix_session_status(archivarix_cookie_file)
            if not session_valid:
                self._set_blocked_reason(session_message)
                with conn:
                    set_external_service_block(
                        conn,
                        "archivarix",
                        "authentication_error",
                        session_message,
                        run_id=run_id,
                        queue_id=queue_id,
                    )
                    self._finish_run(
                        conn,
                        run_id,
                        status="blocked",
                        message=session_message,
                        recovery_status="authentication_error",
                        failed=1,
                    )
                    log_placeholder_recovery_event(conn, run_id, "warn", session_message, video_id)
                return
            archivarix_opener = load_cookie_opener(archivarix_cookie_file, proxy_url)
            status = "not_found"
            error = ""
            video: dict[str, Any] | None = None
            thumbnail_url = ""
            thumbnail_path = ""
            attempts = max(1, int(retry_attempts))
            for attempt in range(1, attempts + 1):
                with conn:
                    conn.execute(
                        """
                        UPDATE placeholder_recovery_worker_runs
                        SET request_started_at = ?, request_count = request_count + 1
                        WHERE run_id = ?
                        """,
                        (utc_now(), run_id),
                    )
                try:
                    video, thumbnail_url, thumbnail_path, status, error = recover_archivarix_video(
                        video_id,
                        thumb_dir,
                        archivarix_opener,
                        refresh_metadata=True,
                        no_api=False,
                        delay=0.0,
                        channel_cache={},
                        stop_event=self._stop,
                        request_timeout=request_timeout,
                        stream_timeout=stream_timeout,
                        thumbnail_timeout=request_timeout,
                        channel_thumbnail_timeout=stream_timeout,
                    )
                except ProxyUnavailableError as exc:
                    with conn:
                        proxy_message = record_proxy_hold(
                            conn,
                            self,
                            exc,
                            run_id=run_id,
                            queue_id=queue_id,
                        )
                        message = f"Placeholder recovery paused: {proxy_message}"
                        self._finish_run(
                            conn,
                            run_id,
                            status="blocked",
                            message=message,
                            recovery_status="proxy_unavailable",
                            failed=1,
                        )
                        log_placeholder_recovery_event(
                            conn,
                            run_id,
                            "proxy error",
                            message,
                            video_id,
                        )
                    return
                except (
                    urllib.error.HTTPError,
                    urllib.error.URLError,
                    TimeoutError,
                    json.JSONDecodeError,
                    OSError,
                ) as exc:
                    status = "timeout" if archivarix_timeout_error(exc) else "error"
                    error = str(exc)
                if self._stop.is_set():
                    with conn:
                        self._finish_run(
                            conn,
                            run_id,
                            status="stopped",
                            message="Stop requested",
                            recovery_status=status,
                        )
                        log_placeholder_recovery_event(conn, run_id, "warn", "Stop requested", video_id)
                    return
                if status != "timeout" or attempt == attempts:
                    break
                delay = archivarix_retry_delay(retry_backoff_seconds, attempt)
                with conn:
                    log_placeholder_recovery_event(
                        conn,
                        run_id,
                        "warn",
                        (
                            f"Archivarix timeout on attempt {attempt}/{attempts}; "
                            f"retrying in {delay:.1f} seconds: {error or 'request timed out'}"
                        ),
                        video_id,
                    )
                if self._stop.wait(delay):
                    with conn:
                        self._finish_run(
                            conn,
                            run_id,
                            status="stopped",
                            message="Stop requested",
                            recovery_status="timeout",
                        )
                        log_placeholder_recovery_event(conn, run_id, "warn", "Stop requested", video_id)
                    return

            save_video_recovery(
                conn,
                video_id,
                video,
                status,
                error,
                thumbnail_url,
                thumbnail_path,
            )
            title = (video or {}).get("title") or title

            with conn:
                if status == "rate_limited":
                    message = error or "Archivarix daily search limit reached"
                    self._set_blocked_reason(message)
                    set_external_service_block(
                        conn,
                        "archivarix",
                        "rate_limited",
                        message,
                        run_id=run_id,
                        queue_id=queue_id,
                    )
                    self._finish_run(
                        conn,
                        run_id,
                        status="blocked",
                        message=message,
                        recovery_status=status,
                        processed=1,
                        failed=1,
                    )
                    log_placeholder_recovery_event(conn, run_id, "warn", message, video_id)
                    return
                if status in {"timeout", "error"}:
                    if status == "timeout":
                        message = (
                            f"Archivarix timed out after {attempts} attempts; "
                            f"queue item retained: {error or 'request timed out'}"
                        )
                        reason_code = "timeout"
                        level = "warn"
                    else:
                        message = f"Archivarix request failed; queue item retained: {error or status}"
                        reason_code = "request_error"
                        level = "error"
                    self._set_blocked_reason(message)
                    set_external_service_block(
                        conn,
                        "archivarix",
                        reason_code,
                        message,
                        run_id=run_id,
                        queue_id=queue_id,
                    )
                    self._finish_run(
                        conn,
                        run_id,
                        status="blocked",
                        message=message,
                        recovery_status=status,
                        processed=1,
                        failed=1,
                    )
                    log_placeholder_recovery_event(conn, run_id, level, message, video_id)
                    return
                if status == "found":
                    level = "found"
                    message = f"found: {title}"
                elif status == "thumbnail_only":
                    level = "thumbnail"
                    message = f"thumbnail only: {title}"
                elif status == "not_found":
                    level = "not found"
                    message = "not found"
                if queue_id:
                    conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (queue_id,))
                rebuild_playlist_reconciliation(conn, playlist_id)
                self._finish_run(
                    conn,
                    run_id,
                    status="complete",
                    message=message,
                    recovery_status=status,
                    processed=1,
                    found=1 if status in {"found", "thumbnail_only"} else 0,
                    failed=1 if status == "error" else 0,
                )
                log_placeholder_recovery_event(conn, run_id, level, message, video_id)
        except ProxyUnavailableError as exc:
            with conn:
                proxy_message = record_proxy_hold(
                    conn,
                    self,
                    exc,
                    run_id=run_id,
                    queue_id=queue_id,
                )
                message = f"Placeholder recovery paused: {proxy_message}"
                self._finish_run(
                    conn,
                    run_id,
                    status="blocked",
                    message=message,
                    recovery_status="proxy_unavailable",
                    failed=1,
                )
                log_placeholder_recovery_event(
                    conn,
                    run_id,
                    "proxy error",
                    message,
                    video_id,
                )
        except Exception as exc:
            with conn:
                self._finish_run(
                    conn,
                    run_id,
                    status="error",
                    message=str(exc),
                    recovery_status="error",
                    failed=1,
                )
                log_placeholder_recovery_event(conn, run_id, "error", f"Worker crashed: {exc}", video_id)
        finally:
            conn.close()


PLACEHOLDER_RECOVERY_WORKER = PlaceholderRecoveryWorker()


def run_optional_account_sync(
    db_path: Path,
    config: dict[str, Any],
    timezone_name: str,
    queue_id: int,
) -> None:
    """Collect optional account-level timestamp sources without blocking other work."""

    my_activity_cookie = config_path(config, "my_activity_cookies")
    if not my_activity_cookie.is_file():
        conn = connect(db_path)
        try:
            with conn:
                log_worker_queue_event(
                    conn,
                    "info",
                    "Google My Activity cookies are not configured; exact watch and subscription dates may be incomplete.",
                )
        finally:
            conn.close()
    else:
        try:
            pages = fetch_my_activity_pages(
                my_activity_cookie,
                max_pages=25,
                proxy_url=configured_proxy(config),
            )
            watch_events = sorted(
                {event.event_id: event for page in pages for event in page.events}.values(),
                key=lambda event: (event.watched_at, event.event_id),
                reverse=True,
            )
            subscription_events = sorted(
                {
                    event.event_id: event
                    for page in pages
                    for event in page.subscription_events
                }.values(),
                key=lambda event: (event.subscribed_at, event.event_id),
                reverse=True,
            )
            conn = connect(db_path)
            try:
                with conn:
                    stats = save_my_activity_events(
                        conn,
                        watch_events,
                        subscription_events,
                        timezone_name,
                    )
                    gap = bool(
                        not stats["first_collection"] and not stats["overlap_events"]
                    )
                    level = "warn" if gap else "info"
                    suffix = (
                        " No stored event overlapped this scan, so a collection gap may exist."
                        if gap
                        else ""
                    )
                    log_worker_queue_event(
                        conn,
                        level,
                        (
                            "My Activity sync: "
                            f"{stats['watch_inserted']} new watches, "
                            f"{stats['subscription_inserted']} new subscriptions, "
                            f"{stats['overlap_events']} overlapping events across "
                            f"{len(pages)} pages.{suffix}"
                        ),
                    )
            finally:
                conn.close()
        except (MyActivityError, OSError, RuntimeError) as exc:
            conn = connect(db_path)
            try:
                with conn:
                    log_worker_queue_event(
                        conn,
                        "warn",
                        f"My Activity sync was skipped: {exc}",
                    )
            finally:
                conn.close()

    token_path = config_path(config, "youtube_oauth_token")
    if not token_path.is_file():
        conn = connect(db_path)
        try:
            with conn:
                log_worker_queue_event(
                    conn,
                    "info",
                    "YouTube Data API OAuth is not configured; subscription and playlist published dates may be incomplete.",
                )
        finally:
            conn.close()
    else:
        try:
            service = build_youtube_data_service(
                config_path(config, "youtube_oauth_client_secrets"),
                token_path,
                configured_proxy(config),
            )
            snapshot = fetch_youtube_account_snapshot(
                service,
                before_request=pace_outbound_request,
            )
            conn = connect(db_path)
            try:
                with conn:
                    stats = save_youtube_data_api_snapshot(conn, snapshot)
                    log_worker_queue_event(
                        conn,
                        "info",
                        (
                            "YouTube Data API sync: "
                            f"{stats['subscriptions']} subscriptions, "
                            f"{stats['playlists']} playlists, and "
                            f"{stats['playlist_items']} playlist items; "
                            f"{stats['playlist_items_unmatched']} timestamps could not be matched."
                        ),
                    )
            finally:
                conn.close()
        except (YouTubeDataApiNotConfigured, YouTubeDataApiError, OSError, RuntimeError) as exc:
            conn = connect(db_path)
            try:
                with conn:
                    log_worker_queue_event(
                        conn,
                        "warn",
                        f"YouTube Data API sync was skipped: {exc}",
                    )
            finally:
                conn.close()

    conn = connect(db_path)
    try:
        with conn:
            remove_worker_queue_entry(conn, queue_id)
    finally:
        conn.close()


class WorkerQueueDispatcher(_ThreadWorkerLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self._placeholder_block_reason = ""
        self._started_at = ""
        self._started_monotonic = 0.0
        self._initial_count = 0
        self._completed_count = 0
        self._metadata_workers: dict[int, tuple[MetadataWorker, str]] = {}
        self._clip_workers: dict[int, tuple[ClipWorker, str]] = {}
        self._placeholder_workers: dict[int, tuple[PlaceholderRecoveryWorker, str]] = {}
        self._plugin_workers: dict[
            int, tuple[PluginTaskWorker, str, str, str]
        ] = {}
        self._plugin_manager: PluginManager | None = None
        self._archivarix_retry_requested = threading.Event()
        self._proxy_retry_requested = threading.Event()
        self._dispatch_mode = "delay"
        self._job_dispatch_delay = 0.0
        self._youtube_max_in_flight = 1
        self._archivarix_max_in_flight = 1
        self._dispatch_revision = 0

    def stats(self, remaining_count: int) -> dict[str, Any]:
        with self._lock:
            active = bool(self._thread and self._thread.is_alive())
            elapsed = max(0.0, time.monotonic() - self._started_monotonic) if active and self._started_monotonic else 0.0
            completed = self._completed_count
            initial = self._initial_count
            started_at = self._started_at if active else ""
            plugin_services = [
                service
                for _worker, service, _plugin_id, _worker_id in self._plugin_workers.values()
            ]
            youtube_in_flight = (
                len(self._metadata_workers)
                + len(self._clip_workers)
                + plugin_services.count("youtube")
            )
            archivarix_in_flight = len(self._placeholder_workers) + plugin_services.count(
                "archivarix"
            )
            plugin_in_flight = len(self._plugin_workers)
        remaining = max(0, int(remaining_count or 0))
        eta_seconds = 0.0
        if active and completed > 0:
            eta_seconds = max(0.0, (elapsed / completed) * remaining)
        return {
            "started_at": started_at,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta_seconds,
            "eta_available": bool(active and completed > 0),
            "initial_count": initial,
            "completed_count": completed,
            "remaining_count": remaining,
            "youtube_in_flight": youtube_in_flight,
            "archivarix_in_flight": archivarix_in_flight,
            "plugin_in_flight": plugin_in_flight,
        }

    def start(
        self,
        db_path: Path,
        cookie_file: Path,
        thumb_dir: Path,
        config_data: dict[str, Any] | None = None,
        plugin_manager: PluginManager | None = None,
    ) -> dict[str, Any]:
        config = config_data or {}
        dispatch_mode = configured_dispatch_mode(config)
        job_dispatch_delay = configured_job_dispatch_delay(config)
        youtube_max_in_flight = configured_youtube_max_in_flight(config)
        archivarix_max_in_flight = configured_archivarix_max_in_flight(config)
        proxy_url = configured_proxy(config)

        conn = connect(db_path)
        try:
            proxy_block = external_service_block(conn, "proxy")
            if proxy_block["blocked"]:
                proxy_available, proxy_message = probe_socks5_proxy(proxy_url)
                with conn:
                    if proxy_available:
                        clear_external_service_block(conn, "proxy")
                        log_worker_queue_event(
                            conn,
                            "info",
                            "Proxy connectivity restored; worker queue hold cleared.",
                        )
                    else:
                        log_worker_queue_event(
                            conn,
                            "error",
                            (
                                "Worker queue start blocked because the configured "
                                f"proxy is unavailable. {proxy_message}"
                            ),
                        )
                if not proxy_available:
                    return {
                        "started": False,
                        "blocked": True,
                        "message": proxy_message,
                    }
                self.allow_proxy_retry()
        finally:
            conn.close()

        def prepare_run() -> None:
            self._started_at = utc_now()
            self._started_monotonic = time.monotonic()
            self._initial_count = 0
            self._completed_count = 0
            self._metadata_workers = {}
            self._clip_workers = {}
            self._placeholder_workers = {}
            self._plugin_workers = {}
            self._plugin_manager = plugin_manager
            self._archivarix_retry_requested.clear()
            self._proxy_retry_requested.clear()
            self._set_dispatch_settings_unlocked(
                dispatch_mode,
                job_dispatch_delay,
                youtube_max_in_flight,
                archivarix_max_in_flight,
            )

        return self._start_background(
            self._run,
            lambda _run_id: (
                db_path,
                cookie_file,
                thumb_dir,
                effective_display_timezone(config),
                config_path(config, "archivarix_cookies"),
                config_path(config, "archivarix_thumbnail_dir"),
                configured_archivarix_request_timeout(config),
                configured_archivarix_stream_timeout(config),
                configured_archivarix_retry_attempts(config),
                configured_archivarix_retry_backoff(config),
                proxy_url,
                dict(config),
            ),
            started_message="Worker queue dispatcher started",
            already_running_message="Worker queue dispatcher already running",
            create_run_id=False,
            before_start=prepare_run,
        )

    def stop(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            if not thread or not thread.is_alive():
                return {"stopping": False, "running": False, "message": "Worker queue dispatcher is not running"}
            self._stop.set()
            metadata_workers = [worker for worker, _run_id in self._metadata_workers.values()]
            clip_workers = [worker for worker, _run_id in self._clip_workers.values()]
            placeholder_workers = [worker for worker, _run_id in self._placeholder_workers.values()]
            plugin_workers = [
                worker for worker, _service, _plugin_id, _worker_id in self._plugin_workers.values()
            ]
            for worker in metadata_workers:
                worker.stop()
            for worker in clip_workers:
                worker.stop()
            for worker in placeholder_workers:
                worker.stop()
            for worker in plugin_workers:
                worker.stop()
            METADATA_WORKER.stop()
            PLAYLIST_SCAN_WORKER.stop()
            LIVE_HISTORY_WORKER.stop()
            PLACEHOLDER_RECOVERY_WORKER.stop()
        running = thread.is_alive()
        return {
            "stopping": running,
            "running": running,
            "message": "Worker queue dispatcher stop requested",
        }

    def allow_archivarix_retry(self) -> None:
        self._placeholder_block_reason = ""
        self._archivarix_retry_requested.set()

    def allow_proxy_retry(self) -> None:
        self._proxy_retry_requested.set()

    def _set_dispatch_settings_unlocked(
        self,
        mode: str,
        job_delay_seconds: float,
        youtube_max_in_flight: int,
        archivarix_max_in_flight: int,
    ) -> None:
        self._dispatch_mode = "throttle" if mode == "throttle" else "delay"
        self._job_dispatch_delay = max(0.0, float(job_delay_seconds))
        self._youtube_max_in_flight = max(
            1,
            min(100, int(youtube_max_in_flight)),
        )
        self._archivarix_max_in_flight = max(
            1,
            min(20, int(archivarix_max_in_flight)),
        )
        self._dispatch_revision += 1

    def update_dispatch_settings(
        self,
        mode: str,
        job_delay_seconds: float,
        youtube_max_in_flight: int,
        archivarix_max_in_flight: int,
    ) -> dict[str, Any]:
        with self._lock:
            self._set_dispatch_settings_unlocked(
                mode,
                job_delay_seconds,
                youtube_max_in_flight,
                archivarix_max_in_flight,
            )
            return self._dispatch_settings_unlocked()

    def _dispatch_settings_unlocked(self) -> dict[str, Any]:
        effective_delay = (
            0.0 if self._dispatch_mode == "throttle" else self._job_dispatch_delay
        )
        return {
            "dispatch_mode": self._dispatch_mode,
            "job_dispatch_delay_seconds": self._job_dispatch_delay,
            "effective_job_dispatch_delay_seconds": effective_delay,
            "youtube_max_in_flight": self._youtube_max_in_flight,
            "archivarix_max_in_flight": self._archivarix_max_in_flight,
        }

    def dispatch_settings(self) -> dict[str, Any]:
        with self._lock:
            return self._dispatch_settings_unlocked()

    def _mark_initial_count(self, count: int) -> None:
        with self._lock:
            self._initial_count = max(0, int(count or 0))

    def _mark_completed(self, count: int = 1) -> None:
        with self._lock:
            self._completed_count += max(0, int(count or 0))

    def _metadata_run_processed(self, db_path: Path, run_id: str) -> int:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT processed FROM metadata_worker_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return int(row["processed"] or 0) if row else 0
        finally:
            conn.close()

    def _wait_for_worker(self, worker: Any) -> None:
        while worker.is_running():
            if self._stop.wait(0.5):
                worker.stop()
            else:
                continue

    def _next_row(
        self,
        db_path: Path,
        worker_types: tuple[str, ...] = (),
        excluded_queue_ids: set[int] | None = None,
        plugin_process_keys: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        conn = connect(db_path)
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if worker_types:
                placeholders = ", ".join("?" for _ in worker_types)
                clauses.append(f"worker_type IN ({placeholders})")
                params.extend(worker_types)
            if "plugin" in worker_types and plugin_process_keys is not None:
                allowed = sorted(plugin_process_keys)
                if allowed:
                    process_clauses = []
                    for plugin_id, worker_id in allowed:
                        process_clauses.append("(source_key = ? AND task_type = ?)")
                        params.extend((plugin_id, worker_id))
                    clauses.append(
                        "(worker_type <> 'plugin' OR ("
                        + " OR ".join(process_clauses)
                        + "))"
                    )
                else:
                    clauses.append("worker_type <> 'plugin'")
            excluded = sorted(excluded_queue_ids or set())
            if excluded:
                placeholders = ", ".join("?" for _ in excluded)
                clauses.append(f"queue_id NOT IN ({placeholders})")
                params.extend(excluded)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            row = conn.execute(
                f"""
                SELECT *
                FROM worker_queue
                {where}
                ORDER BY {worker_queue_order_sql()}
                LIMIT 1
                """,
                params,
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _drop_unknown_row(self, db_path: Path, row: dict[str, Any]) -> None:
        conn = connect(db_path)
        try:
            with conn:
                remove_worker_queue_entry(conn, int(row.get("queue_id") or 0))
        finally:
            conn.close()

    def _run(
        self,
        db_path: Path,
        cookie_file: Path,
        thumb_dir: Path,
        timezone_name: str,
        archivarix_cookie_file: Path,
        archivarix_thumb_dir: Path,
        archivarix_request_timeout: float = 15.0,
        archivarix_stream_timeout: float = 30.0,
        archivarix_retry_attempts: int = 3,
        archivarix_retry_backoff_seconds: float = 2.0,
        proxy_url: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        plugin_manager = self._plugin_manager
        conn = connect(db_path)
        try:
            self._mark_initial_count(worker_queue_count(conn))
        finally:
            conn.close()
        last_dispatch: float | None = None
        next_dispatch = time.monotonic()
        dispatch_revision = -1
        conn = connect(db_path)
        try:
            archivarix_block = external_service_block(conn, "archivarix")
            proxy_block = external_service_block(conn, "proxy")
        finally:
            conn.close()
        archivarix_blocked = bool(archivarix_block["blocked"])
        proxy_blocked = bool(proxy_block["blocked"])
        youtube_blocked = False
        self._placeholder_block_reason = str(archivarix_block["message"])
        if proxy_blocked:
            conn = connect(db_path)
            try:
                with conn:
                    log_worker_queue_event(
                        conn,
                        "error",
                        (
                            "Worker queue start blocked because the configured proxy "
                            f"is unavailable. {proxy_block['message']}"
                        ),
                    )
            finally:
                conn.close()
        try:
            while not self._stop.is_set():
                if self._archivarix_retry_requested.is_set():
                    self._archivarix_retry_requested.clear()
                    archivarix_blocked = False
                    self._placeholder_block_reason = ""
                if self._proxy_retry_requested.is_set():
                    self._proxy_retry_requested.clear()
                    proxy_blocked = False
                authentication_blocked = False
                detected_proxy_block = ""
                with self._lock:
                    metadata_workers = dict(self._metadata_workers)
                    clip_workers = dict(self._clip_workers)
                placeholder_workers = dict(self._placeholder_workers)
                plugin_workers = dict(self._plugin_workers)

                for queue_id, (worker, run_id) in metadata_workers.items():
                    if worker.is_alive():
                        continue
                    self._mark_completed(self._metadata_run_processed(db_path, run_id))
                    if worker.proxy_block_reason():
                        detected_proxy_block = worker.proxy_block_reason()
                    elif worker.blocked_reason():
                        authentication_blocked = True
                    with self._lock:
                        self._metadata_workers.pop(queue_id, None)

                for queue_id, (worker, _run_id) in clip_workers.items():
                    if worker.is_alive():
                        continue
                    if worker.proxy_block_reason():
                        detected_proxy_block = worker.proxy_block_reason()
                    elif worker.blocked_reason():
                        authentication_blocked = True
                    else:
                        self._mark_completed()
                    with self._lock:
                        self._clip_workers.pop(queue_id, None)

                for queue_id, (worker, _run_id) in placeholder_workers.items():
                    if worker.is_alive():
                        continue
                    proxy_reason = worker.proxy_block_reason()
                    reason = worker.blocked_reason()
                    if proxy_reason:
                        detected_proxy_block = proxy_reason
                    elif reason:
                        self._placeholder_block_reason = reason
                        archivarix_blocked = True
                    else:
                        self._mark_completed()
                    with self._lock:
                        self._placeholder_workers.pop(queue_id, None)

                for queue_id, (
                    worker,
                    _service,
                    _plugin_id,
                    _worker_id,
                ) in plugin_workers.items():
                    if worker.is_alive():
                        continue
                    self._mark_completed()
                    with self._lock:
                        self._plugin_workers.pop(queue_id, None)

                if authentication_blocked:
                    youtube_blocked = True
                    with self._lock:
                        active_metadata_workers = [
                            worker for worker, _run_id in self._metadata_workers.values()
                        ]
                        active_clip_workers = [
                            worker for worker, _run_id in self._clip_workers.values()
                        ]
                    for worker in [*active_metadata_workers, *active_clip_workers]:
                        worker.stop()
                if detected_proxy_block:
                    proxy_blocked = True
                    with self._lock:
                        active_network_workers = [
                            worker for worker, _run_id in self._metadata_workers.values()
                        ] + [
                            worker for worker, _run_id in self._clip_workers.values()
                        ] + [
                            worker for worker, _run_id in self._placeholder_workers.values()
                        ]
                    for worker in active_network_workers:
                        worker.stop()

                now = time.monotonic()
                with self._lock:
                    metadata_queue_ids = set(self._metadata_workers)
                    clip_queue_ids = set(self._clip_workers)
                    placeholder_queue_ids = set(self._placeholder_workers)
                    plugin_queue_ids = set(self._plugin_workers)
                    active_plugin_workers = list(self._plugin_workers.values())
                    current_mode = self._dispatch_mode
                    current_job_delay = self._job_dispatch_delay
                    youtube_max_in_flight = self._youtube_max_in_flight
                    archivarix_max_in_flight = self._archivarix_max_in_flight
                    current_dispatch_revision = self._dispatch_revision
                effective_job_delay = (
                    0.0 if current_mode == "throttle" else current_job_delay
                )
                if current_dispatch_revision != dispatch_revision:
                    next_dispatch = (
                        last_dispatch + effective_job_delay
                        if last_dispatch is not None
                        else now
                    )
                    dispatch_revision = current_dispatch_revision

                active_plugin_process_counts = Counter(
                    (plugin_id, worker_id)
                    for _worker, _service, plugin_id, worker_id in active_plugin_workers
                )
                plugin_youtube_in_flight = sum(
                    service == "youtube"
                    for _worker, service, _plugin_id, _worker_id in active_plugin_workers
                )
                plugin_archivarix_in_flight = sum(
                    service == "archivarix"
                    for _worker, service, _plugin_id, _worker_id in active_plugin_workers
                )
                process_definitions = (
                    plugin_manager.process_definitions() if plugin_manager is not None else {}
                )
                eligible_plugin_processes: set[tuple[str, str]] = set()
                for process_key, process in process_definitions.items():
                    if active_plugin_process_counts[process_key] >= int(
                        process["maxInFlight"]
                    ):
                        continue
                    service = str(process["service"])
                    if service == "youtube" and (
                        proxy_blocked
                        or youtube_blocked
                        or len(metadata_queue_ids) + len(clip_queue_ids) + plugin_youtube_in_flight
                        >= youtube_max_in_flight
                    ):
                        continue
                    if service == "archivarix" and (
                        proxy_blocked
                        or archivarix_blocked
                        or len(placeholder_queue_ids) + plugin_archivarix_in_flight
                        >= archivarix_max_in_flight
                    ):
                        continue
                    eligible_plugin_processes.add(process_key)

                eligible_worker_types: list[str] = []
                if (
                    not proxy_blocked
                    and not youtube_blocked
                    and len(metadata_queue_ids) + len(clip_queue_ids) + plugin_youtube_in_flight
                    < youtube_max_in_flight
                ):
                    eligible_worker_types.extend(("metadata", "clip"))
                if (
                    not proxy_blocked
                    and not archivarix_blocked
                    and len(placeholder_queue_ids) + plugin_archivarix_in_flight
                    < archivarix_max_in_flight
                ):
                    eligible_worker_types.append("placeholder")
                if eligible_plugin_processes:
                    eligible_worker_types.append("plugin")
                has_active = bool(
                    metadata_queue_ids or clip_queue_ids or placeholder_queue_ids or plugin_queue_ids
                )
                if not proxy_blocked and not has_active and not youtube_blocked:
                    eligible_worker_types.extend(("account", "playlist", "history"))
                if not eligible_worker_types:
                    if has_active:
                        self._stop.wait(0.05)
                        continue
                    return
                row = self._next_row(
                    db_path,
                    tuple(eligible_worker_types),
                    metadata_queue_ids | clip_queue_ids | placeholder_queue_ids | plugin_queue_ids,
                    eligible_plugin_processes,
                )
                if not row:
                    if has_active:
                        self._stop.wait(0.05)
                        continue
                    return
                if now < next_dispatch:
                    self._stop.wait(
                        max(0.01, min(0.1, next_dispatch - time.monotonic()))
                    )
                    continue
                worker_type = row.get("worker_type") or ""
                queue_id = int(row.get("queue_id") or 0)
                launched = False
                launched_at: float | None = None
                if worker_type == "metadata":
                    worker = MetadataWorker()
                    result = worker.start(
                        db_path,
                        cookie_file,
                        thumb_dir,
                        delay=0.0,
                        limit=1,
                        force=False,
                        stale_days=30,
                        record_summary=False,
                        queue_id=queue_id,
                        proxy_url=proxy_url,
                        timezone_name=timezone_name,
                    )
                    if result.get("started"):
                        with self._lock:
                            self._metadata_workers[queue_id] = (
                                worker,
                                str(result.get("run_id") or ""),
                            )
                        launched = True
                        launched_at = time.monotonic()
                elif worker_type == "clip":
                    worker = ClipWorker()
                    result = worker.start(
                        db_path,
                        cookie_file,
                        row,
                        proxy_url=proxy_url,
                    )
                    if result.get("started"):
                        with self._lock:
                            self._clip_workers[queue_id] = (
                                worker,
                                str(result.get("run_id") or ""),
                            )
                        launched = True
                        launched_at = time.monotonic()
                elif worker_type == "placeholder":
                    worker = PlaceholderRecoveryWorker()
                    result = worker.start(
                        db_path,
                        archivarix_cookie_file,
                        archivarix_thumb_dir,
                        queue_id=queue_id,
                        request_timeout=archivarix_request_timeout,
                        stream_timeout=archivarix_stream_timeout,
                        retry_attempts=archivarix_retry_attempts,
                        retry_backoff_seconds=archivarix_retry_backoff_seconds,
                        proxy_url=proxy_url,
                    )
                    if result.get("blocked"):
                        reason = str(result.get("message") or "unavailable")
                        conn = connect(db_path)
                        try:
                            with conn:
                                if reason != self._placeholder_block_reason:
                                    log_worker_event(
                                        conn,
                                        "",
                                        "placeholder warn",
                                        f"Automatic recovery skipped: {reason}",
                                        row.get("video_id") or "",
                                    )
                        finally:
                            conn.close()
                        self._placeholder_block_reason = reason
                        archivarix_blocked = True
                    elif result.get("started"):
                        self._placeholder_block_reason = ""
                        with self._lock:
                            self._placeholder_workers[queue_id] = (
                                worker,
                                str(result.get("run_id") or ""),
                            )
                        launched = True
                        launched_at = time.monotonic()
                elif worker_type == "playlist":
                    result = PLAYLIST_SCAN_WORKER.start(
                        db_path,
                        cookie_file,
                        delay=0.0,
                        limit=1,
                        force=False,
                        stale_days=7,
                        record_summary=False,
                        proxy_url=proxy_url,
                    )
                    launched = bool(result.get("started"))
                    if launched:
                        launched_at = time.monotonic()
                    if not result.get("started") and not PLAYLIST_SCAN_WORKER.is_running():
                        time.sleep(0.5)
                    self._wait_for_worker(PLAYLIST_SCAN_WORKER)
                    if PLAYLIST_SCAN_WORKER.proxy_block_reason():
                        proxy_blocked = True
                    elif not self._stop.is_set():
                        self._mark_completed()
                elif worker_type == "history":
                    mode = "verify" if row.get("task_type") == "verify" else "recent"
                    result = LIVE_HISTORY_WORKER.start(
                        db_path,
                        cookie_file,
                        mode=mode,
                        timezone_name=timezone_name,
                        proxy_url=proxy_url,
                    )
                    launched = bool(result.get("started"))
                    if launched:
                        launched_at = time.monotonic()
                    if not result.get("started") and not LIVE_HISTORY_WORKER.is_running():
                        time.sleep(0.5)
                    self._wait_for_worker(LIVE_HISTORY_WORKER)
                    if LIVE_HISTORY_WORKER.proxy_block_reason():
                        proxy_blocked = True
                    elif not self._stop.is_set():
                        self._mark_completed()
                elif worker_type == "plugin":
                    plugin_id = str(row.get("source_key") or "")
                    worker_id = str(row.get("task_type") or "")
                    process = (
                        plugin_manager.process_definition(plugin_id, worker_id)
                        if plugin_manager is not None
                        else None
                    )
                    if process is None or plugin_manager is None:
                        self._drop_unknown_row(db_path, row)
                        self._mark_completed()
                    else:
                        worker = PluginTaskWorker()
                        result = worker.start(db_path, plugin_manager, row)
                        if result.get("started"):
                            with self._lock:
                                self._plugin_workers[queue_id] = (
                                    worker,
                                    str(process["service"]),
                                    plugin_id,
                                    worker_id,
                                )
                            launched = True
                            launched_at = time.monotonic()
                elif worker_type == "account":
                    run_optional_account_sync(
                        db_path,
                        config or {},
                        timezone_name,
                        queue_id,
                    )
                    launched = True
                    launched_at = time.monotonic()
                    if not self._stop.is_set():
                        self._mark_completed()
                else:
                    self._drop_unknown_row(db_path, row)
                    self._mark_completed()
                if launched:
                    last_dispatch = launched_at or time.monotonic()
                    next_dispatch = last_dispatch + effective_job_delay
        finally:
            with self._lock:
                metadata_workers = [worker for worker, _run_id in self._metadata_workers.values()]
                clip_workers = [worker for worker, _run_id in self._clip_workers.values()]
                placeholder_workers = [worker for worker, _run_id in self._placeholder_workers.values()]
                plugin_workers = [
                    worker
                    for worker, _service, _plugin_id, _worker_id in self._plugin_workers.values()
                ]
            for worker in metadata_workers:
                worker.stop()
            for worker in clip_workers:
                worker.stop()
            for worker in placeholder_workers:
                worker.stop()
            for worker in plugin_workers:
                worker.stop()
            while any(
                worker.is_alive()
                for worker in [*metadata_workers, *clip_workers, *placeholder_workers, *plugin_workers]
            ):
                time.sleep(0.05)
            with self._lock:
                self._metadata_workers.clear()
                self._clip_workers.clear()
                self._placeholder_workers.clear()
                self._plugin_workers.clear()


WORKER_QUEUE_DISPATCHER = WorkerQueueDispatcher()
