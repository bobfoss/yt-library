"""Background worker orchestration for library enrichment jobs."""

from __future__ import annotations

import argparse
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .core import *

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
        record_summary: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "run_id": self._run_id, "message": "Worker already running"}
            self._stop.clear()
            self._run_id = uuid.uuid4().hex
            self._thread = threading.Thread(
                target=self._run,
                args=(
                    self._run_id,
                    db_path,
                    cookie_file,
                    thumb_dir,
                    delay,
                    limit,
                    force,
                    stale_days,
                    record_summary,
                ),
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
        record_summary: bool,
    ) -> None:
        conn = connect(db_path)
        opener = load_cookie_opener(cookie_file)
        try:
            initial_total = worker_queue_type_count(conn, "metadata")
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
                rows = metadata_queue_rows(conn, force=force, stale_days=stale_days)
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
                queue_id = int(row["queue_id"]) if "queue_id" in row.keys() else 0
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
                        )
                        if not (
                            metadata.get("channel")
                            or metadata.get("channel_url")
                            or metadata.get("channel_thumbnail_path")
                        ):
                            status = "no_metadata"
                    else:
                        metadata = fetch_watch_metadata(opener, video_id, thumb_dir)
                        if not useful_video_metadata(metadata):
                            status = "no_metadata"
                            try:
                                archivarix_opener = load_cookie_opener(ARCHIVARIX_COOKIE_FILE)
                                video, thumbnail_url, thumbnail_path, arch_status, arch_error = recover_archivarix_video(
                                    video_id,
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
                                        channel_id or video_id,
                                        str(video.get("channelThumbnailUrl") or ""),
                                        thumb_dir,
                                    )
                                metadata = metadata_from_archivarix_video(video_id, video, thumbnail_url, thumbnail_path)
                                status = "ok" if useful_video_metadata(metadata) else "no_metadata"
                                save_video_recovery(
                                    conn,
                                    video_id,
                                    video,
                                    arch_status,
                                    arch_error,
                                    thumbnail_url,
                                    thumbnail_path,
                                )
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                    status = "error"
                    error = str(exc)
                now = utc_now()
                with conn:
                    if metadata_source == "channel":
                        store_channel_metadata(conn, metadata, status, error, updated_at=now)
                    else:
                        store_video_metadata(conn, metadata, status, error, updated_at=now)
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
                    if queue_id:
                        conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (queue_id,))
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
        record_summary: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "run_id": self._run_id, "message": "Playlist scan already running"}
            self._stop.clear()
            self._run_id = uuid.uuid4().hex
            self._thread = threading.Thread(
                target=self._run,
                args=(self._run_id, db_path, cookie_file, delay, limit, force, stale_days, record_summary),
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
                try:
                    playlist_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
                    header_page = request_text(opener, playlist_url)
                    header_page_requires_login = youtube_page_requires_login(header_page)
                    header_metadata = extract_playlist_metadata(header_page, playlist_id)
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
                    header_metadata = {}
                header_count_available = bool(header_metadata.get("has_video_count"))
                if not header_count_available and header_page_requires_login:
                    status = "error"
                    error = "skipping: YouTube login session is not accepted by YouTube"
                elif not header_count_available:
                    status = "error"
                    error = "skipping: YouTube playlist header count unavailable"
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
                header_expected_count = int(header_metadata.get("video_count") or 0)
                expected_count = header_expected_count
                exact_count_required = playlist_scan_requires_exact_count(
                    header_metadata,
                    known_owner_channel_id=row["owner_channel_id"] if "owner_channel_id" in row.keys() else "",
                    known_visibility=row["visibility"] if "visibility" in row.keys() else "",
                )
                previous_scan_count = int(row["video_count"] or 0)
                if status == "ok" and (ytdlp_error or playlist_scan_is_incomplete(ytdlp_count, expected_count)):
                    session_valid, _session_message = youtube_session_status(cookie_file, verify_remote=True)
                    if not session_valid:
                        status = "error"
                        error = "skipping: YouTube login session expired"
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
                    if status == "error":
                        video_count, unavailable_count = save_playlist_scan_error(conn, playlist_id, error)
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
                        placeholder_result = enqueue_placeholder_recovery_targets(
                            conn,
                            playlist_id,
                        )
                        placeholder_queued = int(placeholder_result["inserted"])
                    processed += 1
                    if status == "error":
                        failed += 1
                        log_playlist_scan_event(conn, run_id, "error", f"{title}: {error}", playlist_id)
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
                timezone_name = get_setting(conn, "display_timezone", DEFAULT_DISPLAY_TIMEZONE)
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
            with conn:
                conn.execute(
                    """
                    UPDATE live_history_worker_runs
                    SET status = 'error', finished_at = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (utc_now(), str(exc), run_id),
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

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(
        self,
        db_path: Path,
        archivarix_cookie_file: Path,
        thumb_dir: Path,
    ) -> dict[str, Any]:
        session_valid, session_message = archivarix_session_status(archivarix_cookie_file)
        if not session_valid:
            return {
                "started": False,
                "blocked": True,
                "message": session_message,
            }
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "message": "Placeholder recovery already running"}
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(db_path, archivarix_cookie_file, thumb_dir),
                daemon=True,
            )
            self._thread.start()
            return {"started": True, "message": "Placeholder recovery started"}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return {"stopping": False, "message": "Placeholder recovery is not running"}
            self._stop.set()
            return {"stopping": True, "message": "Placeholder recovery stop requested"}

    def _run(
        self,
        db_path: Path,
        archivarix_cookie_file: Path,
        thumb_dir: Path,
    ) -> None:
        conn = connect(db_path)
        archivarix_opener = load_cookie_opener(archivarix_cookie_file)
        try:
            rows = placeholder_worker_queue_rows(conn, limit=1)
            if not rows or self._stop.is_set():
                return
            row = rows[0]
            queue_id = int(row["queue_id"] or 0)
            playlist_id = row["playlist_id"] or ""
            video_id = row["video_id"]
            title = row["current_title"] or video_id
            status = "not_found"
            error = ""
            try:
                video, thumbnail_url, thumbnail_path, status, error = recover_archivarix_video(
                    video_id,
                    thumb_dir,
                    archivarix_opener,
                    refresh_metadata=True,
                    no_api=False,
                    delay=3.0,
                    channel_cache={},
                    stop_event=self._stop,
                    request_timeout=5,
                    stream_timeout=5,
                    thumbnail_timeout=5,
                    channel_thumbnail_timeout=5,
                )
                if self._stop.is_set():
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
                if status == "found":
                    level = "placeholder found"
                    message = f"found: {title}"
                elif status == "thumbnail_only":
                    level = "placeholder thumbnail"
                    message = f"thumbnail only: {title}"
                elif status == "not_found":
                    level = "placeholder not found"
                    message = "not found"
                else:
                    level = "placeholder error"
                    message = error or status
                if queue_id:
                    conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (queue_id,))
                rebuild_playlist_reconciliation(conn, playlist_id)
                log_worker_event(conn, "", level, message, video_id)
        except Exception as exc:
            with conn:
                log_worker_event(conn, "", "placeholder error", f"Worker crashed: {exc}")
        finally:
            conn.close()


PLACEHOLDER_RECOVERY_WORKER = PlaceholderRecoveryWorker()


class WorkerQueueDispatcher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._placeholder_block_reason = ""

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(self, db_path: Path, cookie_file: Path, thumb_dir: Path) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "message": "Worker queue dispatcher already running"}
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(db_path, cookie_file, thumb_dir),
                daemon=True,
            )
            self._thread.start()
            return {"started": True, "message": "Worker queue dispatcher started"}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            if not thread or not thread.is_alive():
                return {"stopping": False, "running": False, "message": "Worker queue dispatcher is not running"}
            self._stop.set()
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

    def _wait_for_worker(self, worker: Any) -> None:
        while worker.is_running():
            if self._stop.wait(0.5):
                worker.stop()
            else:
                continue

    def _next_row(self, db_path: Path) -> dict[str, Any] | None:
        conn = connect(db_path)
        try:
            row = conn.execute(
                """
                SELECT *
                FROM worker_queue
                ORDER BY priority, queue_id
                LIMIT 1
                """
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

    def _run(self, db_path: Path, cookie_file: Path, thumb_dir: Path) -> None:
        while not self._stop.is_set():
            row = self._next_row(db_path)
            if not row:
                return
            worker_type = row.get("worker_type") or ""
            if worker_type == "metadata":
                result = METADATA_WORKER.start(
                    db_path,
                    cookie_file,
                    thumb_dir,
                    delay=1.0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )
                if not result.get("started") and not METADATA_WORKER.is_running():
                    time.sleep(0.5)
                self._wait_for_worker(METADATA_WORKER)
            elif worker_type == "playlist":
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
            elif worker_type == "placeholder":
                result = PLACEHOLDER_RECOVERY_WORKER.start(
                    db_path,
                    ARCHIVARIX_COOKIE_FILE,
                    DEFAULT_ARCHIVARIX_THUMB_DIR,
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
                            remove_worker_queue_entry(conn, int(row.get("queue_id") or 0))
                    finally:
                        conn.close()
                    self._placeholder_block_reason = reason
                    continue
                self._placeholder_block_reason = ""
                if not result.get("started") and not PLACEHOLDER_RECOVERY_WORKER.is_running():
                    time.sleep(0.5)
                self._wait_for_worker(PLACEHOLDER_RECOVERY_WORKER)
            elif worker_type == "history":
                mode = "verify" if row.get("task_type") == "verify" else "recent"
                result = LIVE_HISTORY_WORKER.start(db_path, cookie_file, mode=mode)
                if not result.get("started") and not LIVE_HISTORY_WORKER.is_running():
                    time.sleep(0.5)
                self._wait_for_worker(LIVE_HISTORY_WORKER)
            else:
                self._drop_unknown_row(db_path, row)


WORKER_QUEUE_DISPATCHER = WorkerQueueDispatcher()
