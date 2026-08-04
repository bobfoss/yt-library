"""Versioned optional plugin discovery and request dispatch."""

from __future__ import annotations

from collections.abc import Collection, Iterable as IterableCollection, Mapping
import importlib.metadata as importlib_metadata
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PLUGIN_API_VERSION = 1
PLUGIN_ENTRY_POINT_GROUP = "yt_library.plugins"
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
PLUGIN_BROWSER_ASSET_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
PLUGIN_BROWSER_ASSET_TYPES = {"script", "style"}


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
            _browser_assets(instance)
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
