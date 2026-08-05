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
PLUGIN_ADMIN_PLACEMENTS = {"plugin", "videos"}
PLUGIN_TASK_LIMIT = 250_000
PLUGIN_TASK_PAYLOAD_BYTES = 64 * 1024
PLUGIN_NAVIGATION_GROUP_LIMIT = 10_000
PLUGIN_NAVIGATION_MEMBERSHIP_LIMIT = 250_000
PLUGIN_PLAYLIST_GROUP_KEY_PREFIX = "plugin:"
PLUGIN_CHANNEL_GROUP_KEY_PREFIX = "plugin-channel:"


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


def _plugin_navigation_group_key(
    plugin_id: str,
    group_key: str,
    prefix: str,
) -> str:
    return f"{prefix}{plugin_id}:{group_key}"


def _normalized_navigation_group_projection(
    plugin_id: str,
    value: Any,
    *,
    domain: str,
    identifier_field: str,
    key_prefix: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"Plugin {domain}-group projection must be an object")
    raw_groups = value.get("groups")
    if isinstance(raw_groups, (str, bytes, Mapping)) or not isinstance(
        raw_groups, IterableCollection
    ):
        raise TypeError(f"Plugin {domain} groups must be an iterable of objects")
    raw_memberships = value.get("memberships")
    if isinstance(raw_memberships, (str, bytes, Mapping)) or not isinstance(
        raw_memberships, IterableCollection
    ):
        raise TypeError(
            f"Plugin {domain} memberships must be an iterable of objects"
        )

    groups: list[dict[str, Any]] = []
    raw_group_keys: set[str] = set()
    raw_parents: dict[str, str | None] = {}
    unmatched_group_key: str | None = None
    for item in raw_groups:
        if len(groups) >= PLUGIN_NAVIGATION_GROUP_LIMIT:
            raise ValueError(
                "Plugin returned more than "
                f"{PLUGIN_NAVIGATION_GROUP_LIMIT} {domain} groups"
            )
        if not isinstance(item, Mapping):
            raise TypeError(f"Plugin {domain} groups must be objects")
        group_key = str(item.get("group_key") or "").strip()
        name = str(item.get("name") or "").strip()
        parent_value = item.get("parent_key")
        parent_key = str(parent_value or "").strip() or None
        if not group_key or len(group_key) > 500 or any(ord(char) < 32 for char in group_key):
            raise ValueError(f"Plugin {domain} groups require a valid group_key")
        if group_key in raw_group_keys:
            raise ValueError(f"Plugin returned duplicate {domain} group keys")
        if not name or len(name) > 2_000:
            raise ValueError(f"Plugin {domain} groups require a valid name")
        try:
            position = int(item.get("position") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Plugin {domain} group positions must be integers"
            ) from exc
        if position < 0:
            raise ValueError(
                f"Plugin {domain} group positions must be nonnegative"
            )
        include_unmatched = item.get("include_unmatched", False)
        if not isinstance(include_unmatched, bool):
            raise TypeError(
                f"Plugin {domain} group include_unmatched must be a boolean"
            )
        if include_unmatched:
            if unmatched_group_key is not None:
                raise ValueError(
                    f"Plugin returned multiple unmatched {domain} groups"
                )
            unmatched_group_key = group_key
        icon = str(item.get("icon") or "")[:10_000]
        raw_group_keys.add(group_key)
        raw_parents[group_key] = parent_key
        group = {
            "group_key": _plugin_navigation_group_key(
                plugin_id, group_key, key_prefix
            ),
            "name": name,
            "parent_key": (
                _plugin_navigation_group_key(
                    plugin_id, parent_key, key_prefix
                )
                if parent_key
                else None
            ),
            "position": position,
            "icon": icon,
            "source_plugin_id": plugin_id,
        }
        if include_unmatched:
            group["include_unmatched"] = True
        groups.append(group)
    for group_key, parent_key in raw_parents.items():
        if parent_key is not None and parent_key not in raw_group_keys:
            raise ValueError(
                f"Plugin {domain} group {group_key!r} references a missing parent"
            )
        seen: set[str] = set()
        current: str | None = group_key
        while current is not None:
            if current in seen:
                raise ValueError(
                    f"Plugin {domain} group hierarchy contains a cycle"
                )
            seen.add(current)
            current = raw_parents.get(current)

    memberships: list[dict[str, Any]] = []
    seen_memberships: set[tuple[str, str]] = set()
    for item in raw_memberships:
        if len(memberships) >= PLUGIN_NAVIGATION_MEMBERSHIP_LIMIT:
            raise ValueError(
                "Plugin returned more than "
                f"{PLUGIN_NAVIGATION_MEMBERSHIP_LIMIT} {domain} memberships"
            )
        if not isinstance(item, Mapping):
            raise TypeError(f"Plugin {domain} memberships must be objects")
        group_key = str(item.get("group_key") or "").strip()
        identifier = str(item.get(identifier_field) or "").strip()
        if group_key not in raw_group_keys:
            raise ValueError(
                f"Plugin {domain} membership references an unknown group"
            )
        if not identifier or len(identifier) > 500:
            raise ValueError(
                f"Plugin {domain} memberships require {identifier_field}"
            )
        membership_key = (group_key, identifier)
        if membership_key in seen_memberships:
            raise ValueError(f"Plugin returned duplicate {domain} memberships")
        try:
            position = int(item.get("position") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Plugin {domain} membership positions must be integers"
            ) from exc
        if position < 0:
            raise ValueError(
                f"Plugin {domain} membership positions must be nonnegative"
            )
        seen_memberships.add(membership_key)
        memberships.append(
            {
                "group_key": _plugin_navigation_group_key(
                    plugin_id, group_key, key_prefix
                ),
                identifier_field: identifier,
                "position": position,
                "source_plugin_id": plugin_id,
            }
        )
    if unmatched_group_key is not None and any(
        membership[0] == unmatched_group_key for membership in seen_memberships
    ):
        raise ValueError(
            f"Plugin unmatched {domain} group cannot declare memberships"
        )
    return {
        "plugin_id": plugin_id,
        "revision": str(value.get("revision") or "")[:500],
        "groups": groups,
        "memberships": memberships,
    }


