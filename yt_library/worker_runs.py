"""Typed persistence helpers for background worker run records."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .time_utils import utc_now


WorkerRunKind = Literal["metadata", "playlist", "history", "placeholder", "plugin"]
TERMINAL_WORKER_RUN_STATUSES = frozenset(
    {"complete", "stopped", "error", "blocked", "interrupted"}
)
_MANAGED_TRANSITION_FIELDS = frozenset({"status", "started_at", "finished_at"})


@dataclass(frozen=True)
class WorkerRunSpec:
    table: str
    fields: frozenset[str]
    required_start_fields: frozenset[str] = frozenset()


_COMMON_FIELDS = {
    "status",
    "started_at",
    "finished_at",
    "total",
    "processed",
    "found",
    "failed",
    "message",
}
WORKER_RUN_SPECS: dict[WorkerRunKind, WorkerRunSpec] = {
    "metadata": WorkerRunSpec(
        "metadata_worker_runs",
        frozenset(
            _COMMON_FIELDS
            | {
                "skipped",
                "delay_seconds",
                "requested_limit",
                "force",
                "stale_days",
                "last_video_id",
            }
        ),
    ),
    "playlist": WorkerRunSpec(
        "playlist_scan_worker_runs",
        frozenset(
            _COMMON_FIELDS
            | {
                "skipped",
                "delay_seconds",
                "requested_limit",
                "force",
                "last_playlist_id",
            }
        ),
    ),
    "history": WorkerRunSpec(
        "live_history_worker_runs",
        frozenset(
            _COMMON_FIELDS
            | {
                "skipped",
                "delay_seconds",
                "requested_limit",
                "last_video_id",
            }
        ),
    ),
    "placeholder": WorkerRunSpec(
        "placeholder_recovery_worker_runs",
        frozenset(
            _COMMON_FIELDS
            | {
                "queue_id",
                "video_id",
                "playlist_id",
                "request_started_at",
                "request_count",
                "recovery_status",
            }
        ),
    ),
    "plugin": WorkerRunSpec(
        "plugin_worker_runs",
        frozenset(
            _COMMON_FIELDS
            | {
                "plugin_id",
                "worker_id",
                "queue_id",
                "subject_id",
                "outcome",
                "skipped",
            }
        ),
        frozenset({"plugin_id", "worker_id"}),
    ),
}


class WorkerRunRecorder:
    """Write one worker run family without owning transaction boundaries."""

    def __init__(self, conn: sqlite3.Connection, kind: WorkerRunKind) -> None:
        self.conn = conn
        self.kind = kind
        self.spec = WORKER_RUN_SPECS[kind]

    def _validated_fields(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(fields) - self.spec.fields
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"Unsupported {self.kind} worker run fields: {names}")
        return dict(fields)

    def start(
        self,
        run_id: str,
        *,
        message: str,
        started_at: str | None = None,
        **fields: Any,
    ) -> None:
        values = self._validated_fields(fields)
        if "status" in values:
            raise ValueError("Worker runs always start with running status")
        if "finished_at" in values:
            raise ValueError("Worker runs cannot be finished when they start")
        missing = self.spec.required_start_fields - set(values)
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"Missing {self.kind} worker start fields: {names}")
        values = {
            "status": "running",
            "started_at": started_at or utc_now(),
            "message": message,
            **values,
        }
        columns = ["run_id", *values]
        placeholders = ", ".join("?" for _column in columns)
        self.conn.execute(
            f"INSERT INTO {self.spec.table} ({', '.join(columns)}) VALUES ({placeholders})",
            (run_id, *values.values()),
        )

    def update(
        self,
        run_id: str,
        *,
        increments: Mapping[str, int] | None = None,
        **fields: Any,
    ) -> None:
        values = self._validated_fields(fields)
        increment_values = self._validated_fields(increments or {})
        managed = _MANAGED_TRANSITION_FIELDS & (set(values) | set(increment_values))
        if managed:
            names = ", ".join(sorted(managed))
            raise ValueError(f"Worker run lifecycle fields require a transition: {names}")
        self._write_update(run_id, values, increment_values)

    def _write_update(
        self,
        run_id: str,
        values: Mapping[str, Any],
        increment_values: Mapping[str, int] | None = None,
    ) -> None:
        increment_values = increment_values or {}
        overlap = set(values) & set(increment_values)
        if overlap:
            names = ", ".join(sorted(overlap))
            raise ValueError(f"Worker run fields cannot be set and incremented together: {names}")
        assignments = [f"{field} = ?" for field in values]
        assignments.extend(f"{field} = {field} + ?" for field in increment_values)
        if not assignments:
            raise ValueError("Worker run update requires at least one field")
        self.conn.execute(
            f"UPDATE {self.spec.table} SET {', '.join(assignments)} WHERE run_id = ?",
            (*values.values(), *increment_values.values(), run_id),
        )

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        message: str,
        finished_at: str | None = None,
        **fields: Any,
    ) -> None:
        if status not in TERMINAL_WORKER_RUN_STATUSES:
            raise ValueError(f"Unsupported terminal worker run status: {status}")
        values = self._validated_fields(fields)
        managed = _MANAGED_TRANSITION_FIELDS & set(values)
        if managed:
            names = ", ".join(sorted(managed))
            raise ValueError(f"Worker run lifecycle fields require a transition: {names}")
        self._write_update(
            run_id,
            {
                "status": status,
                "finished_at": finished_at or utc_now(),
                "message": message,
                **values,
            },
        )

    def interrupt_running(self, *, finished_at: str | None = None) -> None:
        self.conn.execute(
            f"""
            UPDATE {self.spec.table}
            SET status = 'interrupted',
                finished_at = ?,
                message = CASE
                  WHEN message = '' THEN 'Interrupted by server restart'
                  ELSE message || ' (interrupted by server restart)'
                END
            WHERE status = 'running'
            """,
            (finished_at or utc_now(),),
        )
