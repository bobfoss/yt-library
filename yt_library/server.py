"""HTTP server and request routing for YT Library Manager."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.server
import json
import math
import os
import posixpath
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import (
    CARD_LAYOUTS,
    PAGE_SIZES,
    SORT_PREFERENCE_VALUES,
    configured_archivarix_max_in_flight,
    configured_admin_advanced,
    configured_dispatch_mode,
    configured_display_timezone,
    configured_filter_preferences,
    configured_history_card_layout,
    configured_job_dispatch_delay,
    configured_page_size,
    configured_partial_completion_min_percent,
    configured_playlist_card_layout,
    configured_proxy_address,
    configured_request_delay_range,
    configured_search_card_layout,
    configured_sort_preferences,
    configured_update_frequency,
    configured_update_hour_minute,
    configured_update_time,
    configured_use_proxy,
    configured_youtube_max_in_flight,
    config_path,
    effective_display_timezone,
    ensure_config_file,
    ensure_directory,
    next_update_at,
    save_config,
    valid_update_frequency,
    valid_filter_preference_key,
    valid_update_hour_minute,
    valid_update_time,
)
from .cookie_files import (
    COOKIE_CONFIG,
    MAX_COOKIE_FILE_BYTES,
    CookieFileError,
    cookie_file_status,
    replace_cookie_file,
)
from .core import (
    FEATURE_BACKFILL_KINDS,
    ROOT,
    admin_status,
    clear_external_service_block,
    clear_worker_queue,
    connect,
    enqueue_account_sync_task,
    enqueue_all_playlist_scan_items,
    enqueue_feature_backfill,
    enqueue_history_task,
    enqueue_initialization_tasks,
    enqueue_update_tasks,
    enqueue_worker_queue_target,
    external_service_block,
    import_history,
    log_live_history_event,
    log_playlist_scan_event,
    log_worker_queue_event,
    metadata_queue_count,
    migrate_database,
    rebuild_history_reconciliation,
    rebuild_metadata_queue,
    rebuild_playlist_reconciliation,
    rebuild_playlist_scan_queue,
    reconcile_worker_runs,
    refresh_exact_history_dates,
    remove_worker_queue_entry,
    takeout_import_message,
    utc_now,
    valid_timezone_name,
    worker_log_cursors,
    worker_log_page,
    worker_logs_after,
    worker_queue_count,
    worker_queue_event_cursor,
    worker_queue_events_after,
    worker_queue_rows,
    worker_queue_rows_by_id,
)
from .network import validated_socks5_proxy_url
from .request_pacing import configure_request_pacing
from .plugins import PluginManager
from .queries import (
    channel_detail_data,
    channel_list_data,
    history_activity_data,
    history_search_data,
    library_bootstrap_data,
    omni_search_data,
    playlist_detail_data,
    playlist_list_data,
    video_collection_data,
    video_detail_data,
    video_summaries_data,
)
from .templates import load_template
from .workers import (
    LIVE_HISTORY_WORKER,
    METADATA_WORKER,
    PLACEHOLDER_RECOVERY_WORKER,
    PLAYLIST_SCAN_WORKER,
    WORKER_QUEUE_DISPATCHER,
)


INDEX_HTML = load_template("index.html")
ADMIN_HTML = load_template("admin.html")
THEME_JS = load_template("theme.js")
TIMEZONE_JS = load_template("timezone.js")
VIDEO_CARD_JS = load_template("video-card.js")
COLLECTION_CARD_JS = load_template("collection-card.js")
INDEX_JS = load_template("index.js")
ADMIN_JS = load_template("admin.js")
FAVICON_SVG = load_template("favicon.svg")

PREFERENCE_POST_PATHS = frozenset(
    {
        "/api/settings/layout",
        "/api/settings/sort",
        "/api/settings/page-size",
        "/api/settings/partial-completion-minimum",
        "/api/settings/filter-preference",
    }
)
ADMIN_CONFIGURATION_POST_PATHS = frozenset(
    {
        "/api/admin/update-schedule",
        "/api/admin/advanced",
        "/api/admin/settings",
        "/api/admin/service/restart",
        "/api/settings/timezone",
        "/api/admin/dispatch-settings",
    }
)


def query_set_param(
    params: dict[str, list[str]],
    name: str,
) -> set[str] | None:
    value = (params.get(name) or [""])[0]
    if value == "__none__":
        return set()
    return {item for item in value.split(",") if item} if value else None


def query_bool_param(
    params: dict[str, list[str]],
    name: str,
    *,
    default: bool = True,
    legacy_name: str = "",
) -> bool:
    values = params.get(name)
    if values is None and legacy_name:
        values = params.get(legacy_name)
    if values is None:
        return default
    return values[0].strip().lower() not in {"0", "false", "no"}


def query_partial_min_percent(
    params: dict[str, list[str]],
    name: str,
) -> int:
    try:
        return max(1, min(99, int((params.get(name) or ["1"])[0] or 1)))
    except ValueError:
        return 1


def video_collection_filter_args(params: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "include_public": query_bool_param(params, "public", legacy_name="videos"),
        "include_unlisted": query_bool_param(params, "unlisted"),
        "include_private": query_bool_param(params, "private"),
        "include_members_only": query_bool_param(params, "members_only"),
        "include_unavailable": query_bool_param(params, "unavailable"),
        "include_unknown": query_bool_param(params, "unknown"),
        "include_removed": query_bool_param(params, "removed"),
        "duplicates_only": query_bool_param(params, "duplicates", default=False),
        "completion_filters": query_set_param(params, "completion"),
        "partial_min_percent": query_partial_min_percent(
            params,
            "completion_min_percent",
        ),
    }


def service_restart_command() -> list[str]:
    return [
        sys.executable,
        str(Path(sys.argv[0]).resolve()),
        *sys.argv[1:],
    ]


def launch_service_replacement() -> None:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
    log_dir = ROOT / ".codex" / "service-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (
        (log_dir / "yt-library.out.log").open("a", encoding="utf-8") as stdout,
        (log_dir / "yt-library.err.log").open("a", encoding="utf-8") as stderr,
    ):
        subprocess.Popen(
            service_restart_command(),
            cwd=str(Path.cwd()),
            creationflags=creationflags,
            stdout=stdout,
            stderr=stderr,
        )


def utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def enqueue_library_update(
    db_path: Path,
    cookie_file: Path,
    video_thumbs: Path,
    config_data: dict[str, Any],
    *,
    scheduled: bool = False,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        with conn:
            queue_stats = enqueue_update_tasks(conn)
            log_worker_queue_event(
                conn,
                "info",
                (
                    "Scheduled update queued: "
                    if scheduled
                    else "Update queued: "
                )
                + (
                    f"{queue_stats['inserted']} new jobs, "
                    f"{queue_stats['already_queued']} already queued"
                ),
            )
    finally:
        conn.close()
    dispatcher = WORKER_QUEUE_DISPATCHER.start(
        db_path,
        cookie_file,
        video_thumbs,
        config_data,
    )
    return {"queue": queue_stats, "dispatcher": dispatcher}


class UpdateScheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._db_path: Path | None = None
        self._cookie_file: Path | None = None
        self._video_thumbs: Path | None = None
        self._config_data: dict[str, Any] | None = None
        self._next_run_at: datetime | None = None
        self._last_queued_at = ""
        self._last_error = ""

    def start(
        self,
        db_path: Path,
        cookie_file: Path,
        video_thumbs: Path,
        config_data: dict[str, Any],
    ) -> None:
        with self._lock:
            self._db_path = db_path
            self._cookie_file = cookie_file
            self._video_thumbs = video_thumbs
            self._config_data = config_data
            self._next_run_at = self._calculate_next_run(config_data)
            if self._thread and self._thread.is_alive():
                self._wake.set()
                return
            self._stop = threading.Event()
            self._wake = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                name="library-update-scheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def schedule_changed(self, config_data: dict[str, Any]) -> None:
        with self._lock:
            self._config_data = config_data
            self._next_run_at = self._calculate_next_run(config_data)
            self._last_error = ""
        self._wake.set()

    def status(self, config_data: dict[str, Any]) -> dict[str, Any]:
        frequency = configured_update_frequency(config_data)
        enabled = frequency != "off"
        with self._lock:
            next_run_at = self._next_run_at
            if enabled and next_run_at is None:
                next_run_at = next_update_at(config_data)
            return {
                "enabled": enabled,
                "frequency": frequency,
                "hourMinute": configured_update_hour_minute(config_data),
                "time": configured_update_time(config_data),
                "nextRunAt": utc_timestamp(next_run_at) if enabled and next_run_at else "",
                "lastQueuedAt": self._last_queued_at,
                "lastError": self._last_error,
            }

    @staticmethod
    def _calculate_next_run(config_data: dict[str, Any]) -> datetime | None:
        if configured_update_frequency(config_data) == "off":
            return None
        return next_update_at(config_data)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                config_data = self._config_data
                next_run_at = self._next_run_at
                db_path = self._db_path
                cookie_file = self._cookie_file
                video_thumbs = self._video_thumbs
            if (
                not config_data
                or configured_update_frequency(config_data) == "off"
                or not next_run_at
                or not db_path
                or not cookie_file
                or not video_thumbs
            ):
                self._wake.wait(60)
                self._wake.clear()
                continue

            wait_seconds = (next_run_at - datetime.now(timezone.utc)).total_seconds()
            if wait_seconds > 0:
                self._wake.wait(min(60, wait_seconds))
                self._wake.clear()
                continue

            queued_at = utc_now()
            error = ""
            try:
                result = enqueue_library_update(
                    db_path,
                    cookie_file,
                    video_thumbs,
                    config_data,
                    scheduled=True,
                )
                dispatcher = result["dispatcher"]
                if dispatcher.get("blocked"):
                    error = str(dispatcher.get("message") or "Worker queue could not start")
            except Exception as exc:
                error = str(exc)
            with self._lock:
                self._last_queued_at = queued_at
                self._last_error = error
                self._next_run_at = self._calculate_next_run(config_data)


UPDATE_SCHEDULER = UpdateScheduler()


class LibraryHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args,
        db_path: Path,
        cookie_file: Path,
        video_thumbs: Path,
        takeout_dir: Path,
        config_data: dict[str, Any],
        plugin_manager: PluginManager,
        service_started_at: str,
        restart_pending: Callable[[], bool],
        request_restart: Callable[[], bool],
        directory: str | None = None,
        **kwargs,
    ):
        self.db_path = db_path
        self.cookie_file = cookie_file
        self.video_thumbs = video_thumbs
        self.takeout_dir = takeout_dir
        self.config_data = config_data
        self.plugin_manager = plugin_manager
        self.service_started_at = service_started_at
        self.restart_pending = restart_pending
        self.request_restart = request_restart
        super().__init__(*args, directory=directory, **kwargs)

    def _run_transaction(
        self,
        operation: Callable[[sqlite3.Connection], Any],
    ) -> Any:
        conn = connect(self.db_path)
        try:
            with conn:
                return operation(conn)
        finally:
            conn.close()

    def _start_worker_queue(self) -> dict[str, Any]:
        return WORKER_QUEUE_DISPATCHER.start(
            self.db_path,
            self.cookie_file,
            self.video_thumbs,
            self.config_data,
        )

    def _enqueue_and_start(
        self,
        enqueue: Callable[[sqlite3.Connection], Any],
    ) -> tuple[Any, dict[str, Any]]:
        result = self._run_transaction(enqueue)
        return result, self._start_worker_queue()

    def _handle_initialize(self) -> None:
        queue_stats, dispatcher = self._enqueue_and_start(enqueue_initialization_tasks)
        self.send_json({"ok": True, "queue": queue_stats, "dispatcher": dispatcher})

    def _handle_metadata_start(self, params: dict[str, list[str]]) -> None:
        stale_days = max(0, int((params.get("stale_days") or ["30"])[0] or 30))
        force = (params.get("force") or ["0"])[0] in {"1", "true", "yes"}
        metadata_kind = (params.get("kind") or ["all"])[0].strip().lower()
        if metadata_kind not in {"all", "video", "channel"}:
            self.send_json(
                {"error": "Metadata kind must be all, video, or channel"},
                status=400,
            )
            return

        def enqueue(conn: sqlite3.Connection) -> dict[str, Any]:
            if metadata_queue_count(conn, metadata_kind=metadata_kind) == 0:
                return rebuild_metadata_queue(
                    conn,
                    force=force,
                    stale_days=stale_days,
                    metadata_kind=metadata_kind,
                )
            return {
                "cleared": 0,
                "inserted": 0,
                "queued": metadata_queue_count(conn, metadata_kind=metadata_kind),
            }

        queue_stats, dispatcher = self._enqueue_and_start(enqueue)
        self.send_json({"queue": queue_stats, "dispatcher": dispatcher})

    def _handle_feature_backfill_start(self, params: dict[str, list[str]]) -> None:
        kind = (params.get("kind") or [""])[0].strip().lower()
        if kind not in FEATURE_BACKFILL_KINDS:
            self.send_json(
                {
                    "error": (
                        "Feature backfill kind must be video_visibility, "
                        "playlist_metadata, or channel_account"
                    )
                },
                status=400,
            )
            return
        limit = max(0, int((params.get("limit") or ["0"])[0] or 0))
        queue_stats, dispatcher = self._enqueue_and_start(
            lambda conn: enqueue_feature_backfill(conn, kind, limit=limit)
        )
        self.send_json({"queue": queue_stats, "dispatcher": dispatcher})

    def _handle_playlist_scan_start(self) -> None:
        queue_stats, dispatcher = self._enqueue_and_start(
            lambda conn: enqueue_all_playlist_scan_items(
                conn,
                force=True,
                stale_days=7,
                discover_current=True,
            )
        )
        self.send_json({"queue": queue_stats, "dispatcher": dispatcher})

    def _handle_history_fetch_start(self, mode: str) -> None:
        def enqueue(conn: sqlite3.Connection) -> None:
            enqueue_history_task(conn, mode, priority=0, manual=True)
            enqueue_account_sync_task(conn, priority=-1, manual=True)

        _, dispatcher = self._enqueue_and_start(enqueue)
        self.send_json({"dispatcher": dispatcher})

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        cache_control: str,
        status: int = 200,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_page_get(self, path: str) -> bool:
        static_assets = {
            "/favicon.svg": (FAVICON_SVG, "image/svg+xml"),
            "/timezone.js": (TIMEZONE_JS, "text/javascript; charset=utf-8"),
            "/theme.js": (THEME_JS, "text/javascript; charset=utf-8"),
            "/video-card.js": (VIDEO_CARD_JS, "text/javascript; charset=utf-8"),
            "/collection-card.js": (
                COLLECTION_CARD_JS,
                "text/javascript; charset=utf-8",
            ),
            "/index.js": (INDEX_JS, "text/javascript; charset=utf-8"),
            "/admin.js": (ADMIN_JS, "text/javascript; charset=utf-8"),
        }
        asset = static_assets.get(path)
        if asset is not None:
            text, content_type = asset
            self._send_bytes(
                text.encode("utf-8"),
                content_type,
                cache_control="no-cache",
            )
            return True
        if path in {"/", "/index.html"}:
            self._send_bytes(
                self.render_page(INDEX_HTML),
                "text/html; charset=utf-8",
                cache_control="no-store",
            )
            return True
        if path == "/admin":
            self._send_bytes(
                self.render_page(ADMIN_HTML),
                "text/html; charset=utf-8",
                cache_control="no-store",
            )
            return True
        if path == "/history":
            self.send_response(302)
            self.send_header("Location", "/#view=history")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def _handle_admin_get(self, parsed: urllib.parse.ParseResult) -> None:
        if parsed.path == "/api/admin/service/status":
            self.send_json({"service": self.service_status()})
            return
        if parsed.path == "/api/admin/status":
            params = urllib.parse.parse_qs(parsed.query)
            include_logs = (params.get("include_logs") or ["1"])[0].strip().lower() not in {"0", "false", "no"}
            try:
                worker_queue_limit = max(0, min(10000, int((params.get("queue_limit") or ["500"])[0] or 500)))
            except ValueError:
                worker_queue_limit = 500
            data = admin_status(
                self.db_path,
                METADATA_WORKER,
                PLAYLIST_SCAN_WORKER,
                LIVE_HISTORY_WORKER,
                WORKER_QUEUE_DISPATCHER,
                include_logs,
                worker_queue_limit,
            )
            data["dispatchSettings"] = self.dispatch_settings()
            data["settings"] = self.admin_settings()
            data["service"] = self.service_status()
            data["plugins"] = self.plugin_manager.statuses()
            self.send_json(data)
            return
        if parsed.path == "/api/admin/cookies/status":
            self.send_json(
                {
                    "cookies": {
                        kind: cookie_file_status(
                            config_path(self.config_data, config_key),
                            expected_domains,
                        )
                        for kind, (config_key, expected_domains) in COOKIE_CONFIG.items()
                    }
                }
            )
            return
        if parsed.path == "/api/admin/queue/events":
            self.stream_worker_queue_events()
            return
        if parsed.path == "/api/admin/logs/events":
            self.stream_worker_log_events()
            return
        if parsed.path == "/api/admin/logs":
            params = urllib.parse.parse_qs(parsed.query)
            source = (params.get("source") or [""])[0].strip().lower()
            severity = (params.get("level") or [""])[0].strip().lower()
            if source == "all":
                source = ""
            if severity == "all":
                severity = ""
            try:
                limit = max(1, min(200, int((params.get("limit") or ["100"])[0] or 100)))
                offset = max(0, int((params.get("offset") or ["0"])[0] or 0))
            except ValueError:
                self.send_json({"error": "Invalid log pagination"}, status=400)
                return
            conn = connect(self.db_path)
            try:
                try:
                    rows, total = worker_log_page(
                        conn,
                        limit=limit,
                        offset=offset,
                        source=source,
                        severity=severity,
                    )
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, status=400)
                    return
                self.send_json(
                    {
                        "limit": limit,
                        "offset": offset,
                        "total": total,
                        "rows": rows,
                    }
                )
            finally:
                conn.close()
            return
        if parsed.path == "/api/admin/queue":
            params = urllib.parse.parse_qs(parsed.query)
            queue_type = (params.get("type") or [""])[0]
            try:
                limit = max(1, min(100, int((params.get("limit") or ["20"])[0] or 20)))
            except ValueError:
                limit = 20
            try:
                offset = max(0, int((params.get("offset") or ["0"])[0] or 0))
            except ValueError:
                offset = 0
            include_total = (params.get("include_total") or ["1"])[0] not in {"0", "false", "no"}
            conn = connect(self.db_path)
            try:
                if queue_type == "worker":
                    total = worker_queue_count(conn) if include_total else 0
                    rows = worker_queue_rows(conn, limit=limit, offset=offset)
                else:
                    self.send_json({"error": "Unknown queue type"}, status=400)
                    return
                self.send_json(
                    {
                        "type": queue_type,
                        "limit": limit,
                        "offset": offset,
                        "total": total,
                        "rows": [dict(row) for row in rows],
                    }
                )
            finally:
                conn.close()
            return

        return super().do_GET()

    def _handle_library_get(self, parsed: urllib.parse.ParseResult) -> None:
        if parsed.path.startswith("/plugins/"):
            suffix = parsed.path[len("/plugins/") :]
            plugin_id, separator, asset_path = suffix.partition("/assets/")
            if not separator or not plugin_id or not asset_path:
                self.send_json({"error": "Plugin browser asset is required"}, status=404)
                return
            status, content_type, body = self.plugin_manager.handle_browser_asset(
                plugin_id,
                urllib.parse.unquote(asset_path),
            )
            self._send_bytes(
                body,
                content_type,
                cache_control="no-cache",
                status=status,
            )
            return
        if parsed.path == "/api/plugins":
            self.send_json({"plugins": self.plugin_manager.statuses()})
            return
        if parsed.path.startswith("/api/plugins/"):
            suffix = parsed.path[len("/api/plugins/") :]
            plugin_id, separator, plugin_path = suffix.partition("/")
            if not separator or not plugin_path:
                self.send_json({"error": "Plugin route is required"}, status=404)
                return
            status, payload = self.plugin_manager.handle_api(
                plugin_id,
                "GET",
                plugin_path,
                urllib.parse.parse_qs(parsed.query),
            )
            self.send_json(payload, status=status)
            return
        if parsed.path == "/api/settings":
            conn = connect(self.db_path)
            try:
                self.send_json(
                    {
                        "displayTimezone": self.display_timezone_name(),
                        **self.layout_settings(),
                    }
                )
            finally:
                conn.close()
            return
        if parsed.path == "/api/bootstrap":
            conn = connect(self.db_path)
            try:
                data = library_bootstrap_data(conn)
            finally:
                conn.close()
            data["plugins"] = self.plugin_manager.statuses()
            self.send_json(data)
            return
        if parsed.path == "/api/playlists":
            params = urllib.parse.parse_qs(parsed.query)
            visibility_value = (params.get("visibility") or [""])[0]
            visibilities = (
                {value for value in visibility_value.split(",") if value}
                if visibility_value
                else None
            )
            try:
                limit = max(1, min(500, int((params.get("limit") or ["100"])[0] or 100)))
                offset = max(0, int((params.get("offset") or ["0"])[0] or 0))
            except ValueError:
                limit, offset = 100, 0
            conn = connect(self.db_path)
            try:
                data = playlist_list_data(
                    conn,
                    query=(params.get("q") or [""])[0],
                    visibilities=visibilities,
                    include_removed=(params.get("removed") or ["1"])[0] != "0",
                    sort=(params.get("sort") or ["title"])[0],
                    unavailable_only=(params.get("unavailable_only") or ["0"])[0] == "1",
                    group_key=(params.get("group_key") or [""])[0],
                    limit=limit,
                    offset=offset,
                )
            finally:
                conn.close()
            self.send_json(data)
            return
        if parsed.path.startswith("/api/playlists/"):
            suffix = parsed.path[len("/api/playlists/") :]
            is_videos = suffix.endswith("/videos")
            playlist_id = urllib.parse.unquote(suffix[: -len("/videos")] if is_videos else suffix)
            conn = connect(self.db_path)
            try:
                if is_videos:
                    params = urllib.parse.parse_qs(parsed.query)
                    try:
                        limit = max(1, min(500, int((params.get("limit") or ["100"])[0] or 100)))
                        offset = max(0, int((params.get("offset") or ["0"])[0] or 0))
                    except ValueError:
                        limit, offset = 100, 0
                    data = video_collection_data(
                        conn,
                        playlist_id=playlist_id,
                        query=(params.get("q") or [""])[0],
                        **video_collection_filter_args(params),
                        sort=(params.get("sort") or ["playlist_order"])[0],
                        limit=limit,
                        offset=offset,
                    )
                else:
                    data = playlist_detail_data(conn, playlist_id)
            finally:
                conn.close()
            if data is None:
                self.send_json({"error": "Playlist not found"}, status=404)
            else:
                self.send_json(data)
            return
        if parsed.path == "/api/videos":
            params = urllib.parse.parse_qs(parsed.query)
            try:
                limit = max(1, min(500, int((params.get("limit") or ["100"])[0] or 100)))
                offset = max(0, int((params.get("offset") or ["0"])[0] or 0))
            except ValueError:
                limit, offset = 100, 0
            conn = connect(self.db_path)
            try:
                data = video_collection_data(
                    conn,
                    scope=(params.get("scope") or ["playlist"])[0],
                    channel_id=(params.get("channel_id") or [""])[0],
                    query=(params.get("q") or [""])[0],
                    **video_collection_filter_args(params),
                    sort=(params.get("sort") or ["newest_added"])[0],
                    limit=limit,
                    offset=offset,
                )
            finally:
                conn.close()
            self.send_json(data)
            return
        if parsed.path == "/api/videos/batch":
            params = urllib.parse.parse_qs(parsed.query)
            video_ids = list(dict.fromkeys(params.get("id") or []))
            if not video_ids:
                self.send_json({"error": "At least one video id is required"}, status=400)
                return
            if len(video_ids) > 100:
                self.send_json({"error": "At most 100 video ids are allowed"}, status=400)
                return
            conn = connect(self.db_path)
            try:
                data = video_summaries_data(conn, video_ids)
            finally:
                conn.close()
            self.send_json(data)
            return
        if parsed.path.startswith("/api/videos/"):
            video_id = urllib.parse.unquote(parsed.path[len("/api/videos/") :])
            conn = connect(self.db_path)
            try:
                data = video_detail_data(conn, video_id)
            finally:
                conn.close()
            if data is None:
                self.send_json({"error": "Video not found"}, status=404)
            else:
                self.send_json(data)
            return
        if parsed.path == "/api/channels":
            params = urllib.parse.parse_qs(parsed.query)
            category_value = (params.get("categories") or [""])[0]
            categories = {value for value in category_value.split(",") if value} if category_value else None
            try:
                limit = max(1, min(500, int((params.get("limit") or ["100"])[0] or 100)))
                offset = max(0, int((params.get("offset") or ["0"])[0] or 0))
            except ValueError:
                limit, offset = 100, 0
            conn = connect(self.db_path)
            try:
                data = channel_list_data(
                    conn,
                    query=(params.get("q") or [""])[0],
                    categories=categories,
                    subscribed_only=(params.get("subscribed_only") or ["0"])[0] == "1",
                    sort=(params.get("sort") or ["title"])[0],
                    limit=limit,
                    offset=offset,
                )
            finally:
                conn.close()
            self.send_json(data)
            return
        if parsed.path.startswith("/api/channels/"):
            suffix = parsed.path[len("/api/channels/") :]
            is_videos = suffix.endswith("/videos")
            channel_reference = urllib.parse.unquote(
                suffix[: -len("/videos")] if is_videos else suffix
            )
            conn = connect(self.db_path)
            try:
                channel = channel_detail_data(conn, channel_reference)
                if is_videos:
                    params = urllib.parse.parse_qs(parsed.query)
                    try:
                        limit = max(1, min(500, int((params.get("limit") or ["100"])[0] or 100)))
                        offset = max(0, int((params.get("offset") or ["0"])[0] or 0))
                    except ValueError:
                        limit, offset = 100, 0
                    data = (
                        video_collection_data(
                            conn,
                            channel_id=channel["channel_id"],
                            sort=(params.get("sort") or ["title"])[0],
                            limit=limit,
                            offset=offset,
                        )
                        if channel is not None
                        else None
                    )
                else:
                    data = channel
            finally:
                conn.close()
            if data is None:
                self.send_json({"error": "Channel not found"}, status=404)
            else:
                self.send_json(data)
            return
        if parsed.path == "/api/search":
            params = urllib.parse.parse_qs(parsed.query)
            query = (params.get("q") or [""])[0]
            search_fields = query_set_param(params, "search_fields")
            sort = (params.get("sort") or [None])[0]
            try:
                limit = max(1, min(5000, int((params.get("limit") or ["100"])[0] or 100)))
            except ValueError:
                limit = 100
            try:
                offset = max(0, int((params.get("offset") or ["0"])[0] or 0))
            except ValueError:
                offset = 0
            conn = connect(self.db_path)
            try:
                data = omni_search_data(
                    conn,
                    query,
                    search_fields=search_fields,
                    result_kinds=query_set_param(params, "kinds"),
                    playlist_group_key=(params.get("playlist_group_key") or [""])[0],
                    video_source=(params.get("video_source") or [""])[0],
                    channel_source=(params.get("channel_source") or [""])[0],
                    video_meta_filters=query_set_param(params, "video_meta"),
                    video_reaction_filters=query_set_param(params, "video_reaction"),
                    video_completion_filters=query_set_param(params, "video_completion"),
                    video_partial_min_percent=query_partial_min_percent(
                        params,
                        "video_completion_min_percent",
                    ),
                    video_playlist_membership_filters=query_set_param(
                        params,
                        "video_playlist_membership",
                    ),
                    channel_subscription_filters=query_set_param(
                        params,
                        "channel_subscription",
                    ),
                    channel_status_filters=query_set_param(params, "channel_status"),
                    playlist_meta_filters=query_set_param(params, "playlist_meta"),
                    playlist_ownership_filters=query_set_param(
                        params,
                        "playlist_ownership",
                    ),
                    playlist_status_filters=query_set_param(params, "playlist_status"),
                    sort=sort,
                    limit=limit,
                    offset=offset,
                    display_timezone=effective_display_timezone(self.config_data),
                )
            finally:
                conn.close()
            self.send_json(data)
            return
        if parsed.path == "/api/history/search":
            params = urllib.parse.parse_qs(parsed.query)
            query = (params.get("q") or [""])[0]
            channel_id = (params.get("channel_id") or [""])[0]
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
                data = history_search_data(conn, query, limit=limit, offset=offset, channel_id=channel_id)
            finally:
                conn.close()
            self.send_json(data)
            return
        if parsed.path == "/api/history/activity":
            params = urllib.parse.parse_qs(parsed.query)
            start_date = (params.get("start") or [""])[0]
            end_date = (params.get("end") or [""])[0]
            channel_id = (params.get("channel_id") or [""])[0]
            conn = connect(self.db_path)
            try:
                data = history_activity_data(
                    conn,
                    start_date=start_date,
                    end_date=end_date,
                    channel_id=channel_id,
                )
            finally:
                conn.close()
            self.send_json(data)
            return

        return super().do_GET()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if self._handle_page_get(parsed.path):
            return
        if parsed.path.startswith("/api/admin/"):
            self._handle_admin_get(parsed)
            return
        self._handle_library_get(parsed)

    def _handle_cookie_post(self, parsed: urllib.parse.ParseResult) -> None:
        if parsed.path.startswith("/api/admin/cookies/"):
            kind = parsed.path.rsplit("/", 1)[-1]
            cookie_config = COOKIE_CONFIG.get(kind)
            if cookie_config is None:
                self.send_json({"error": "Unknown cookie service"}, status=404)
                return
            if self.headers.get("X-YT-Library-Admin") != "1":
                self.send_json({"error": "Missing admin request header"}, status=403)
                return
            origin = self.headers.get("Origin", "")
            if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host", ""):
                self.send_json({"error": "Cross-origin cookie updates are not allowed"}, status=403)
                return
            if not self.headers.get("Content-Type", "").lower().startswith("text/plain"):
                self.send_json({"error": "Cookie updates require text/plain content"}, status=415)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            if content_length < 1 or content_length > MAX_COOKIE_FILE_BYTES:
                self.send_json({"error": "Cookie content must be from 1 byte to 2 MiB"}, status=413)
                return
            content = self.rfile.read(content_length)
            config_key, expected_domains = cookie_config
            try:
                status = replace_cookie_file(
                    config_path(self.config_data, config_key),
                    content,
                    expected_domains,
                )
            except CookieFileError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json({"ok": True, "kind": kind, "status": status})
            return

    def _handle_preference_post(
        self,
        parsed: urllib.parse.ParseResult,
        params: dict[str, list[str]],
    ) -> None:
        if parsed.path == "/api/settings/layout":
            context = (params.get("context") or [""])[0].strip().lower()
            layout = (params.get("value") or [""])[0].strip().lower()
            config_key = {
                "search": "search_card_layout",
                "playlist": "playlist_card_layout",
                "history": "history_card_layout",
            }.get(context)
            if config_key is None or layout not in CARD_LAYOUTS:
                self.send_json({"error": "Invalid card layout preference"}, status=400)
                return
            self.config_data[config_key] = layout
            save_config(self.config_data)
            self.send_json({"ok": True, "context": context, "layout": layout})
            return
        if parsed.path == "/api/settings/sort":
            context = (params.get("context") or [""])[0].strip().lower()
            sort = (params.get("value") or [""])[0].strip().lower()
            if sort not in SORT_PREFERENCE_VALUES.get(context, frozenset()):
                self.send_json({"error": "Invalid sort preference"}, status=400)
                return
            preferences = configured_sort_preferences(self.config_data)
            preferences[context] = sort
            self.config_data["sort_preferences"] = preferences
            save_config(self.config_data)
            self.send_json({"ok": True, "context": context, "sort": sort})
            return
        if parsed.path == "/api/settings/page-size":
            try:
                page_size = int((params.get("value") or [""])[0])
            except ValueError:
                page_size = 0
            if page_size not in PAGE_SIZES:
                self.send_json({"error": "Invalid page size preference"}, status=400)
                return
            self.config_data["page_size"] = page_size
            save_config(self.config_data)
            self.send_json({"ok": True, "pageSize": page_size})
            return
        if parsed.path == "/api/settings/partial-completion-minimum":
            try:
                minimum_percent = int((params.get("value") or [""])[0])
            except ValueError:
                minimum_percent = 0
            if not 1 <= minimum_percent <= 99:
                self.send_json(
                    {"error": "Partial completion minimum must be from 1 to 99"},
                    status=400,
                )
                return
            self.config_data["partial_completion_min_percent"] = minimum_percent
            save_config(self.config_data)
            self.send_json(
                {
                    "ok": True,
                    "partialCompletionMinPercent": minimum_percent,
                }
            )
            return
        if parsed.path == "/api/settings/filter-preference":
            preference_key = (params.get("key") or [""])[0].strip()
            enabled_value = (params.get("enabled") or [""])[0].strip().lower()
            if not valid_filter_preference_key(preference_key):
                self.send_json({"error": "Invalid filter preference"}, status=400)
                return
            if enabled_value not in {"0", "1", "false", "true"}:
                self.send_json({"error": "Invalid filter preference value"}, status=400)
                return
            enabled = enabled_value in {"1", "true"}
            preferences = configured_filter_preferences(self.config_data)
            if enabled:
                preferences[preference_key] = True
            else:
                preferences.pop(preference_key, None)
            self.config_data["filter_preferences"] = preferences
            save_config(self.config_data)
            self.send_json(
                {
                    "ok": True,
                    "key": preference_key,
                    "enabled": enabled,
                    "filterPreferences": preferences,
                }
            )
            return

    def _handle_admin_configuration_post(
        self,
        parsed: urllib.parse.ParseResult,
        params: dict[str, list[str]],
    ) -> None:
        if parsed.path == "/api/admin/update-schedule":
            frequency = (params.get("frequency") or [""])[0].strip().lower()
            if not valid_update_frequency(frequency):
                self.send_json(
                    {"error": "Update frequency must be off, hourly, or daily"},
                    status=400,
                )
                return
            update_time = (params.get("at") or [""])[0].strip()
            hour_minute = (params.get("minute") or [""])[0].strip()
            if frequency == "daily" and not valid_update_time(update_time):
                self.send_json({"error": "Update time must use HH:MM"}, status=400)
                return
            if frequency == "hourly" and not valid_update_hour_minute(hour_minute):
                self.send_json(
                    {"error": "Hourly update minute must be between 0 and 59"},
                    status=400,
                )
                return
            self.config_data["update_frequency"] = frequency
            if valid_update_time(update_time):
                self.config_data["update_time"] = update_time
            if valid_update_hour_minute(hour_minute):
                self.config_data["update_hour_minute"] = int(hour_minute)
            save_config(self.config_data)
            UPDATE_SCHEDULER.schedule_changed(self.config_data)
            self.send_json({"ok": True, "settings": self.admin_settings()})
            return
        if parsed.path == "/api/admin/advanced":
            enabled = (params.get("enabled") or ["0"])[0].strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            self.config_data["admin_advanced"] = enabled
            save_config(self.config_data)
            self.send_json({"ok": True, "settings": self.admin_settings()})
            return
        if parsed.path == "/api/admin/settings":
            timezone_name = (params.get("display_timezone") or [""])[0].strip()
            use_proxy = (params.get("use_proxy") or ["0"])[0].strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            proxy_url = (params.get("proxy") or [""])[0].strip()
            if not valid_timezone_name(timezone_name):
                self.send_json(
                    {"error": f"Invalid IANA timezone: {timezone_name}"},
                    status=400,
                )
                return
            try:
                proxy_url = validated_socks5_proxy_url(proxy_url)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            if use_proxy and not proxy_url:
                self.send_json(
                    {"error": "Enter a SOCKS5 proxy URL or clear Use proxy"},
                    status=400,
                )
                return

            previous_timezone = configured_display_timezone(self.config_data)
            previous_use_proxy = configured_use_proxy(self.config_data)
            previous_proxy = configured_proxy_address(self.config_data)
            proxy_changed = (
                previous_use_proxy != use_proxy
                or previous_proxy != proxy_url
            )
            self.config_data["display_timezone"] = timezone_name
            self.config_data["use_proxy"] = use_proxy
            self.config_data["proxy"] = proxy_url
            save_config(self.config_data)
            if previous_timezone != timezone_name:
                UPDATE_SCHEDULER.schedule_changed(self.config_data)
            if previous_timezone != timezone_name:
                conn = connect(self.db_path)
                try:
                    with conn:
                        refresh_exact_history_dates(conn, timezone_name)
                finally:
                    conn.close()
            if proxy_changed:
                conn = connect(self.db_path)
                try:
                    with conn:
                        clear_external_service_block(conn, "proxy")
                finally:
                    conn.close()
                self.request_restart()
            self.send_json(
                {
                    "ok": True,
                    "settings": self.admin_settings(),
                    "restartScheduled": proxy_changed,
                    "service": self.service_status(),
                }
            )
            return
        if parsed.path == "/api/admin/service/restart":
            scheduled = self.request_restart()
            self.send_json(
                {
                    "ok": True,
                    "restartScheduled": scheduled,
                    "service": self.service_status(),
                }
            )
            return
        if parsed.path == "/api/settings/timezone":
            value = (params.get("value") or [""])[0].strip()
            if not valid_timezone_name(value):
                self.send_json({"error": f"Invalid IANA timezone: {value}"}, status=400)
                return
            conn = connect(self.db_path)
            try:
                with conn:
                    self.config_data["display_timezone"] = value
                    save_config(self.config_data)
                    UPDATE_SCHEDULER.schedule_changed(self.config_data)
                    refresh_exact_history_dates(conn, value)
            finally:
                conn.close()
            self.send_json({"ok": True, "displayTimezone": value})
            return
        if parsed.path == "/api/admin/dispatch-settings":
            try:
                job_dispatch_delay = float(
                    (params.get("job_dispatch_delay_seconds") or [""])[0]
                )
                request_delay_min = float(
                    (params.get("request_delay_min_seconds") or [""])[0]
                )
                request_delay_max = float(
                    (params.get("request_delay_max_seconds") or [""])[0]
                )
                youtube_max_in_flight = int(
                    (params.get("youtube_max_in_flight") or [""])[0]
                )
                archivarix_max_in_flight = int(
                    (params.get("archivarix_max_in_flight") or [""])[0]
                )
            except ValueError:
                self.send_json(
                    {"error": "Dispatch settings must be numbers"},
                    status=400,
                )
                return
            request_delays = (
                job_dispatch_delay,
                request_delay_min,
                request_delay_max,
            )
            if (
                any(not math.isfinite(value) for value in request_delays)
                or any(value < 0 for value in request_delays)
            ):
                self.send_json(
                    {"error": "Delays must be finite and zero or greater"},
                    status=400,
                )
                return
            if request_delay_max < request_delay_min:
                self.send_json(
                    {"error": "Throttle maximum must be greater than or equal to minimum"},
                    status=400,
                )
                return
            if not 1 <= youtube_max_in_flight <= 100:
                self.send_json(
                    {"error": "YouTube max in flight must be between 1 and 100"},
                    status=400,
                )
                return
            if not 1 <= archivarix_max_in_flight <= 20:
                self.send_json(
                    {"error": "Archivarix max in flight must be between 1 and 20"},
                    status=400,
                )
                return
            dispatch_mode = (
                (params.get("dispatch_mode") or [""])[0].strip().lower()
            )
            if dispatch_mode not in {"delay", "throttle"}:
                self.send_json(
                    {"error": "Dispatch mode must be delay or throttle"},
                    status=400,
                )
                return
            self.config_data["dispatch_mode"] = dispatch_mode
            self.config_data["job_dispatch_delay_seconds"] = job_dispatch_delay
            self.config_data["request_delay_min_seconds"] = request_delay_min
            self.config_data["request_delay_max_seconds"] = request_delay_max
            self.config_data["youtube_max_in_flight"] = youtube_max_in_flight
            self.config_data["archivarix_max_in_flight"] = archivarix_max_in_flight
            save_config(self.config_data)
            WORKER_QUEUE_DISPATCHER.update_dispatch_settings(
                dispatch_mode,
                job_dispatch_delay,
                youtube_max_in_flight,
                archivarix_max_in_flight,
            )
            configure_request_pacing(self.config_data)
            self.send_json(
                {
                    "ok": True,
                    "dispatchSettings": self.dispatch_settings(),
                }
            )
            return

    def _handle_admin_action_post(
        self,
        parsed: urllib.parse.ParseResult,
        params: dict[str, list[str]],
    ) -> None:
        if parsed.path == "/api/admin/initialize":
            self._handle_initialize()
            return
        if parsed.path == "/api/admin/update/start":
            result = enqueue_library_update(
                self.db_path,
                self.cookie_file,
                self.video_thumbs,
                self.config_data,
            )
            self.send_json({"ok": True, **result})
            return
        if parsed.path == "/api/admin/metadata/start":
            self._handle_metadata_start(params)
            return
        if parsed.path == "/api/admin/feature-backfill/start":
            self._handle_feature_backfill_start(params)
            return
        if parsed.path == "/api/admin/queue/add-target":
            target = (params.get("target") or [""])[0]
            conn = connect(self.db_path)
            try:
                with conn:
                    try:
                        result = enqueue_worker_queue_target(conn, target)
                    except ValueError as exc:
                        self.send_json({"error": str(exc)}, status=400)
                        return
                self.send_json({"ok": True, **result})
            finally:
                conn.close()
            return
        if parsed.path == "/api/admin/queue/rebuild":
            if (
                WORKER_QUEUE_DISPATCHER.is_running()
                or METADATA_WORKER.is_running()
                or PLAYLIST_SCAN_WORKER.is_running()
                or LIVE_HISTORY_WORKER.is_running()
            ):
                self.send_json({"error": "Stop active workers before rebuilding the queue"}, status=409)
                return
            conn = connect(self.db_path)
            try:
                with conn:
                    cleared = clear_worker_queue(conn)
                    metadata = rebuild_metadata_queue(conn, force=False, stale_days=30)
                    playlists = rebuild_playlist_scan_queue(conn, force=False, stale_days=7)
                    enqueue_history_task(conn, "recent", priority=0, manual=False)
                    enqueue_account_sync_task(conn, priority=-1, manual=False)
                self.send_json({"ok": True, "cleared": cleared, "metadata": metadata, "playlists": playlists, "history": 1, "account": 1})
            finally:
                conn.close()
            return
        if parsed.path == "/api/admin/queue/clear":
            if (
                WORKER_QUEUE_DISPATCHER.is_running()
                or METADATA_WORKER.is_running()
                or PLAYLIST_SCAN_WORKER.is_running()
                or LIVE_HISTORY_WORKER.is_running()
            ):
                self.send_json({"error": "Stop active workers before clearing the queue"}, status=409)
                return
            conn = connect(self.db_path)
            try:
                with conn:
                    cleared = clear_worker_queue(conn)
            finally:
                conn.close()
            self.send_json({"ok": True, "cleared": cleared})
            return
        if parsed.path == "/api/admin/queue/remove":
            try:
                queue_id = int((params.get("queue_id") or ["0"])[0] or 0)
            except ValueError:
                queue_id = 0
            if not queue_id:
                self.send_json({"error": "Missing queue_id"}, status=400)
                return
            conn = connect(self.db_path)
            try:
                with conn:
                    removed = remove_worker_queue_entry(conn, queue_id)
            finally:
                conn.close()
            self.send_json({"ok": removed, "removed": removed})
            return
        if parsed.path == "/api/admin/queue/start":
            dispatcher = self._start_worker_queue()
            self.send_json({"ok": True, "dispatcher": dispatcher})
            return
        if parsed.path == "/api/admin/archivarix/retry":
            conn = connect(self.db_path)
            try:
                with conn:
                    cleared = clear_external_service_block(conn, "archivarix")
            finally:
                conn.close()
            WORKER_QUEUE_DISPATCHER.allow_archivarix_retry()
            dispatcher = self._start_worker_queue()
            self.send_json({"ok": True, "cleared": cleared, "dispatcher": dispatcher})
            return
        if parsed.path == "/api/admin/proxy/retry":
            conn = connect(self.db_path)
            try:
                with conn:
                    was_blocked = external_service_block(conn, "proxy")["blocked"]
                    log_worker_queue_event(
                        conn,
                        "info",
                        "Proxy retry requested; restarting the worker queue.",
                    )
            finally:
                conn.close()
            dispatcher = self._start_worker_queue()
            conn = connect(self.db_path)
            try:
                proxy_block = external_service_block(conn, "proxy")
            finally:
                conn.close()
            self.send_json(
                {
                    "ok": not bool(dispatcher.get("blocked")),
                    "cleared": bool(was_blocked and not proxy_block["blocked"]),
                    "proxyBlock": proxy_block,
                    "dispatcher": dispatcher,
                }
            )
            return
        if parsed.path == "/api/admin/queue/stop":
            result = {
                "dispatcher": WORKER_QUEUE_DISPATCHER.stop(),
                "metadata": METADATA_WORKER.stop(),
                "playlists": PLAYLIST_SCAN_WORKER.stop(),
                "history": LIVE_HISTORY_WORKER.stop(),
            }
            self.send_json({"ok": True, **result})
            return
        if parsed.path == "/api/admin/playlists/start":
            self._handle_playlist_scan_start()
            return
        if parsed.path == "/api/admin/playlists/reconcile":
            conn = connect(self.db_path)
            run_id = uuid.uuid4().hex
            started_at = utc_now()
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
                            utc_now(),
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
            self._handle_history_fetch_start("recent")
            return
        if parsed.path == "/api/admin/live-history/verify":
            self._handle_history_fetch_start("verify")
            return
        if parsed.path == "/api/admin/history/import-takeout":
            try:
                ensure_directory(self.takeout_dir)
            except OSError as exc:
                self.send_json({"error": f"Could not create Takeout directory: {exc}"}, status=500)
                return
            run_id = uuid.uuid4().hex
            started_at = utc_now()
            conn = connect(self.db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO live_history_worker_runs(
                          run_id, status, started_at, requested_limit, message
                        )
                        VALUES (?, 'running', ?, 0, 'Takeout directory import started')
                        """,
                        (run_id, started_at),
                    )
                    log_live_history_event(conn, run_id, "info", "Takeout directory import started")
            finally:
                conn.close()
            try:
                import_stats = import_history(
                    argparse.Namespace(
                        db=str(self.db_path),
                        takeout=str(self.takeout_dir),
                        history_key="",
                        config_data=self.config_data,
                    )
                )
            except SystemExit as exc:
                message = str(exc)
                conn = connect(self.db_path)
                try:
                    with conn:
                        conn.execute(
                            """
                            UPDATE live_history_worker_runs
                            SET status = 'error', finished_at = ?, failed = 1, message = ?
                            WHERE run_id = ?
                            """,
                            (utc_now(), message, run_id),
                        )
                        log_live_history_event(conn, run_id, "error", message)
                finally:
                    conn.close()
                self.send_json({"error": str(exc)}, status=400)
                return
            message = takeout_import_message(import_stats)
            conn = connect(self.db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        UPDATE live_history_worker_runs
                        SET status = 'complete',
                            finished_at = ?,
                            total = ?,
                            processed = ?,
                            found = ?,
                            skipped = ?,
                            message = ?
                        WHERE run_id = ?
                        """,
                        (
                            utc_now(),
                            import_stats["total_watch_rows"],
                            import_stats["total_watch_rows"],
                            import_stats["inserted_watch_rows"],
                            import_stats["duplicate_watch_rows"],
                            message,
                            run_id,
                        ),
                    )
                    log_live_history_event(conn, run_id, "info", message)
            finally:
                conn.close()
            self.send_json({"ok": True, "run_id": run_id, "message": message, **import_stats})
            return
        if parsed.path == "/api/admin/history/reconcile":
            conn = connect(self.db_path)
            try:
                with conn:
                    stats = rebuild_history_reconciliation(conn, effective_display_timezone(self.config_data))
            finally:
                conn.close()
            self.send_json({"ok": True, **stats})
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path.startswith("/api/admin/cookies/"):
            self._handle_cookie_post(parsed)
            return
        if parsed.path in PREFERENCE_POST_PATHS:
            self._handle_preference_post(parsed, params)
            return
        if parsed.path in ADMIN_CONFIGURATION_POST_PATHS:
            self._handle_admin_configuration_post(parsed, params)
            return
        if parsed.path.startswith("/api/admin/"):
            self._handle_admin_action_post(parsed, params)
            return
        self.send_error(404, "Not found")

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/settings/timezone":
            self.send_error(404, "Not found")
            return
        value = ""
        self.config_data["display_timezone"] = value
        save_config(self.config_data)
        self.send_json({"ok": True, "displayTimezone": value})

    def render_page(self, template: str) -> bytes:
        timezone_name = self.display_timezone_name()
        config = json.dumps(
            {
                "displayTimezone": timezone_name,
                **self.layout_settings(),
            },
            ensure_ascii=False,
        )
        scripts = (
            f"<script>window.YT_LIBRARY_CONFIG={config};</script>"
            '<script src="/timezone.js"></script>'
            '<script src="/video-card.js"></script>'
            '<script src="/collection-card.js"></script>'
        )
        return template.replace("</head>", scripts + "</head>").encode("utf-8")

    def display_timezone_name(self) -> str:
        return configured_display_timezone(self.config_data)

    def layout_settings(self) -> dict[str, Any]:
        return {
            "searchCardLayout": configured_search_card_layout(self.config_data),
            "playlistCardLayout": configured_playlist_card_layout(self.config_data),
            "historyCardLayout": configured_history_card_layout(self.config_data),
            "sortPreferences": configured_sort_preferences(self.config_data),
            "pageSize": configured_page_size(self.config_data),
            "partialCompletionMinPercent": (
                configured_partial_completion_min_percent(self.config_data)
            ),
            "filterPreferences": configured_filter_preferences(self.config_data),
        }

    def dispatch_settings(self) -> dict[str, Any]:
        request_delay_min, request_delay_max = configured_request_delay_range(
            self.config_data
        )
        dispatch_mode = configured_dispatch_mode(self.config_data)
        job_dispatch_delay = configured_job_dispatch_delay(self.config_data)
        return {
            "dispatch_mode": dispatch_mode,
            "job_dispatch_delay_seconds": job_dispatch_delay,
            "effective_job_dispatch_delay_seconds": (
                0.0 if dispatch_mode == "throttle" else job_dispatch_delay
            ),
            "request_delay_min_seconds": request_delay_min,
            "request_delay_max_seconds": request_delay_max,
            "youtube_max_in_flight": configured_youtube_max_in_flight(
                self.config_data
            ),
            "archivarix_max_in_flight": configured_archivarix_max_in_flight(
                self.config_data
            ),
        }

    def admin_settings(self) -> dict[str, Any]:
        return {
            "displayTimezone": configured_display_timezone(self.config_data),
            "useProxy": configured_use_proxy(self.config_data),
            "proxy": configured_proxy_address(self.config_data),
            "updateFrequency": configured_update_frequency(self.config_data),
            "updateHourMinute": configured_update_hour_minute(self.config_data),
            "updateTime": configured_update_time(self.config_data),
            "updateSchedule": UPDATE_SCHEDULER.status(self.config_data),
            "adminAdvanced": configured_admin_advanced(self.config_data),
        }

    def service_status(self) -> dict[str, Any]:
        return {
            "status": "restarting" if self.restart_pending() else "running",
            "pid": os.getpid(),
            "startedAt": self.service_started_at,
        }

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_sse(self, event: str, data: Any, event_id: int | None = None) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        parts = []
        if event_id is not None:
            parts.append(f"id: {event_id}\n")
        parts.append(f"event: {event}\n")
        parts.append(f"data: {payload}\n\n")
        self.wfile.write("".join(parts).encode("utf-8"))
        self.wfile.flush()

    def stream_worker_queue_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN")
            cursor = worker_queue_event_cursor(conn)
            rows = [dict(row) for row in worker_queue_rows(conn)]
            total = len(rows)
            conn.commit()

            self.send_sse("queue_reset", {"total": total}, cursor)
            for offset in range(0, total, 250):
                self.send_sse(
                    "queue_snapshot",
                    {"rows": rows[offset : offset + 250], "total": total},
                    cursor,
                )
            self.send_sse("queue_ready", {"total": total}, cursor)

            last_heartbeat = time.monotonic()
            while True:
                events = worker_queue_events_after(conn, cursor, limit=500)
                if events:
                    latest_by_queue: dict[int, sqlite3.Row] = {}
                    for row in events:
                        latest_by_queue[int(row["queue_id"])] = row
                    cursor = int(events[-1]["event_id"])
                    removals = [
                        queue_id
                        for queue_id, row in latest_by_queue.items()
                        if row["operation"] == "remove"
                    ]
                    upsert_ids = [
                        queue_id
                        for queue_id, row in latest_by_queue.items()
                        if row["operation"] != "remove"
                    ]
                    upserts = [dict(row) for row in worker_queue_rows_by_id(conn, upsert_ids)]
                    existing_ids = {int(row["queue_id"]) for row in upserts}
                    removals.extend(queue_id for queue_id in upsert_ids if queue_id not in existing_ids)
                    self.send_sse(
                        "queue_delta",
                        {
                            "upserts": upserts,
                            "removals": sorted(set(removals)),
                            "total": worker_queue_count(conn),
                        },
                        cursor,
                    )
                    last_heartbeat = time.monotonic()
                    continue
                if time.monotonic() - last_heartbeat >= 15:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        finally:
            conn.close()

    def stream_worker_log_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        conn = connect(self.db_path)
        try:
            cursors = worker_log_cursors(conn)
            self.send_sse(
                "log_reset",
                {"cursors": cursors},
            )

            last_heartbeat = time.monotonic()
            while True:
                logs = worker_logs_after(conn, cursors, limit=500)
                if any(logs.values()):
                    for name, rows in logs.items():
                        if rows:
                            cursors[name] = int(rows[-1]["id"])
                    self.send_sse(
                        "log_delta",
                        {
                            **{name: [dict(row) for row in rows] for name, rows in logs.items()},
                            "cursors": cursors,
                        },
                    )
                    last_heartbeat = time.monotonic()
                    continue
                if time.monotonic() - last_heartbeat >= 15:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        finally:
            conn.close()

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
    ensure_config_file(args.config_data)
    configure_request_pacing(args.config_data)
    migrate_database(db_path)
    conn = connect(db_path)
    try:
        conn.execute("SELECT 1 FROM playlists LIMIT 1")
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"Database schema migration failed: {exc}") from exc
    finally:
        conn.close()
    reconcile_worker_runs(
        db_path,
        METADATA_WORKER,
        PLAYLIST_SCAN_WORKER,
        LIVE_HISTORY_WORKER,
        PLACEHOLDER_RECOVERY_WORKER,
    )
    plugin_manager = PluginManager(args.config_data)
    service_started_at = utc_now()
    restart_requested = threading.Event()
    server: http.server.ThreadingHTTPServer | None = None

    def restart_pending() -> bool:
        return restart_requested.is_set()

    def request_restart() -> bool:
        if restart_requested.is_set():
            return False
        restart_requested.set()

        def shutdown_after_response() -> None:
            time.sleep(0.35)
            if server is not None:
                server.shutdown()

        threading.Thread(target=shutdown_after_response, daemon=True).start()
        return True

    def handler(*handler_args, **handler_kwargs):
        return LibraryHandler(
            *handler_args,
            db_path=db_path,
            cookie_file=Path(args.cookies),
            video_thumbs=Path(args.video_thumbs),
            takeout_dir=Path(args.takeout),
            config_data=args.config_data,
            plugin_manager=plugin_manager,
            service_started_at=service_started_at,
            restart_pending=restart_pending,
            request_restart=request_restart,
            directory=str(ROOT),
            **handler_kwargs,
        )

    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    UPDATE_SCHEDULER.start(
        db_path,
        Path(args.cookies),
        Path(args.video_thumbs),
        args.config_data,
    )
    print(f"Serving http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        UPDATE_SCHEDULER.stop()
        plugin_manager.shutdown()
        server.server_close()
    if restart_requested.is_set():
        print("Restarting service")
        launch_service_replacement()
