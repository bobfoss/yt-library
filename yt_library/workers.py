"""Background worker orchestration for library enrichment jobs."""

from __future__ import annotations

import argparse
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import (
    config_path,
    configured_archivarix_max_in_flight,
    configured_archivarix_request_interval,
    configured_youtube_max_in_flight,
    configured_youtube_request_interval,
    effective_display_timezone,
)
from .core import *


def youtube_authentication_debug_message(
    exc: YouTubeAuthenticationError,
    cookie_file: Path,
) -> str:
    parts = [exc.diagnostics, youtube_cookie_diagnostics(cookie_file)]
    return "YouTube authentication diagnostics: " + " | ".join(part for part in parts if part)


class _ThreadWorkerLifecycle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_id = ""
        self._blocked_reason = ""

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
    ) -> None:
        conn = connect(db_path)
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
                    force=force,
                    stale_days=stale_days,
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
                    session_valid, session_message = youtube_session_status(cookie_file, verify_remote=False)
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
                opener = load_cookie_opener(cookie_file)
                row_queue_id = int(row["queue_id"]) if "queue_id" in row.keys() else 0
                video_id = row["video_id"]
                metadata_source = row["metadata_source"] if "metadata_source" in row.keys() else "history"
                queued_channel_id = row["channel_id"] if "channel_id" in row.keys() else ""
                queued_channel_title = row["channel_title"] if "channel_title" in row.keys() else ""
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
                    "watch_progress_percent": "0",
                    "watch_resume_seconds": "0",
                    "yt_status": "",
                }
                try:
                    if metadata_source == "channel" and queued_channel_id:
                        metadata = fetch_channel_metadata(
                            opener,
                            queued_channel_id,
                            thumb_dir,
                            fallback_query=queued_channel_title,
                            require_authenticated=cookie_file.exists(),
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
                            youtube_authentication_debug_message(exc, cookie_file),
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
                if metadata_source != "channel" and status == "ok" and not self._stop.is_set():
                    try:
                        channel_metadata, channel_status, channel_error = fetch_new_channel_metadata_if_needed(
                            conn,
                            opener,
                            thumb_dir,
                            metadata,
                            require_authenticated=cookie_file.exists(),
                        )
                    except YouTubeAuthenticationError as exc:
                        authentication_error = f"Metadata worker stopped: {exc}"
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
                                youtube_authentication_debug_message(exc, cookie_file),
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
                        if status == "no_metadata":
                            enqueue_placeholder_recovery_item(
                                conn,
                                video_id=video_id,
                                current_title=row["current_title"] or video_id,
                                source_key=row["source_key"] or "",
                                playlist_count=int(row["playlist_count"] or 0),
                                priority=int(row["priority"] or 0),
                                updated_at=now,
                            )
                    processed += 1
                    channel_label = metadata.get("channel") or queued_channel_title or queued_channel_id or video_id
                    if status == "error":
                        failed += 1
                        subject_id = channel_label if metadata_source == "channel" else video_id
                        log_worker_event(conn, run_id, f"{metadata_source} error", error, subject_id)
                    else:
                        found += 1
                        title = metadata.get("title") or video_id
                        if metadata_source == "channel":
                            log_worker_event(conn, run_id, metadata_source, f"{status}: {channel_label} (via {title})", channel_label)
                        else:
                            log_worker_event(conn, run_id, metadata_source, f"{status}: {title}", video_id)
                            if channel_status:
                                discovered_channel_label = (
                                    channel_metadata.get("channel")
                                    or metadata.get("channel")
                                    or video_metadata_channel_id(metadata)
                                )
                                log_worker_event(
                                    conn,
                                    run_id,
                                    "channel",
                                    f"{channel_status}: {discovered_channel_label} (discovered via {title})",
                                    discovered_channel_label,
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
    ) -> dict[str, Any]:
        return self._start_background(
            self._run,
            lambda run_id: (run_id, db_path, cookie_file, delay, limit, force, stale_days, record_summary),
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
    ) -> None:
        conn = connect(db_path)
        opener = load_cookie_opener(cookie_file)
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
                header_page_requires_login = False
                youtube_debug = ""
                try:
                    playlist_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
                    header_page = request_text(opener, playlist_url)
                    header_page_requires_login = youtube_page_requires_login(header_page)
                    header_metadata = extract_playlist_metadata(header_page, playlist_id)
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                    header_metadata = {}
                    youtube_debug = youtube_request_error_diagnostics(exc, "playlist header")
                header_count_available = bool(header_metadata.get("has_video_count"))
                if not header_count_available and header_page_requires_login:
                    status = "error"
                    error = "skipping: YouTube login session is not accepted by YouTube"
                    youtube_debug = (
                        youtube_page_diagnostics(header_page, "playlist header")
                        + " | "
                        + youtube_cookie_diagnostics(cookie_file)
                    )
                elif not header_count_available:
                    status = "error"
                    error = "skipping: YouTube playlist header count unavailable"
                    if youtube_debug:
                        error += "; request diagnostics logged at debug level"
                else:
                    try:
                        videos, playlist_metadata = scan_playlist_ytdlp(playlist_id, cookie_file)
                    except Exception as exc:
                        ytdlp_error = str(exc)
                ytdlp_count = len(videos)
                if header_metadata.get("video_count"):
                    playlist_metadata["video_count"] = header_metadata["video_count"]
                for key in (
                    "title",
                    "description",
                    "owner",
                    "owner_channel_id",
                    "owner_thumbnail_url",
                    "thumbnail_url",
                    "url",
                ):
                    if header_metadata.get(key):
                        playlist_metadata[key] = header_metadata[key]
                if header_metadata.get("visibility"):
                    playlist_metadata["visibility"] = header_metadata["visibility"]
                    playlist_metadata["owner_channel_id"] = ""
                    playlist_metadata["owner_thumbnail_url"] = ""
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
                thumbnail_url = str(playlist_metadata.get("thumbnail_url") or "").strip()
                if thumbnail_url:
                    playlist_metadata["thumbnail_path"] = cache_thumbnail(
                        opener,
                        playlist_id,
                        thumbnail_url,
                        DEFAULT_THUMB_DIR,
                    )
                header_expected_count = int(header_metadata.get("video_count") or 0)
                expected_count = header_expected_count
                exact_count_required = playlist_scan_requires_exact_count(
                    header_metadata,
                    known_owner_channel_id=row["owner_channel_id"] if "owner_channel_id" in row.keys() else "",
                    known_visibility=row["visibility"] if "visibility" in row.keys() else "",
                )
                previous_scan_count = int(row["video_count"] or 0)
                if status == "ok" and (ytdlp_error or playlist_scan_is_incomplete(ytdlp_count, expected_count)):
                    session_valid, session_message = youtube_session_status(cookie_file, verify_remote=True)
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
                    expected_source = "YouTube playlist header" if header_expected_count else "playlist metadata"
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
                    if status == "error" and playlist_id == LIKED_VIDEOS_PLAYLIST_ID:
                        video_count = int(
                            conn.execute("SELECT COUNT(*) FROM videos WHERE reaction = 'L'").fetchone()[0]
                            or 0
                        )
                        unavailable_count = 0
                    elif status == "error":
                        video_count, unavailable_count = save_playlist_scan_error(conn, playlist_id, error)
                    elif playlist_id == LIKED_VIDEOS_PLAYLIST_ID:
                        video_count, unavailable_count = save_liked_video_reactions(
                            conn,
                            videos,
                            replace=not expected_count or len(videos) >= expected_count,
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
                    else:
                        found += 1
                        reported_note = ""
                        if (
                            expected_count
                            and not exact_count_required
                            and video_count < expected_count
                        ):
                            reported_note = f"; {video_count} exposed of {expected_count} reported"
                        log_playlist_scan_event(
                            conn,
                            run_id,
                            "info",
                            (
                                f"{title}: {video_count} videos, {unavailable_count} unavailable"
                                + reported_note
                                + (f"; queued {metadata_queued} metadata items" if metadata_queued else "")
                                + (f"; queued {placeholder_queued} placeholder recoveries" if placeholder_queued else "")
                            ),
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
                log_playlist_scan_event(conn, run_id, "error", f"Playlist scan crashed: {exc}")
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
    ) -> dict[str, Any]:
        label = "Verify history" if mode == "verify" else "History fetch"
        return self._start_background(
            self._run,
            lambda run_id: (run_id, db_path, cookie_file, mode, timezone_name),
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
                        utc_now(),
                        HISTORY_BATCH_DELAY_SECONDS,
                        batch_size,
                        f"{label} started",
                    ),
                )
                log_live_history_event(conn, run_id, "info", f"{label} started with {batch_size} per batch")
                conn.execute(
                    """
                    DELETE FROM worker_queue
                    WHERE worker_type = 'history' AND task_type = ?
                    """,
                    (mode,),
                )

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
            processed = 0
            inserted_total = 0
            skipped_total = 0
            last_video_id = ""
            final_message = ""
            while not self._stop.is_set():
                end = start + batch_size - 1
                rows = fetch_youtube_history_web(
                    cookie_file,
                    limit=batch_size,
                    start=start,
                    timezone_name=timezone_name,
                )
                fetched_ids = [row.get("video_id") or "" for row in rows if row.get("video_id")]
                with conn:
                    existing_ids = youtube_occurrence_sequence(conn, start, len(rows))
                    overlap_offset = find_feed_overlap(fetched_ids, existing_ids) if mode == "recent" else None
                    inserted, existing, batch_last_video_id = save_youtube_history_events(conn, rows, start)
                    reconcile_stats = rebuild_history_reconciliation(conn, timezone_name)
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
                log_live_history_event(conn, run_id, "info" if status == "complete" else "warn", final_message, last_video_id)
        except Exception as exc:
            if isinstance(exc, YouTubeAuthenticationError):
                error_message = str(exc)
                debug_message = youtube_authentication_debug_message(exc, cookie_file)
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
    ) -> dict[str, Any]:
        return self._start_background(
            self._run,
            lambda run_id: (run_id, db_path, archivarix_cookie_file, thumb_dir, queue_id),
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
                log_placeholder_recovery_event(conn, run_id, "info", "Placeholder recovery started")
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
            archivarix_opener = load_cookie_opener(archivarix_cookie_file)
            status = "not_found"
            error = ""
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
                    request_timeout=5,
                    stream_timeout=5,
                    thumbnail_timeout=5,
                    channel_thumbnail_timeout=5,
                )
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
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                status = "error"
                error = str(exc)

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
                if status == "found":
                    level = "found"
                    message = f"found: {title}"
                elif status == "thumbnail_only":
                    level = "thumbnail"
                    message = f"thumbnail only: {title}"
                elif status == "not_found":
                    level = "not found"
                    message = "not found"
                else:
                    level = "error"
                    message = error or status
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


