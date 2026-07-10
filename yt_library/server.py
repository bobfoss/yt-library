"""HTTP server and request routing for YT Library Manager."""

from __future__ import annotations

import argparse
import http.server
import json
import posixpath
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from .core import *
from .queries import fetch_app_data, history_search_data
from .templates import load_template
from .workers import (
    LIVE_HISTORY_WORKER,
    METADATA_WORKER,
    PLAYLIST_SCAN_WORKER,
    WORKER_QUEUE_DISPATCHER,
)


INDEX_HTML = load_template("index.html")
HISTORY_HTML = load_template("history.html")
ADMIN_HTML = load_template("admin.html")

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
        if parsed.path == "/api/admin/status":
            params = urllib.parse.parse_qs(parsed.query)
            include_logs = (params.get("include_logs") or ["1"])[0].strip().lower() not in {"0", "false", "no"}
            try:
                worker_queue_limit = max(0, min(10000, int((params.get("queue_limit") or ["0"])[0] or 0)))
            except ValueError:
                worker_queue_limit = 0
            self.send_json(
                admin_status(
                    self.db_path,
                    METADATA_WORKER,
                    PLAYLIST_SCAN_WORKER,
                    LIVE_HISTORY_WORKER,
                    WORKER_QUEUE_DISPATCHER,
                    include_logs,
                    worker_queue_limit,
                )
            )
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

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/admin/metadata/start":
            stale_days = max(0, int((params.get("stale_days") or ["30"])[0] or 30))
            force = (params.get("force") or ["0"])[0] in {"1", "true", "yes"}
            conn = connect(self.db_path)
            try:
                with conn:
                    if metadata_queue_count(conn, force=False, stale_days=stale_days) == 0:
                        queue_stats = rebuild_metadata_queue(conn, force=force, stale_days=stale_days)
                    else:
                        queue_stats = {
                            "cleared": 0,
                            "inserted": 0,
                            "queued": metadata_queue_count(conn, force=False, stale_days=stale_days),
                        }
            finally:
                conn.close()
            dispatcher = WORKER_QUEUE_DISPATCHER.start(self.db_path, self.cookie_file, self.video_thumbs)
            self.send_json({"queue": queue_stats, "dispatcher": dispatcher})
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
                self.send_json({"ok": True, "cleared": cleared, "metadata": metadata, "playlists": playlists, "history": 1})
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
            dispatcher = WORKER_QUEUE_DISPATCHER.start(self.db_path, self.cookie_file, self.video_thumbs)
            self.send_json({"ok": True, "dispatcher": dispatcher})
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
            conn = connect(self.db_path)
            try:
                with conn:
                    if playlist_scan_queue_count(conn) == 0:
                        queue_stats = rebuild_playlist_scan_queue(conn, force=True, stale_days=7)
                    else:
                        queue_stats = {
                            "cleared": 0,
                            "inserted": 0,
                            "queued": playlist_scan_queue_count(conn),
                        }
            finally:
                conn.close()
            dispatcher = WORKER_QUEUE_DISPATCHER.start(self.db_path, self.cookie_file, self.video_thumbs)
            self.send_json({"queue": queue_stats, "dispatcher": dispatcher})
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
            conn = connect(self.db_path)
            try:
                with conn:
                    enqueue_history_task(conn, "recent", priority=0, manual=True)
            finally:
                conn.close()
            dispatcher = WORKER_QUEUE_DISPATCHER.start(self.db_path, self.cookie_file, self.video_thumbs)
            self.send_json({"dispatcher": dispatcher})
            return
        if parsed.path == "/api/admin/live-history/verify":
            conn = connect(self.db_path)
            try:
                with conn:
                    enqueue_history_task(conn, "verify", priority=0, manual=True)
            finally:
                conn.close()
            dispatcher = WORKER_QUEUE_DISPATCHER.start(self.db_path, self.cookie_file, self.video_thumbs)
            self.send_json({"dispatcher": dispatcher})
            return
        if parsed.path == "/api/admin/live-history/stop":
            self.send_json(WORKER_QUEUE_DISPATCHER.stop())
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
        raise SystemExit(f"Database not found: {db_path}. Run migrate or import first.")
    conn = connect(db_path)
    try:
        conn.execute("SELECT 1 FROM playlists LIMIT 1")
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"Database schema is not initialized. Run migrate first: {exc}") from exc
    finally:
        conn.close()
    reconcile_worker_runs(db_path, METADATA_WORKER, PLAYLIST_SCAN_WORKER, LIVE_HISTORY_WORKER)

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
