from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import re

from yt_library.worker_runs import WorkerRunRecorder

from tests.support import migrated_connection


class WorkerRunRecorderTests(unittest.TestCase):
    def test_runtime_modules_do_not_write_worker_run_tables_directly(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        run_tables = (
            "metadata_worker_runs",
            "playlist_scan_worker_runs",
            "live_history_worker_runs",
            "placeholder_recovery_worker_runs",
            "plugin_worker_runs",
        )
        direct_write = re.compile(
            rf"(?:INSERT\s+INTO|UPDATE)\s+(?:{'|'.join(run_tables)})",
            re.IGNORECASE,
        )
        for relative_path in (
            "yt_library/core.py",
            "yt_library/plugins.py",
            "yt_library/server.py",
            "yt_library/workers.py",
        ):
            source = (project_root / relative_path).read_text(encoding="utf-8")
            with self.subTest(relative_path=relative_path):
                self.assertIsNone(direct_write.search(source))

    def test_typed_recorders_start_update_and_finish_each_run_family(self) -> None:
        starts = {
            "metadata": {"total": 4, "requested_limit": 4},
            "playlist": {"total": 3, "force": 1},
            "history": {"requested_limit": 100},
            "placeholder": {"queue_id": 7},
            "plugin": {"plugin_id": "example", "worker_id": "scan", "queue_id": 8},
        }
        tables = {
            "metadata": "metadata_worker_runs",
            "playlist": "playlist_scan_worker_runs",
            "history": "live_history_worker_runs",
            "placeholder": "placeholder_recovery_worker_runs",
            "plugin": "plugin_worker_runs",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    for kind, fields in starts.items():
                        recorder = WorkerRunRecorder(conn, kind)
                        recorder.start(
                            f"{kind}-run",
                            message=f"{kind} started",
                            started_at="2026-08-06T01:00:00Z",
                            **fields,
                        )
                        recorder.update(f"{kind}-run", processed=1, found=1)
                        recorder.finish(
                            f"{kind}-run",
                            status="complete",
                            message=f"{kind} complete",
                            finished_at="2026-08-06T01:05:00Z",
                            processed=2,
                            found=2,
                        )
                for kind, table in tables.items():
                    row = conn.execute(
                        f"SELECT status, started_at, finished_at, processed, found, message "
                        f"FROM {table} WHERE run_id = ?",
                        (f"{kind}-run",),
                    ).fetchone()
                    self.assertEqual(
                        tuple(row),
                        (
                            "complete",
                            "2026-08-06T01:00:00Z",
                            "2026-08-06T01:05:00Z",
                            2,
                            2,
                            f"{kind} complete",
                        ),
                    )
            finally:
                conn.close()

    def test_updates_support_validated_atomic_increments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                recorder = WorkerRunRecorder(conn, "placeholder")
                with conn:
                    recorder.start("placeholder-run", message="started")
                    recorder.update(
                        "placeholder-run",
                        request_started_at="2026-08-06T01:01:00Z",
                        increments={"request_count": 1},
                    )
                row = conn.execute(
                    "SELECT request_started_at, request_count "
                    "FROM placeholder_recovery_worker_runs WHERE run_id = ?",
                    ("placeholder-run",),
                ).fetchone()
                self.assertEqual(tuple(row), ("2026-08-06T01:01:00Z", 1))
            finally:
                conn.close()

    def test_invalid_fields_statuses_and_plugin_starts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                metadata = WorkerRunRecorder(conn, "metadata")
                with self.assertRaisesRegex(ValueError, "Unsupported metadata"):
                    metadata.start("bad-field", message="bad", queue_id=1)
                with self.assertRaisesRegex(ValueError, "always start"):
                    metadata.start("bad-status", message="bad", status="complete")
                with self.assertRaisesRegex(ValueError, "Unsupported terminal"):
                    metadata.finish("missing", status="running", message="bad")
                with self.assertRaisesRegex(ValueError, "require a transition"):
                    metadata.update("missing", status="complete")
                with self.assertRaisesRegex(ValueError, "Missing plugin"):
                    WorkerRunRecorder(conn, "plugin").start("missing-plugin", message="bad")
            finally:
                conn.close()

    def test_recorders_leave_commit_and_rollback_to_the_caller(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                recorder = WorkerRunRecorder(conn, "metadata")
                with self.assertRaisesRegex(RuntimeError, "rollback"):
                    with conn:
                        recorder.start("rolled-back", message="started")
                        raise RuntimeError("rollback")
                count = conn.execute(
                    "SELECT COUNT(*) FROM metadata_worker_runs WHERE run_id = 'rolled-back'"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.close()

    def test_interrupt_running_preserves_terminal_rows_and_appends_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                recorder = WorkerRunRecorder(conn, "history")
                with conn:
                    recorder.start(
                        "running-with-message",
                        message="Fetching history",
                        started_at="2026-08-06T01:00:00Z",
                    )
                    recorder.start(
                        "running-empty-message",
                        message="",
                        started_at="2026-08-06T01:00:00Z",
                    )
                    recorder.start(
                        "already-complete",
                        message="Started",
                        started_at="2026-08-06T01:00:00Z",
                    )
                    recorder.finish(
                        "already-complete",
                        status="complete",
                        message="Complete",
                        finished_at="2026-08-06T01:02:00Z",
                    )
                    recorder.interrupt_running(finished_at="2026-08-06T01:03:00Z")
                rows = {
                    row[0]: tuple(row[1:])
                    for row in conn.execute(
                        "SELECT run_id, status, finished_at, message "
                        "FROM live_history_worker_runs ORDER BY run_id"
                    )
                }
                self.assertEqual(
                    rows["running-with-message"],
                    (
                        "interrupted",
                        "2026-08-06T01:03:00Z",
                        "Fetching history (interrupted by server restart)",
                    ),
                )
                self.assertEqual(
                    rows["running-empty-message"],
                    ("interrupted", "2026-08-06T01:03:00Z", "Interrupted by server restart"),
                )
                self.assertEqual(
                    rows["already-complete"],
                    ("complete", "2026-08-06T01:02:00Z", "Complete"),
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
