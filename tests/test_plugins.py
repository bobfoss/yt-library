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
    browser_assets = (
        {"path": "browser.css", "type": "style"},
        {"path": "browser.js", "type": "script"},
    )

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

    def handle_browser_asset(self, path):
        content_types = {
            "browser.css": "text/css; charset=utf-8",
            "browser.js": "text/javascript; charset=utf-8",
        }
        return content_types[path], f"/* {path} */"

    def filter_videos(self, query):
        return {
            "video_ids": {"available", "unavailable"},
            "search_match_ids": {"unavailable"} if query else set(),
        }

    def project_videos(self, video_ids):
        return [
            {"video_id": video_id, "title": f"Projected {video_id}"}
            for video_id in sorted(video_ids)
            if video_id != "unavailable"
        ]

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
    def test_core_query_and_browser_templates_have_no_plugin_specific_contract(self) -> None:
        root = Path(__file__).resolve().parents[1] / "yt_library"
        for path in (
            root / "queries.py",
            root / "templates" / "index.html",
            root / "templates" / "index.js",
        ):
            with self.subTest(path=path):
                self.assertNotIn("subtitle", path.read_text(encoding="utf-8").casefold())

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
            self.assertEqual(status["browserAssets"], list(FakePlugin.browser_assets))
            self.assertEqual(response_status, 200)
            self.assertEqual(payload, {"query": "history"})
            asset_status, content_type, body = manager.handle_browser_asset(
                "subtitles", "browser.js"
            )
            video_ids, search_match_ids = manager.filter_videos(
                "subtitles", "history"
            )
            projections = manager.project_videos(
                "subtitles",
                {"available", "unavailable"},
            )
            projected_video = manager.projected_video("available")
            self.assertEqual(asset_status, 200)
            self.assertEqual(content_type, "text/javascript; charset=utf-8")
            self.assertEqual(body, b"/* browser.js */")
            self.assertEqual(video_ids, frozenset({"available", "unavailable"}))
            self.assertEqual(search_match_ids, frozenset({"unavailable"}))
            self.assertEqual(
                projections,
                {
                    "available": {
                        "video_id": "available",
                        "title": "Projected available",
                    }
                },
            )
            self.assertEqual(
                projected_video,
                {
                    "video_id": "available",
                    "title": "Projected available",
                    "projection_plugin_ids": ["subtitles"],
                },
            )

    def test_video_projection_contract_rejects_invalid_plugin_rows(self) -> None:
        class InvalidProjectionPlugin(FakePlugin):
            def project_videos(self, video_ids):
                return [{"video_id": "not-requested", "title": "Wrong"}]

        manager = PluginManager(
            {"plugins": {"subtitles": {"enabled": True}}},
            entry_points=[FakeEntryPoint(InvalidProjectionPlugin)],
        )

        with self.assertRaisesRegex(ValueError, "unrequested"):
            manager.project_videos("subtitles", {"requested"})

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
