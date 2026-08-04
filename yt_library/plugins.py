"""Versioned optional plugin discovery and request dispatch."""

from __future__ import annotations

from collections.abc import Collection, Iterable as IterableCollection, Mapping
import importlib.metadata as importlib_metadata
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .database import connect
from .time_utils import utc_now


PLUGIN_API_VERSION = 2
PLUGIN_ENTRY_POINT_GROUP = "yt_library.plugins"
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
PLUGIN_PROCESS_ID = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
PLUGIN_BROWSER_ASSET_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
PLUGIN_BROWSER_ASSET_TYPES = {"script", "style"}
PLUGIN_PROCESS_SERVICES = {"local", "youtube", "archivarix"}
PLUGIN_ADMIN_SURFACES = {"none", "basic", "advanced"}
PLUGIN_TASK_LIMIT = 250_000
PLUGIN_TASK_PAYLOAD_BYTES = 64 * 1024


@dataclass(frozen=True)
class PluginContext:
    """Narrow host services made available to an activated plugin."""

    root: Path
    config_path: Path
    plugin_id: str
    plugin_config: dict[str, Any]

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.config_path.resolve().parent / path


class PluginPlanningContext:
    """Bounded read-only library information available while planning tasks."""

    def __init__(self, conn: sqlite3.Connection, plugin_id: str) -> None:
        self._conn = conn
        self.plugin_id = plugin_id

    def library_videos(self) -> Iterable[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT video_id, title, availability, is_playable
            FROM videos
            WHERE video_id <> ''
            ORDER BY video_id
            """
        )
        for row in rows:
            yield dict(row)

    def latest_worker_outcomes(self, worker_id: str) -> dict[str, dict[str, Any]]:
        normalized_worker_id = str(worker_id or "").strip()
        if not PLUGIN_PROCESS_ID.fullmatch(normalized_worker_id):
            raise ValueError(f"Invalid plugin worker ID: {worker_id}")
        return {
            str(row["subject_id"]): dict(row)
            for row in self._conn.execute(
                """
                SELECT subject_id, outcome, status, finished_at, message
                FROM (
                  SELECT subject_id, outcome, status, finished_at, message,
                         ROW_NUMBER() OVER (
                           PARTITION BY subject_id
                           ORDER BY started_at DESC, run_id DESC
                         ) AS rank
                  FROM plugin_worker_runs
                  WHERE plugin_id = ? AND worker_id = ? AND subject_id <> ''
                )
                WHERE rank = 1
                """,
                (self.plugin_id, normalized_worker_id),
            )
        }


class PluginWorkerRuntime:
    """Host-owned logging and cancellation surface for one plugin task."""

    def __init__(
        self,
        db_path: Path,
        *,
        run_id: str,
        queue_id: int,
        plugin_id: str,
        worker_id: str,
        subject_id: str,
        stop_event: threading.Event,
    ) -> None:
        self.run_id = run_id
        self.queue_id = queue_id
        self.plugin_id = plugin_id
        self.worker_id = worker_id
        self.subject_id = subject_id
        self._db_path = db_path
        self._stop_event = stop_event

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def log(self, level: str, message: str, *, subject_id: str = "") -> None:
        normalized_level = str(level or "info").strip().lower()[:40] or "info"
        normalized_message = str(message or "").strip()[:10_000]
        normalized_subject = str(subject_id or self.subject_id).strip()[:500]
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO plugin_worker_log(
                      run_id, plugin_id, worker_id, created_at, level,
                      subject_id, message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.run_id,
                        self.plugin_id,
                        self.worker_id,
                        utc_now(),
                        normalized_level,
                        normalized_subject,
                        normalized_message,
                    ),
                )
        finally:
            conn.close()


@dataclass
class _PluginRecord:
    plugin_id: str
    configured: dict[str, Any]
    instance: Any | None = None
    state: str = "configured"
    message: str = ""


def _installed_entry_points() -> list[Any]:
    discovered = importlib_metadata.entry_points()
    if hasattr(discovered, "select"):
        return list(discovered.select(group=PLUGIN_ENTRY_POINT_GROUP))
    return list(discovered.get(PLUGIN_ENTRY_POINT_GROUP, ()))


def _browser_assets(instance: Any) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    for value in getattr(instance, "browser_assets", ()):
        if not isinstance(value, dict):
            raise TypeError("Plugin browser assets must be objects")
        path = str(value.get("path") or "")
        asset_type = str(value.get("type") or "")
        if (
            not PLUGIN_BROWSER_ASSET_PATH.fullmatch(path)
            or ".." in path.split("/")
            or asset_type not in PLUGIN_BROWSER_ASSET_TYPES
        ):
            raise ValueError(f"Invalid plugin browser asset: {path or '<missing>'}")
        assets.append({"path": path, "type": asset_type})
    if assets and not callable(getattr(instance, "handle_browser_asset", None)):
        raise TypeError("Plugin browser assets require handle_browser_asset")
    return assets


def _asset_error(message: str) -> bytes:
    return json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")


def _normalized_video_ids(value: Any, label: str) -> frozenset[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, IterableCollection):
        raise TypeError(f"Plugin {label} must be an iterable of video IDs")
    return frozenset(
        video_id
        for item in value
        if (video_id := str(item).strip())
    )


def _normalized_video_projections(
    value: Any,
    requested_video_ids: Collection[str],
) -> dict[str, dict[str, str]]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(
        value, IterableCollection
    ):
        raise TypeError("Plugin video projections must be an iterable of objects")
    requested = frozenset(requested_video_ids)
    projections: dict[str, dict[str, str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("Plugin video projections must be objects")
        video_id = str(item.get("video_id") or "").strip()
        if not video_id:
            raise ValueError("Plugin video projections require video_id")
        if video_id not in requested:
            raise ValueError("Plugin returned an unrequested video projection")
        if video_id in projections:
            raise ValueError("Plugin returned duplicate video projections")
        projections[video_id] = {
            "video_id": video_id,
            "title": str(item.get("title") or "").strip(),
        }
    return projections


def _short_text(value: Any, *, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _worker_processes(instance: Any) -> list[dict[str, Any]]:
    raw_processes = getattr(instance, "worker_processes", ())
    if callable(raw_processes):
        raw_processes = raw_processes()
    if isinstance(raw_processes, (str, bytes, Mapping)) or not isinstance(
        raw_processes, IterableCollection
    ):
        raise TypeError("Plugin worker_processes must be an iterable of objects")
    processes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_processes:
        if not isinstance(raw, Mapping):
            raise TypeError("Plugin worker process definitions must be objects")
        worker_id = _short_text(raw.get("id"), maximum=80)
        if not PLUGIN_PROCESS_ID.fullmatch(worker_id):
            raise ValueError(f"Invalid plugin worker process ID: {worker_id or '<missing>'}")
        if worker_id in seen:
            raise ValueError(f"Duplicate plugin worker process ID: {worker_id}")
        seen.add(worker_id)
        name = _short_text(raw.get("name"), maximum=120)
        if not name:
            raise ValueError(f"Plugin worker process {worker_id} requires a name")
        service = _short_text(raw.get("service") or "local", maximum=40).lower()
        if service not in PLUGIN_PROCESS_SERVICES:
            raise ValueError(f"Invalid service for plugin worker process {worker_id}: {service}")
        surface = _short_text(
            raw.get("admin_surface") or raw.get("adminSurface") or "none",
            maximum=20,
        ).lower()
        if surface not in PLUGIN_ADMIN_SURFACES:
            raise ValueError(
                f"Invalid admin surface for plugin worker process {worker_id}: {surface}"
            )
        try:
            max_in_flight = int(raw.get("max_in_flight", raw.get("maxInFlight", 1)))
        except (TypeError, ValueError):
            max_in_flight = 1
        hooks_value = raw.get("hooks") or ()
        if isinstance(hooks_value, (str, bytes)):
            hooks_value = (hooks_value,)
        if not isinstance(hooks_value, IterableCollection):
            raise TypeError(f"Plugin worker process {worker_id} hooks must be iterable")
        hooks: list[str] = []
        for value in hooks_value:
            hook = _short_text(value, maximum=80)
            if not PLUGIN_PROCESS_ID.fullmatch(hook):
                raise ValueError(f"Invalid plugin lifecycle hook: {hook or '<missing>'}")
            if hook not in hooks:
                hooks.append(hook)
        processes.append(
            {
                "id": worker_id,
                "name": name,
                "description": _short_text(raw.get("description"), maximum=500),
                "service": service,
                "maxInFlight": max(1, min(100, max_in_flight)),
                "adminSurface": surface,
                "buttonLabel": _short_text(
                    raw.get("button_label") or raw.get("buttonLabel") or name,
                    maximum=120,
                ),
                "confirm": _short_text(raw.get("confirm"), maximum=500),
                "hooks": hooks,
            }
        )
    if processes:
        if not callable(getattr(instance, "plan_worker", None)):
            raise TypeError("Plugin worker processes require plan_worker")
        if not callable(getattr(instance, "run_worker", None)):
            raise TypeError("Plugin worker processes require run_worker")
    return processes


def _normalize_plugin_task(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("Plugin worker tasks must be objects")
    task_id = _short_text(raw.get("task_id") or raw.get("taskId"), maximum=500)
    if not task_id:
        raise ValueError("Plugin worker tasks require task_id")
    subject_id = _short_text(raw.get("subject_id") or raw.get("subjectId") or task_id, maximum=500)
    video_id = _short_text(raw.get("video_id") or raw.get("videoId"), maximum=500)
    title = _short_text(raw.get("title"), maximum=2_000)
    payload = raw.get("payload") or {}
    if not isinstance(payload, Mapping):
        raise TypeError("Plugin worker task payload must be an object")
    payload_json = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    if len(payload_json.encode("utf-8")) > PLUGIN_TASK_PAYLOAD_BYTES:
        raise ValueError("Plugin worker task payload is too large")
    try:
        priority = int(raw.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0
    return {
        "task_id": task_id,
        "subject_id": subject_id,
        "video_id": video_id,
        "title": title,
        "payload_json": payload_json,
        "priority": max(-1_000, min(1_000, priority)),
    }


def _normalize_worker_result(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError("Plugin worker result must be an object")
    outcome = _short_text(value.get("outcome") or "complete", maximum=80).lower()
    if not PLUGIN_PROCESS_ID.fullmatch(outcome):
        raise ValueError(f"Invalid plugin worker outcome: {outcome}")

    def count(name: str, default: int = 0) -> int:
        try:
            return max(0, int(value.get(name, default)))
        except (TypeError, ValueError):
            return max(0, default)

    return {
        "outcome": outcome,
        "processed": count("processed", 1),
        "found": count("found"),
        "failed": count("failed"),
        "skipped": count("skipped"),
        "message": _short_text(value.get("message"), maximum=10_000),
    }


class PluginManager:
    """Load only explicitly enabled plugins and contain plugin failures."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        db_path: Path | None = None,
        entry_points: Iterable[Any] | None = None,
    ) -> None:
        self._config = config
        self._db_path = Path(db_path) if db_path is not None else None
        self._records: dict[str, _PluginRecord] = {}
        configured = config.get("plugins")
        if not isinstance(configured, dict):
            return
        enabled = {
            plugin_id: dict(plugin_config)
            for plugin_id, plugin_config in configured.items()
            if (
                isinstance(plugin_id, str)
                and PLUGIN_ID.fullmatch(plugin_id)
                and isinstance(plugin_config, dict)
                and plugin_config.get("enabled") is True
            )
        }
        if not enabled:
            return
        available = list(entry_points) if entry_points is not None else _installed_entry_points()
        by_name: dict[str, list[Any]] = {}
        for entry_point in available:
            by_name.setdefault(str(entry_point.name), []).append(entry_point)
        for plugin_id, plugin_config in enabled.items():
            record = _PluginRecord(plugin_id=plugin_id, configured=plugin_config)
            self._records[plugin_id] = record
            matches = by_name.get(plugin_id, [])
            if not matches:
                record.state = "missing"
                record.message = f"No installed {PLUGIN_ENTRY_POINT_GROUP} entry point named {plugin_id}"
                continue
            if len(matches) > 1:
                record.state = "error"
                record.message = f"Multiple installed entry points are named {plugin_id}"
                continue
            self._load_plugin(record, matches[0])

    def _load_plugin(self, record: _PluginRecord, entry_point: Any) -> None:
        try:
            factory = entry_point.load()
            instance = factory()
            instance_id = str(getattr(instance, "plugin_id", ""))
            api_version = int(getattr(instance, "plugin_api_version", 0))
            if instance_id != record.plugin_id:
                raise ValueError(
                    f"Entry point {record.plugin_id} created plugin {instance_id or '<missing>'}"
                )
            if api_version != PLUGIN_API_VERSION:
                record.state = "incompatible"
                record.message = (
                    f"Plugin API {api_version} is incompatible with host API {PLUGIN_API_VERSION}"
                )
                return
            _browser_assets(instance)
            _worker_processes(instance)
            context = PluginContext(
                root=Path(__file__).resolve().parent.parent,
                config_path=Path(
                    str(self._config.get("_config_path") or "yt_library.config.json")
                ),
                plugin_id=record.plugin_id,
                plugin_config=dict(record.configured),
            )
            instance.start(context)
            record.instance = instance
            record.state = "loaded"
        except Exception as exc:
            record.state = "error"
            record.message = f"{type(exc).__name__}: {exc}"

    def _record_status(self, record: _PluginRecord) -> dict[str, Any]:
        instance = record.instance
        payload: dict[str, Any] = {
            "id": record.plugin_id,
            "enabled": True,
            "state": record.state,
        }
        if record.message:
            payload["message"] = record.message
        if instance is None:
            return payload
        payload.update(
            {
                "name": str(getattr(instance, "plugin_name", record.plugin_id)),
                "version": str(getattr(instance, "plugin_version", "")),
                "apiVersion": int(getattr(instance, "plugin_api_version", 0)),
                "capabilities": sorted(str(value) for value in getattr(instance, "capabilities", ())),
            }
        )
        browser_assets = _browser_assets(instance)
        if browser_assets:
            payload["browserAssets"] = browser_assets
        worker_processes = _worker_processes(instance)
        if worker_processes:
            payload["workerProcesses"] = self._worker_process_statuses(
                record.plugin_id,
                worker_processes,
            )
        try:
            plugin_status = instance.status()
            if not isinstance(plugin_status, dict):
                raise TypeError("Plugin status must be a JSON object")
            payload["pluginStatus"] = plugin_status
            payload["state"] = str(plugin_status.get("state") or "ready")
        except Exception as exc:
            payload["state"] = "error"
            payload["message"] = f"Status failed: {type(exc).__name__}: {exc}"
        return payload

    def statuses(self) -> list[dict[str, Any]]:
        return [self._record_status(self._records[key]) for key in sorted(self._records)]

    def _worker_process_statuses(
        self,
        plugin_id: str,
        processes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self._db_path is None or not self._db_path.exists():
            return [
                {**process, "queuedCount": 0, "runningCount": 0, "latestRun": None}
                for process in processes
            ]
        conn = connect(self._db_path)
        try:
            queued = {
                str(row["task_type"]): int(row["count"] or 0)
                for row in conn.execute(
                    """
                    SELECT task_type, COUNT(*) AS count
                    FROM worker_queue
                    WHERE worker_type = 'plugin' AND source_key = ?
                    GROUP BY task_type
                    """,
                    (plugin_id,),
                )
            }
            running = {
                str(row["worker_id"]): int(row["count"] or 0)
                for row in conn.execute(
                    """
                    SELECT worker_id, COUNT(*) AS count
                    FROM plugin_worker_runs
                    WHERE plugin_id = ? AND status = 'running'
                    GROUP BY worker_id
                    """,
                    (plugin_id,),
                )
            }
            latest = {
                str(row["worker_id"]): dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM (
                      SELECT runs.*,
                             ROW_NUMBER() OVER (
                               PARTITION BY worker_id
                               ORDER BY started_at DESC, run_id DESC
                             ) AS rank
                      FROM plugin_worker_runs runs
                      WHERE plugin_id = ?
                    )
                    WHERE rank = 1
                    """,
                    (plugin_id,),
                )
            }
        finally:
            conn.close()
        return [
            {
                **process,
                "queuedCount": queued.get(process["id"], 0),
                "runningCount": running.get(process["id"], 0),
                "latestRun": latest.get(process["id"]),
            }
            for process in processes
        ]

    def process_definition(self, plugin_id: str, worker_id: str) -> dict[str, Any] | None:
        record = self._records.get(plugin_id)
        if record is None or record.instance is None:
            return None
        return next(
            (
                process
                for process in _worker_processes(record.instance)
                if process["id"] == worker_id
            ),
            None,
        )

    def process_definitions(self) -> dict[tuple[str, str], dict[str, Any]]:
        definitions: dict[tuple[str, str], dict[str, Any]] = {}
        for plugin_id, record in self._records.items():
            if record.instance is None:
                continue
            for process in _worker_processes(record.instance):
                definitions[(plugin_id, process["id"])] = process
        return definitions

    def enqueue_process(
        self,
        conn: sqlite3.Connection,
        plugin_id: str,
        worker_id: str,
        params: Mapping[str, Any] | None = None,
        *,
        manual: bool,
    ) -> dict[str, Any]:
        record = self._records.get(plugin_id)
        if record is None:
            raise LookupError(f"Plugin is not enabled: {plugin_id}")
        if record.instance is None:
            raise RuntimeError(f"Plugin is unavailable: {plugin_id}")
        process = self.process_definition(plugin_id, worker_id)
        if process is None:
            raise LookupError(f"Unknown plugin worker process: {plugin_id}/{worker_id}")
        try:
            planned = record.instance.plan_worker(
                worker_id,
                PluginPlanningContext(conn, plugin_id),
                dict(params or {}),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Plugin worker planning failed: {plugin_id}/{worker_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if isinstance(planned, (str, bytes, Mapping)) or not isinstance(
            planned, IterableCollection
        ):
            raise TypeError("Plugin worker plan must be an iterable of task objects")
        now = utc_now()
        inserted = 0
        already_queued = 0
        planned_count = 0
        for raw_task in planned:
            planned_count += 1
            if planned_count > PLUGIN_TASK_LIMIT:
                raise ValueError(
                    f"Plugin worker plan exceeds the {PLUGIN_TASK_LIMIT} task limit"
                )
            task = _normalize_plugin_task(raw_task)
            subject_key = (
                f"plugin:{plugin_id}:{worker_id}:{task['task_id']}"
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO worker_queue(
                  subject_key, worker_type, task_type, video_id, current_title,
                  source_key, priority, manual, plugin_subject_id, payload_json,
                  created_at, updated_at
                )
                VALUES (?, 'plugin', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subject_key,
                    worker_id,
                    task["video_id"],
                    task["title"],
                    plugin_id,
                    task["priority"],
                    1 if manual else 0,
                    task["subject_id"],
                    task["payload_json"],
                    now,
                    now,
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                already_queued += 1
                conn.execute(
                    """
                    UPDATE worker_queue
                    SET priority = MIN(priority, ?),
                        manual = MAX(manual, ?),
                        current_title = CASE
                          WHEN current_title = '' THEN ? ELSE current_title
                        END,
                        plugin_subject_id = ?,
                        payload_json = ?,
                        updated_at = ?
                    WHERE subject_key = ?
                    """,
                    (
                        task["priority"],
                        1 if manual else 0,
                        task["title"],
                        task["subject_id"],
                        task["payload_json"],
                        now,
                        subject_key,
                    ),
                )
        conn.execute(
            """
            INSERT INTO plugin_worker_log(
              run_id, plugin_id, worker_id, created_at, level, subject_id, message
            )
            VALUES ('', ?, ?, ?, 'queue info', '', ?)
            """,
            (
                plugin_id,
                worker_id,
                now,
                (
                    f"Queued {inserted} {process['name']} tasks; "
                    f"{already_queued} already queued"
                ),
            ),
        )
        return {
            "pluginId": plugin_id,
            "workerId": worker_id,
            "name": process["name"],
            "planned": planned_count,
            "inserted": inserted,
            "alreadyQueued": already_queued,
        }

    def enqueue_hook(
        self,
        conn: sqlite3.Connection,
        hook: str,
    ) -> list[dict[str, Any]]:
        normalized_hook = str(hook or "").strip()
        results: list[dict[str, Any]] = []
        for (plugin_id, worker_id), process in self.process_definitions().items():
            if normalized_hook not in process["hooks"]:
                continue
            results.append(
                self.enqueue_process(
                    conn,
                    plugin_id,
                    worker_id,
                    {"hook": normalized_hook},
                    manual=False,
                )
            )
        return results

    def run_worker(
        self,
        plugin_id: str,
        worker_id: str,
        task: Mapping[str, Any],
        runtime: PluginWorkerRuntime,
    ) -> dict[str, Any]:
        record = self._records.get(plugin_id)
        if record is None or record.instance is None:
            raise RuntimeError(f"Plugin worker is unavailable: {plugin_id}/{worker_id}")
        if self.process_definition(plugin_id, worker_id) is None:
            raise LookupError(f"Unknown plugin worker process: {plugin_id}/{worker_id}")
        try:
            return _normalize_worker_result(
                record.instance.run_worker(worker_id, dict(task), runtime)
            )
        except Exception as exc:
            raise RuntimeError(
                f"Plugin worker failed: {plugin_id}/{worker_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def handle_api(
        self,
        plugin_id: str,
        method: str,
        path: str,
        query: dict[str, list[str]],
    ) -> tuple[int, Any]:
        record = self._records.get(plugin_id)
        if record is None:
            return 404, {"error": f"Plugin is not enabled: {plugin_id}"}
        if record.instance is None:
            return 503, {
                "error": f"Plugin is unavailable: {plugin_id}",
                "plugin": self._record_status(record),
            }
        try:
            response = record.instance.handle_api(method, path, query)
            if response is None:
                return 404, {"error": f"Unknown plugin route: {plugin_id}/{path}"}
            status, payload = response
            status = int(status)
            if status < 100 or status > 599:
                raise ValueError(f"Plugin returned invalid HTTP status {status}")
            return status, payload
        except Exception as exc:
            return 503, {
                "error": f"Plugin request failed: {plugin_id}",
                "message": f"{type(exc).__name__}: {exc}",
            }

    def handle_browser_asset(self, plugin_id: str, path: str) -> tuple[int, str, bytes]:
        record = self._records.get(plugin_id)
        if record is None:
            return 404, "application/json; charset=utf-8", _asset_error(
                f"Plugin is not enabled: {plugin_id}"
            )
        if record.instance is None:
            return 503, "application/json; charset=utf-8", _asset_error(
                f"Plugin is unavailable: {plugin_id}"
            )
        assets = {asset["path"]: asset for asset in _browser_assets(record.instance)}
        if path not in assets:
            return 404, "application/json; charset=utf-8", _asset_error(
                f"Unknown plugin browser asset: {plugin_id}/{path}"
            )
        try:
            response = record.instance.handle_browser_asset(path)
            if not isinstance(response, tuple) or len(response) != 2:
                raise TypeError("Plugin browser asset response must be (content_type, body)")
            content_type, body = response
            if isinstance(body, str):
                body = body.encode("utf-8")
            if not isinstance(body, bytes):
                raise TypeError("Plugin browser asset body must be bytes or text")
            return 200, str(content_type), body
        except Exception as exc:
            return 503, "application/json; charset=utf-8", _asset_error(
                f"Plugin browser asset failed: {type(exc).__name__}: {exc}"
            )

    def filter_videos(
        self,
        plugin_id: str,
        query: str,
    ) -> tuple[frozenset[str], frozenset[str]]:
        record = self._records.get(plugin_id)
        if record is None:
            raise LookupError(f"Plugin is not enabled: {plugin_id}")
        if record.instance is None:
            raise RuntimeError(f"Plugin is unavailable: {plugin_id}")
        handler = getattr(record.instance, "filter_videos", None)
        if not callable(handler):
            raise TypeError(f"Plugin does not provide a video filter: {plugin_id}")
        try:
            payload = handler(query)
        except Exception as exc:
            raise RuntimeError(
                f"Plugin video filter failed: {plugin_id}: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise TypeError("Plugin video filter response must be a mapping")
        video_ids = _normalized_video_ids(payload.get("video_ids"), "video_ids")
        search_match_ids = _normalized_video_ids(
            payload.get("search_match_ids", ()),
            "search_match_ids",
        )
        if not search_match_ids.issubset(video_ids):
            raise ValueError("Plugin search matches must be included in its video filter")
        return video_ids, search_match_ids

    def project_videos(
        self,
        plugin_id: str,
        video_ids: Collection[str],
    ) -> dict[str, dict[str, str]]:
        record = self._records.get(plugin_id)
        if record is None:
            raise LookupError(f"Plugin is not enabled: {plugin_id}")
        if record.instance is None:
            raise RuntimeError(f"Plugin is unavailable: {plugin_id}")
        requested_video_ids = frozenset(
            video_id
            for value in video_ids
            if (video_id := str(value).strip())
        )
        if not requested_video_ids:
            return {}
        handler = getattr(record.instance, "project_videos", None)
        if not callable(handler):
            return {}
        try:
            payload = handler(requested_video_ids)
        except Exception as exc:
            raise RuntimeError(
                f"Plugin video projection failed: {plugin_id}: {type(exc).__name__}: {exc}"
            ) from exc
        return _normalized_video_projections(payload, requested_video_ids)

    def projected_video(self, video_id: str) -> dict[str, Any] | None:
        video_id = str(video_id).strip()
        if not video_id:
            return None
        projection: dict[str, Any] | None = None
        plugin_ids: list[str] = []
        for plugin_id in sorted(self._records):
            record = self._records[plugin_id]
            if record.instance is None:
                continue
            try:
                candidate = self.project_videos(plugin_id, {video_id}).get(video_id)
            except (RuntimeError, TypeError, ValueError):
                continue
            if candidate is None:
                continue
            plugin_ids.append(plugin_id)
            if projection is None or (not projection["title"] and candidate["title"]):
                projection = dict(candidate)
        if projection is None:
            return None
        projection["projection_plugin_ids"] = plugin_ids
        return projection

    def shutdown(self) -> None:
        for record in reversed(list(self._records.values())):
            if record.instance is None:
                continue
            try:
                record.instance.shutdown()
            except Exception:
                pass
            record.instance = None


class PluginTaskWorker:
    """Run one queued plugin task with host-owned lifecycle persistence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_id = ""

    def is_alive(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def is_running(self) -> bool:
        return self.is_alive() and not self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()

    def start(
        self,
        db_path: Path,
        manager: PluginManager,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {
                    "started": False,
                    "run_id": self._run_id,
                    "message": "Plugin worker task already running",
                }
            self._stop.clear()
            self._run_id = uuid.uuid4().hex
            self._thread = threading.Thread(
                target=self._run,
                args=(Path(db_path), manager, dict(row)),
                name=f"plugin-worker-{row.get('source_key', '')}-{row.get('task_type', '')}",
                daemon=True,
            )
            self._thread.start()
            return {
                "started": True,
                "run_id": self._run_id,
                "message": "Plugin worker task started",
            }

    def _run(
        self,
        db_path: Path,
        manager: PluginManager,
        row: dict[str, Any],
    ) -> None:
        run_id = self._run_id
        queue_id = int(row.get("queue_id") or 0)
        plugin_id = str(row.get("source_key") or "").strip()
        worker_id = str(row.get("task_type") or "").strip()
        subject_id = str(
            row.get("plugin_subject_id") or row.get("video_id") or ""
        ).strip()
        if not subject_id:
            subject_key = str(row.get("subject_key") or "")
            subject_id = subject_key.rsplit(":", 1)[-1]
        started_at = utc_now()
        conn = connect(db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO plugin_worker_runs(
                      run_id, plugin_id, worker_id, status, started_at,
                      queue_id, subject_id, message
                    )
                    VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        plugin_id,
                        worker_id,
                        started_at,
                        queue_id,
                        subject_id,
                        "Plugin worker task started",
                    ),
                )
        finally:
            conn.close()
        runtime = PluginWorkerRuntime(
            db_path,
            run_id=run_id,
            queue_id=queue_id,
            plugin_id=plugin_id,
            worker_id=worker_id,
            subject_id=subject_id,
            stop_event=self._stop,
        )
        status = "complete"
        result = {
            "outcome": "complete",
            "processed": 0,
            "found": 0,
            "failed": 0,
            "skipped": 0,
            "message": "",
        }
        try:
            try:
                payload = json.loads(str(row.get("payload_json") or "{}"))
            except json.JSONDecodeError as exc:
                raise ValueError("Queued plugin task payload is invalid JSON") from exc
            task = {
                "queue_id": queue_id,
                "subject_id": subject_id,
                "video_id": str(row.get("video_id") or ""),
                "title": str(row.get("current_title") or ""),
                "payload": payload,
            }
            result = manager.run_worker(plugin_id, worker_id, task, runtime)
            if self._stop.is_set():
                status = "interrupted"
                result["message"] = result["message"] or "Interrupted by stop request"
            else:
                conn = connect(db_path)
                try:
                    with conn:
                        conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (queue_id,))
                finally:
                    conn.close()
        except Exception as exc:
            status = "error"
            result = {
                "outcome": "worker_error",
                "processed": 1,
                "found": 0,
                "failed": 1,
                "skipped": 0,
                "message": f"{type(exc).__name__}: {exc}",
            }
            runtime.log("error", result["message"])
            conn = connect(db_path)
            try:
                with conn:
                    conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (queue_id,))
            finally:
                conn.close()
        finished_at = utc_now()
        conn = connect(db_path)
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE plugin_worker_runs
                    SET status = ?, finished_at = ?, outcome = ?, processed = ?,
                        found = ?, failed = ?, skipped = ?, message = ?
                    WHERE run_id = ?
                    """,
                    (
                        status,
                        finished_at,
                        result["outcome"],
                        result["processed"],
                        result["found"],
                        result["failed"],
                        result["skipped"],
                        result["message"],
                        run_id,
                    ),
                )
        finally:
            conn.close()
