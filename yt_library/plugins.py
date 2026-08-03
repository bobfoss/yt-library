"""Versioned optional plugin discovery and request dispatch."""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PLUGIN_API_VERSION = 1
PLUGIN_ENTRY_POINT_GROUP = "yt_library.plugins"
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]*$")


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


class PluginManager:
    """Load only explicitly enabled plugins and contain plugin failures."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        entry_points: Iterable[Any] | None = None,
    ) -> None:
        self._config = config
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

    def shutdown(self) -> None:
        for record in reversed(list(self._records.values())):
            if record.instance is None:
                continue
            try:
                record.instance.shutdown()
            except Exception:
                pass
            record.instance = None
