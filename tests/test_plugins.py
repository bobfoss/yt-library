from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yt_library.plugins import PLUGIN_API_VERSION, PluginManager


class FakePlugin:
    plugin_id = "subtitles"
    plugin_name = "Test Subtitles"
    plugin_version = "1.2.3"
    plugin_api_version = PLUGIN_API_VERSION
    capabilities = {"subtitle_search"}

    def __init__(self) -> None:
        self.context = None
        self.stopped = False

    def start(self, context) -> None:
        self.context = context

    def status(self):
        return {"state": "ready", "database": {"available": True}}

    def handle_api(self, method, path, query):
        if method == "GET" and path == "search":
            return 200, {"query": (query.get("q") or [""])[0]}
        return None

    def shutdown(self) -> None:
        self.stopped = True


class FakeEntryPoint:
    name = "subtitles"

    def __init__(self, factory) -> None:
        self.factory = factory
        self.load_count = 0

    def load(self):
        self.load_count += 1
        return self.factory


class PluginManagerTests(unittest.TestCase):
    def test_disabled_plugin_does_not_load_installed_entry_point(self) -> None:
        entry_point = FakeEntryPoint(FakePlugin)

        manager = PluginManager(
            {"plugins": {"subtitles": {"enabled": False}}},
            entry_points=[entry_point],
        )

        self.assertEqual(entry_point.load_count, 0)
        self.assertEqual(manager.statuses(), [])

    def test_enabled_plugin_loads_with_versioned_context_and_routes_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entry_point = FakeEntryPoint(FakePlugin)
            manager = PluginManager(
                {
                    "_config_path": str(root / "yt_library.config.json"),
                    "plugins": {
                        "subtitles": {
                            "enabled": True,
                            "config": "../YT Subtitles/yt_subtitles.config.json",
                        }
                    },
                },
                entry_points=[entry_point],
            )

            status = manager.statuses()[0]
            response_status, payload = manager.handle_api(
                "subtitles", "GET", "search", {"q": ["history"]}
            )

            self.assertEqual(entry_point.load_count, 1)
            self.assertEqual(status["state"], "ready")
            self.assertEqual(status["apiVersion"], PLUGIN_API_VERSION)
            self.assertEqual(response_status, 200)
            self.assertEqual(payload, {"query": "history"})

    def test_incompatible_and_missing_plugins_are_nonfatal(self) -> None:
        class IncompatiblePlugin(FakePlugin):
            plugin_api_version = 999

        manager = PluginManager(
            {
                "plugins": {
                    "subtitles": {"enabled": True},
                    "missing": {"enabled": True},
                }
            },
            entry_points=[FakeEntryPoint(IncompatiblePlugin)],
        )

        statuses = {status["id"]: status for status in manager.statuses()}
        self.assertEqual(statuses["subtitles"]["state"], "incompatible")
        self.assertEqual(statuses["missing"]["state"], "missing")
        request_status, _ = manager.handle_api("subtitles", "GET", "status", {})
        self.assertEqual(request_status, 503)


if __name__ == "__main__":
    unittest.main()
