"""Background worker orchestration for library enrichment jobs."""

from __future__ import annotations

import argparse
import sqlite3
import threading
import time
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
                log_worker_event(conn, run_id, "info", f"Queued {len(rows)} metadata items")

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
                    if metadata_source != "channel":
                        conn.execute(
                            """
                            INSERT INTO video_metadata(
                              video_id, title, description, channel_id, duration_text, view_count,
                              upload_date, thumbnail_url, thumbnail_path,
                              watch_progress_percent, watch_resume_seconds,
                              yt_status, fetch_status, fetch_error, fetched_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(video_id) DO UPDATE SET
                              title=excluded.title,
                              description=excluded.description,
                              channel_id=excluded.channel_id,
                              duration_text=excluded.duration_text,
                              view_count=excluded.view_count,
                              upload_date=excluded.upload_date,
                              thumbnail_url=excluded.thumbnail_url,
                              thumbnail_path=excluded.thumbnail_path,
                              watch_progress_percent=excluded.watch_progress_percent,
                              watch_resume_seconds=excluded.watch_resume_seconds,
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
                                bounded_int(metadata.get("watch_progress_percent")),
                                max(0, int(metadata.get("watch_resume_seconds") or 0)),
                                metadata.get("yt_status", ""),
                                status,
                                error,
                                now,
                                now,
                            ),
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