class WorkerQueueDispatcher(_ThreadWorkerLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self._placeholder_block_reason = ""
        self._started_at = ""
        self._started_monotonic = 0.0
        self._initial_count = 0
        self._completed_count = 0
        self._metadata_workers: dict[int, tuple[MetadataWorker, str]] = {}
        self._placeholder_workers: dict[int, tuple[PlaceholderRecoveryWorker, str]] = {}
        self._archivarix_retry_requested = threading.Event()

    def stats(self, remaining_count: int) -> dict[str, Any]:
        with self._lock:
            active = bool(self._thread and self._thread.is_alive())
            elapsed = max(0.0, time.monotonic() - self._started_monotonic) if active and self._started_monotonic else 0.0
            completed = self._completed_count
            initial = self._initial_count
            started_at = self._started_at if active else ""
            youtube_in_flight = len(self._metadata_workers)
            archivarix_in_flight = len(self._placeholder_workers)
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
        }

    def start(
        self,
        db_path: Path,
        cookie_file: Path,
        thumb_dir: Path,
        config_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = config_data or {}
        def prepare_run() -> None:
            self._started_at = utc_now()
            self._started_monotonic = time.monotonic()
            self._initial_count = 0
            self._completed_count = 0
            self._metadata_workers = {}
            self._placeholder_workers = {}
            self._archivarix_retry_requested.clear()

        return self._start_background(
            self._run,
            lambda _run_id: (
                db_path,
                cookie_file,
                thumb_dir,
                effective_display_timezone(config),
                config_path(config, "archivarix_cookies"),
                config_path(config, "archivarix_thumbnail_dir"),
                configured_youtube_request_interval(config),
                configured_youtube_max_in_flight(config),
                configured_archivarix_request_interval(config),
                configured_archivarix_max_in_flight(config),
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
            placeholder_workers = [worker for worker, _run_id in self._placeholder_workers.values()]
            for worker in metadata_workers:
                worker.stop()
            for worker in placeholder_workers:
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
    ) -> dict[str, Any] | None:
        conn = connect(db_path)
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if worker_types:
                placeholders = ", ".join("?" for _ in worker_types)
                clauses.append(f"worker_type IN ({placeholders})")
                params.extend(worker_types)
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
                ORDER BY priority, queue_id
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
        youtube_interval: float,
        youtube_max_in_flight: int,
        archivarix_interval: float,
        archivarix_max_in_flight: int,
    ) -> None:
        conn = connect(db_path)
        try:
            self._mark_initial_count(worker_queue_count(conn))
        finally:
            conn.close()
        next_youtube_launch = time.monotonic()
        next_archivarix_launch = time.monotonic()
        conn = connect(db_path)
        try:
            block = external_service_block(conn, "archivarix")
        finally:
            conn.close()
        archivarix_blocked = bool(block["blocked"])
        youtube_blocked = False
        self._placeholder_block_reason = str(block["message"])
        try:
            while not self._stop.is_set():
                if self._archivarix_retry_requested.is_set():
                    self._archivarix_retry_requested.clear()
                    archivarix_blocked = False
                    self._placeholder_block_reason = ""
                authentication_blocked = False
                with self._lock:
                    metadata_workers = dict(self._metadata_workers)
                placeholder_workers = dict(self._placeholder_workers)

                for queue_id, (worker, run_id) in metadata_workers.items():
                    if worker.is_alive():
                        continue
                    self._mark_completed(self._metadata_run_processed(db_path, run_id))
                    if worker.blocked_reason():
                        authentication_blocked = True
                    with self._lock:
                        self._metadata_workers.pop(queue_id, None)

                for queue_id, (worker, _run_id) in placeholder_workers.items():
                    if worker.is_alive():
                        continue
                    reason = worker.blocked_reason()
                    if reason:
                        self._placeholder_block_reason = reason
                        archivarix_blocked = True
                    else:
                        self._mark_completed()
                    with self._lock:
                        self._placeholder_workers.pop(queue_id, None)

                if authentication_blocked:
                    youtube_blocked = True
                    with self._lock:
                        active_metadata_workers = [
                            worker for worker, _run_id in self._metadata_workers.values()
                        ]
                    for worker in active_metadata_workers:
                        worker.stop()

                now = time.monotonic()
                with self._lock:
                    metadata_queue_ids = set(self._metadata_workers)
                    placeholder_queue_ids = set(self._placeholder_workers)

                if (
                    not youtube_blocked
                    and len(metadata_queue_ids) < youtube_max_in_flight
                    and now >= next_youtube_launch
                ):
                    row = self._next_row(db_path, ("metadata",), metadata_queue_ids)
                    if row:
                        queue_id = int(row.get("queue_id") or 0)
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
                        )
                        if result.get("started"):
                            with self._lock:
                                self._metadata_workers[queue_id] = (worker, str(result.get("run_id") or ""))
                            next_youtube_launch = now + youtube_interval

                if (
                    not archivarix_blocked
                    and len(placeholder_queue_ids) < archivarix_max_in_flight
                    and now >= next_archivarix_launch
                ):
                    row = self._next_row(db_path, ("placeholder",), placeholder_queue_ids)
                    if row:
                        queue_id = int(row.get("queue_id") or 0)
                        worker = PlaceholderRecoveryWorker()
                        result = worker.start(
                            db_path,
                            archivarix_cookie_file,
                            archivarix_thumb_dir,
                            queue_id=queue_id,
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
                            next_archivarix_launch = now + archivarix_interval

                with self._lock:
                    has_active = bool(self._metadata_workers or self._placeholder_workers)
                if has_active:
                    self._stop.wait(0.05)
                    continue

                eligible_worker_types: list[str] = []
                if not youtube_blocked:
                    eligible_worker_types.extend(("metadata", "playlist", "history"))
                if not archivarix_blocked:
                    eligible_worker_types.append("placeholder")
                if not eligible_worker_types:
                    return
                row = self._next_row(db_path, tuple(eligible_worker_types))
                if not row:
                    return
                worker_type = row.get("worker_type") or ""
                if worker_type in {"metadata", "placeholder"}:
                    wait_until = next_youtube_launch if worker_type == "metadata" else next_archivarix_launch
                    self._stop.wait(max(0.01, min(0.1, wait_until - time.monotonic())))
                    continue
                if worker_type == "playlist":
                    result = PLAYLIST_SCAN_WORKER.start(
                        db_path,
                        cookie_file,
                        delay=0.0,
                        limit=1,
                        force=False,
                        stale_days=7,
                        record_summary=False,
                    )
                    if not result.get("started") and not PLAYLIST_SCAN_WORKER.is_running():
                        time.sleep(0.5)
                    self._wait_for_worker(PLAYLIST_SCAN_WORKER)
                    if not self._stop.is_set():
                        self._mark_completed()
                elif worker_type == "history":
                    mode = "verify" if row.get("task_type") == "verify" else "recent"
                    result = LIVE_HISTORY_WORKER.start(db_path, cookie_file, mode=mode, timezone_name=timezone_name)
                    if not result.get("started") and not LIVE_HISTORY_WORKER.is_running():
                        time.sleep(0.5)
                    self._wait_for_worker(LIVE_HISTORY_WORKER)
                    if not self._stop.is_set():
                        self._mark_completed()
                else:
                    self._drop_unknown_row(db_path, row)
                    self._mark_completed()
        finally:
            with self._lock:
                metadata_workers = [worker for worker, _run_id in self._metadata_workers.values()]
                placeholder_workers = [worker for worker, _run_id in self._placeholder_workers.values()]
            for worker in metadata_workers:
                worker.stop()
            for worker in placeholder_workers:
                worker.stop()
            while any(worker.is_alive() for worker in [*metadata_workers, *placeholder_workers]):
                time.sleep(0.05)
            with self._lock:
                self._metadata_workers.clear()
                self._placeholder_workers.clear()


WORKER_QUEUE_DISPATCHER = WorkerQueueDispatcher()