def _navigation_memberships_for_known_identifiers(
    projection: Mapping[str, Any],
    known: frozenset[str] | None,
    *,
    identifier_field: str,
) -> list[dict[str, Any]]:
    memberships = [
        dict(membership)
        for membership in projection["memberships"]
        if known is None or membership[identifier_field] in known
    ]
    if known is None:
        return memberships
    unmatched_groups = [
        group for group in projection["groups"] if group.get("include_unmatched") is True
    ]
    if not unmatched_groups:
        return memberships
    assigned = {
        str(membership[identifier_field])
        for membership in projection["memberships"]
        if membership[identifier_field] in known
    }
    unmatched_ids = sorted(known - assigned)
    if len(memberships) + len(unmatched_ids) > PLUGIN_NAVIGATION_MEMBERSHIP_LIMIT:
        raise ValueError(
            "Plugin projection expands beyond the navigation membership limit"
        )
    unmatched_group = unmatched_groups[0]
    memberships.extend(
        {
            "group_key": unmatched_group["group_key"],
            identifier_field: identifier,
            "position": position,
            "source_plugin_id": unmatched_group["source_plugin_id"],
        }
        for position, identifier in enumerate(unmatched_ids)
    )
    return memberships


def _normalized_playlist_group_projection(
    plugin_id: str,
    value: Any,
) -> dict[str, Any]:
    return _normalized_navigation_group_projection(
        plugin_id,
        value,
        domain="playlist",
        identifier_field="playlist_id",
        key_prefix=PLUGIN_PLAYLIST_GROUP_KEY_PREFIX,
    )


def _normalized_channel_group_projection(
    plugin_id: str,
    value: Any,
) -> dict[str, Any]:
    return _normalized_navigation_group_projection(
        plugin_id,
        value,
        domain="channel",
        identifier_field="channel_id",
        key_prefix=PLUGIN_CHANNEL_GROUP_KEY_PREFIX,
    )


def _short_text(value: Any, *, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _plugin_admin_inputs(
    worker_id: str,
    action_id: str,
    raw_inputs: Any,
) -> list[dict[str, Any]]:
    if raw_inputs is None:
        return []
    if isinstance(raw_inputs, (str, bytes, Mapping)) or not isinstance(
        raw_inputs, IterableCollection
    ):
        raise TypeError(
            f"Plugin worker process {worker_id} admin action {action_id} "
            "inputs must be iterable"
        )
    inputs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_input in raw_inputs:
        if not isinstance(raw_input, Mapping):
            raise TypeError("Plugin admin action inputs must be objects")
        name = _short_text(raw_input.get("name"), maximum=80)
        if not PLUGIN_PROCESS_ID.fullmatch(name):
            raise ValueError(f"Invalid plugin admin input name: {name or '<missing>'}")
        if name in seen:
            raise ValueError(f"Duplicate plugin admin input name: {name}")
        seen.add(name)
        label = _short_text(raw_input.get("label") or name, maximum=120)
        try:
            max_length = int(
                raw_input.get("max_length", raw_input.get("maxLength", 500))
            )
        except (TypeError, ValueError):
            max_length = 500
        inputs.append(
            {
                "name": name,
                "label": label,
                "placeholder": _short_text(
                    raw_input.get("placeholder"), maximum=250
                ),
                "required": raw_input.get("required") is True,
                "maxLength": max(1, min(2000, max_length)),
            }
        )
    return inputs


def _plugin_admin_actions(
    worker_id: str,
    raw_process: Mapping[str, Any],
    *,
    process_name: str,
    process_description: str,
    legacy_surface: str,
    legacy_button_label: str,
    legacy_confirm: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    if legacy_surface != "none":
        actions.append(
            {
                "id": "default",
                "placement": "plugin",
                "surface": legacy_surface,
                "buttonLabel": legacy_button_label,
                "description": process_description,
                "confirm": legacy_confirm,
                "inputs": [],
            }
        )
        seen.add("default")
    raw_actions = raw_process.get("admin_actions", raw_process.get("adminActions"))
    if raw_actions is None:
        return actions
    if isinstance(raw_actions, (str, bytes, Mapping)) or not isinstance(
        raw_actions, IterableCollection
    ):
        raise TypeError(f"Plugin worker process {worker_id} admin actions must be iterable")
    for raw_action in raw_actions:
        if not isinstance(raw_action, Mapping):
            raise TypeError("Plugin worker process admin actions must be objects")
        action_id = _short_text(raw_action.get("id"), maximum=80)
        if not PLUGIN_PROCESS_ID.fullmatch(action_id):
            raise ValueError(f"Invalid plugin admin action ID: {action_id or '<missing>'}")
        if action_id in seen:
            raise ValueError(f"Duplicate plugin admin action ID: {action_id}")
        seen.add(action_id)
        placement = _short_text(
            raw_action.get("placement") or "plugin", maximum=20
        ).lower()
        if placement not in PLUGIN_ADMIN_PLACEMENTS:
            raise ValueError(f"Invalid plugin admin placement: {placement}")
        surface = _short_text(
            raw_action.get("surface") or "advanced", maximum=20
        ).lower()
        if surface not in PLUGIN_ADMIN_SURFACES - {"none"}:
            raise ValueError(f"Invalid plugin admin action surface: {surface}")
        actions.append(
            {
                "id": action_id,
                "placement": placement,
                "surface": surface,
                "buttonLabel": _short_text(
                    raw_action.get("button_label")
                    or raw_action.get("buttonLabel")
                    or process_name,
                    maximum=120,
                ),
                "description": _short_text(
                    raw_action.get("description"), maximum=500
                ),
                "confirm": _short_text(raw_action.get("confirm"), maximum=500),
                "inputs": _plugin_admin_inputs(
                    worker_id,
                    action_id,
                    raw_action.get("inputs"),
                ),
            }
        )
    return actions


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
        description = _short_text(raw.get("description"), maximum=500)
        button_label = _short_text(
            raw.get("button_label") or raw.get("buttonLabel") or name,
            maximum=120,
        )
        confirm = _short_text(raw.get("confirm"), maximum=500)
        process = {
            "id": worker_id,
            "name": name,
            "description": description,
            "service": service,
            "maxInFlight": max(1, min(100, max_in_flight)),
            "adminSurface": surface,
            "buttonLabel": button_label,
            "confirm": confirm,
            "hooks": hooks,
        }
        process["adminActions"] = _plugin_admin_actions(
            worker_id,
            raw,
            process_name=name,
            process_description=description,
            legacy_surface=surface,
            legacy_button_label=button_label,
            legacy_confirm=confirm,
        )
        processes.append(process)
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
    """Track configured plugins, load enabled ones, and contain failures."""

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
        configured_plugins = {
            plugin_id: dict(plugin_config)
            for plugin_id, plugin_config in configured.items()
            if (
                isinstance(plugin_id, str)
                and PLUGIN_ID.fullmatch(plugin_id)
                and isinstance(plugin_config, dict)
            )
        }
        if not configured_plugins:
            return
        has_enabled_plugin = any(
            plugin_config.get("enabled") is True
            for plugin_config in configured_plugins.values()
        )
        available = (
            list(entry_points) if entry_points is not None else _installed_entry_points()
        ) if has_enabled_plugin else []
        by_name: dict[str, list[Any]] = {}
        for entry_point in available:
            by_name.setdefault(str(entry_point.name), []).append(entry_point)
        for plugin_id, plugin_config in configured_plugins.items():
            record = _PluginRecord(plugin_id=plugin_id, configured=plugin_config)
            self._records[plugin_id] = record
            if plugin_config.get("enabled") is not True:
                record.state = "disabled"
                record.message = "Plugin is disabled"
                continue
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
            capabilities = {
                str(value) for value in getattr(instance, "capabilities", ())
            }
            if "playlist_groups" in capabilities and not callable(
                getattr(instance, "project_playlist_groups", None)
            ):
                raise TypeError(
                    "Plugin playlist_groups capability requires "
                    "project_playlist_groups"
                )
            if "channel_groups" in capabilities and not callable(
                getattr(instance, "project_channel_groups", None)
            ):
                raise TypeError(
                    "Plugin channel_groups capability requires "
                    "project_channel_groups"
                )
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
            "name": str(record.configured.get("name") or record.plugin_id),
            "enabled": record.configured.get("enabled") is True,
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
        log_subject_id = ""
        for raw_task in planned:
            planned_count += 1
            if planned_count > PLUGIN_TASK_LIMIT:
                raise ValueError(
                    f"Plugin worker plan exceeds the {PLUGIN_TASK_LIMIT} task limit"
                )
            task = _normalize_plugin_task(raw_task)
            if planned_count == 1:
                log_subject_id = task["subject_id"]
            elif planned_count == 2:
                log_subject_id = ""
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
            VALUES ('', ?, ?, ?, 'queue info', ?, ?)
            """,
            (
                plugin_id,
                worker_id,
                now,
                log_subject_id,
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
        params: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_hook = str(hook or "").strip()
        hook_params = dict(params or {})
        hook_params["hook"] = normalized_hook
        results: list[dict[str, Any]] = []
        for index, ((plugin_id, worker_id), process) in enumerate(
            self.process_definitions().items()
        ):
            if normalized_hook not in process["hooks"]:
                continue
            savepoint = f"plugin_hook_{index}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                result = self.enqueue_process(
                    conn,
                    plugin_id,
                    worker_id,
                    hook_params,
                    manual=False,
                )
            except Exception as exc:
                conn.execute(f"ROLLBACK TO {savepoint}")
                conn.execute(f"RELEASE {savepoint}")
                message = f"{type(exc).__name__}: {exc}"
                conn.execute(
                    """
                    INSERT INTO plugin_worker_log(
                      run_id, plugin_id, worker_id, created_at, level,
                      subject_id, message
                    )
                    VALUES ('', ?, ?, ?, 'queue error', '', ?)
                    """,
                    (
                        plugin_id,
                        worker_id,
                        utc_now(),
                        f"{normalized_hook} hook planning failed: {message}",
                    ),
                )
                result = {
                    "pluginId": plugin_id,
                    "workerId": worker_id,
                    "name": process["name"],
                    "planned": 0,
                    "inserted": 0,
                    "alreadyQueued": 0,
                    "error": message,
                }
            else:
                conn.execute(f"RELEASE {savepoint}")
            results.append(result)
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

    def _project_navigation_groups(
        self,
        *,
        capability: str,
        method_name: str,
        identifier_field: str,
        normalizer: Any,
        known_identifiers: Collection[str] | None = None,
    ) -> dict[str, Any]:
        known = (
            None
            if known_identifiers is None
            else frozenset(
                identifier
                for value in known_identifiers
                if (identifier := str(value).strip())
            )
        )
        groups: list[dict[str, Any]] = []
        memberships: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for plugin_id in sorted(self._records):
            record = self._records[plugin_id]
            instance = record.instance
            if instance is None or capability not in {
                str(value) for value in getattr(instance, "capabilities", ())
            }:
                continue
            try:
                status = instance.status()
                if not isinstance(status, Mapping):
                    raise TypeError("Plugin status must be a JSON object")
                if str(status.get("state") or "ready") != "ready":
                    continue
                payload = getattr(instance, method_name)()
                projection = normalizer(plugin_id, payload)
                projected_memberships = _navigation_memberships_for_known_identifiers(
                    projection,
                    known,
                    identifier_field=identifier_field,
                )
            except Exception as exc:
                errors.append(
                    {
                        "pluginId": plugin_id,
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            groups.extend(projection["groups"])
            memberships.extend(projected_memberships)
        return {
            "groups": groups,
            "memberships": memberships,
            "errors": errors,
        }

    def project_playlist_groups(
        self,
        known_playlist_ids: Collection[str] | None = None,
    ) -> dict[str, Any]:
        return self._project_navigation_groups(
            capability="playlist_groups",
            method_name="project_playlist_groups",
            identifier_field="playlist_id",
            normalizer=_normalized_playlist_group_projection,
            known_identifiers=known_playlist_ids,
        )

    def project_channel_groups(
        self,
        known_channel_ids: Collection[str] | None = None,
    ) -> dict[str, Any]:
        return self._project_navigation_groups(
            capability="channel_groups",
            method_name="project_channel_groups",
            identifier_field="channel_id",
            normalizer=_normalized_channel_group_projection,
            known_identifiers=known_channel_ids,
        )

    def _identifiers_for_group(
        self,
        group_key: str,
        *,
        key_prefix: str,
        identifier_field: str,
        projection: dict[str, Any],
    ) -> frozenset[str] | None:
        normalized_group_key = str(group_key or "").strip()
        if not normalized_group_key.startswith(key_prefix):
            return None
        parent_by_group = {
            str(group["group_key"]): (
                str(group["parent_key"]) if group.get("parent_key") else None
            )
            for group in projection["groups"]
        }
        if normalized_group_key not in parent_by_group:
            return None

        selected_groups = {normalized_group_key}
        changed = True
        while changed:
            changed = False
            for candidate, parent in parent_by_group.items():
                if candidate not in selected_groups and parent in selected_groups:
                    selected_groups.add(candidate)
                    changed = True
        return frozenset(
            str(membership[identifier_field])
            for membership in projection["memberships"]
            if membership["group_key"] in selected_groups
        )

    def playlist_ids_for_group(
        self,
        group_key: str,
        known_playlist_ids: Collection[str] | None = None,
    ) -> frozenset[str] | None:
        if not str(group_key or "").strip().startswith(
            PLUGIN_PLAYLIST_GROUP_KEY_PREFIX
        ):
            return None
        return self._identifiers_for_group(
            group_key,
            key_prefix=PLUGIN_PLAYLIST_GROUP_KEY_PREFIX,
            identifier_field="playlist_id",
            projection=self.project_playlist_groups(known_playlist_ids),
        )

    def channel_ids_for_group(
        self,
        group_key: str,
        known_channel_ids: Collection[str] | None = None,
    ) -> frozenset[str] | None:
        if not str(group_key or "").strip().startswith(
            PLUGIN_CHANNEL_GROUP_KEY_PREFIX
        ):
            return None
        return self._identifiers_for_group(
            group_key,
            key_prefix=PLUGIN_CHANNEL_GROUP_KEY_PREFIX,
            identifier_field="channel_id",
            projection=self.project_channel_groups(known_channel_ids),
        )

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
