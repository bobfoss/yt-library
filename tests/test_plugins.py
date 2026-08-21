from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yt_library import core
from yt_library.config import load_config
from yt_library.plugins import (
    PLUGIN_API_VERSION,
    PLUGIN_HOST_FEATURES,
    PluginManager,
    PluginPlanningContext,
    PluginTaskWorker,
    PluginYoutubeSession,
    PluginWorkerRuntime,
    PluginWorkerStopped,
)
from yt_library.workers import WorkerQueueDispatcher

from tests.support import migrated_connection


class FakePlugin:
    plugin_id = "subtitles"
    plugin_name = "Test Subtitles"
    plugin_version = "1.2.3"
    plugin_api_version = PLUGIN_API_VERSION
    capabilities = {"channel_groups", "subtitle_search", "playlist_groups"}
    browser_assets = (
        {"path": "browser.css", "type": "style"},
        {"path": "browser.js", "type": "script"},
    )
    worker_processes = (
        {
            "id": "fetch",
            "name": "Fetch test data",
            "service": "youtube",
            "maxInFlight": 2,
            "adminSurface": "advanced",
            "buttonLabel": "Fetch",
            "hooks": ["library_update", "video_scan", "clip_scan"],
            "adminActions": [
                {
                    "id": "fetch-video",
                    "placement": "videos",
                    "surface": "advanced",
                    "buttonLabel": "Fetch one",
                    "inputs": [
                        {
                            "name": "video_id",
                            "label": "Video ID",
                            "placeholder": "11-character ID",
                            "required": True,
                            "maxLength": 11,
                        }
                    ],
                }
            ],
        },
    )

    def __init__(self) -> None:
        self.context = None
        self.stopped = False

    def start(self, context) -> None:
        self.context = context

    def status(self):
        return {
            "state": "ready",
            "database": {"available": True},
            "adminMetrics": [
                {
                    "id": "items",
                    "label": "Items",
                    "value": 1234,
                    "format": "integer",
                },
                {
                    "id": "database-size",
                    "label": "Database size",
                    "value": 4096,
                    "format": "bytes",
                    "description": "Current plugin database file size.",
                },
            ],
        }

    def handle_api(self, method, path, query):
        if method == "GET" and path == "search":
            return 200, {"query": (query.get("q") or [""])[0]}
        return None

    def handle_api_request(self, method, path, query, body):
        if method == "POST" and path == "messages":
            return 201, {"body": body, "query": query}
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

    def filter_clips(self, query, clips):
        clip_ids = {
            str(clip["clip_id"])
            for clip in clips
            if str(clip.get("source_video_id") or "") == "available"
        }
        return {
            "clip_ids": clip_ids,
            "search_match_ids": clip_ids if query else set(),
        }

    def project_videos(self, video_ids):
        return [
            {"video_id": video_id, "title": f"Projected {video_id}"}
            for video_id in sorted(video_ids)
            if video_id != "unavailable"
        ]

    def project_playlist_groups(self):
        return {
            "revision": "example-1",
            "groups": [
                {
                    "group_key": "parent",
                    "name": "Parent",
                    "parent_key": None,
                    "position": 0,
                    "icon": "folder",
                },
                {
                    "group_key": "child",
                    "name": "Child",
                    "parent_key": "parent",
                    "position": 0,
                    "icon": "spark",
                },
                {
                    "group_key": "other",
                    "name": "Other",
                    "parent_key": None,
                    "position": 1,
                    "icon": "",
                },
            ],
            "memberships": [
                {
                    "group_key": "parent",
                    "playlist_id": "PLparent",
                    "position": 0,
                },
                {
                    "group_key": "child",
                    "playlist_id": "PLchild",
                    "position": 0,
                },
                {
                    "group_key": "other",
                    "playlist_id": "PLmissing",
                    "position": 0,
                },
            ],
        }

    def project_channel_groups(self):
        return {
            "revision": "channels-1",
            "groups": [
                {
                    "group_key": "channels",
                    "name": "Channels",
                    "parent_key": None,
                    "position": 0,
                    "icon": "folder",
                },
                {
                    "group_key": "science",
                    "name": "Science",
                    "parent_key": "channels",
                    "position": 0,
                    "icon": "spark",
                },
                {
                    "group_key": "space",
                    "name": "Space",
                    "parent_key": "science",
                    "position": 0,
                    "icon": "rocket",
                },
                {
                    "group_key": "other-channels",
                    "name": "Other channels",
                    "parent_key": None,
                    "position": 1,
                    "icon": "",
                },
            ],
            "memberships": [
                {
                    "group_key": "channels",
                    "channel_id": "UCparent",
                    "position": 0,
                },
                {
                    "group_key": "science",
                    "channel_id": "UCchild",
                    "position": 0,
                },
                {
                    "group_key": "space",
                    "channel_id": "UCgrandchild",
                    "position": 0,
                },
                {
                    "group_key": "other-channels",
                    "channel_id": "UCmissing",
                    "position": 0,
                },
            ],
        }

    def plan_worker(self, worker_id, context, params):
        self.planned_params = params
        return [
            {
                "task_id": row["video_id"],
                "subject_id": row["video_id"],
                "video_id": row["video_id"],
                "title": row["title"],
                "payload": {"example": True},
            }
            for row in context.library_videos()
        ]

    def run_worker(self, worker_id, task, runtime):
        runtime.log("info", f"Processed {task['video_id']}")
        return {
            "outcome": "found",
            "processed": 1,
            "found": 1,
            "message": "Test plugin task complete",
        }

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
    def test_planning_context_exposes_generic_broadcast_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "live1234567",
                        title="Active stream",
                        channel_id="UCstreamowner",
                        channel_title="Stream owner",
                        video_type="livestream",
                        broadcast_status="live",
                        broadcast_started_at="2026-08-11T10:00:00Z",
                        broadcast_ended_at=None,
                        broadcast_status_checked_at="2026-08-11T10:05:00Z",
                        source="youtube",
                    )
                videos = list(
                    PluginPlanningContext(conn, "example").library_videos()
                )
            finally:
                conn.close()

        self.assertEqual(
            videos,
            [
                {
                    "video_id": "live1234567",
                    "title": "Active stream",
                    "channel_id": "UCstreamowner",
                    "availability": "unknown",
                    "is_playable": None,
                    "video_type": "livestream",
                    "broadcast_status": "live",
                    "broadcast_started_at": "2026-08-11T10:00:00Z",
                    "broadcast_ended_at": None,
                    "broadcast_status_checked_at": "2026-08-11T10:05:00Z",
                }
            ],
        )

    def test_start_context_looks_up_explicit_library_videos(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "abcdefghijk",
                        title="Captured stream",
                        channel_id="UCstreamowner",
                        channel_title="Stream owner",
                        video_type="livestream",
                        broadcast_status="ended",
                        source="youtube",
                    )
            finally:
                conn.close()
            plugin = FakePlugin()
            PluginManager(
                {
                    "_config_path": str(root / "yt_library.config.json"),
                    "plugins": {"subtitles": {"enabled": True}},
                },
                db_path=db_path,
                entry_points=[FakeEntryPoint(lambda: plugin)],
            )

            videos = plugin.context.library_videos(
                ["missing0000", "abcdefghijk", "abcdefghijk"]
            )

            self.assertEqual(len(videos), 1)
            self.assertEqual(videos[0]["video_id"], "abcdefghijk")
            self.assertEqual(videos[0]["channel_id"], "UCstreamowner")
            self.assertEqual(videos[0]["broadcast_status"], "ended")
            with self.assertRaisesRegex(ValueError, "Invalid YouTube video ID"):
                plugin.context.library_videos(["invalid"])

    def test_planning_context_exposes_canonical_clip_sources_and_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(conn, "abcdefghijk", title="Source title")
                    core.save_clip_metadata(
                        conn,
                        {
                            "clip_id": "UgkxPlanningClip",
                            "title": "Clip title",
                            "source_video_id": "abcdefghijk",
                            "start_ms": 1_000,
                            "end_ms": 2_000,
                            "availability": "active",
                        },
                        fetched=True,
                    )
                clips = list(PluginPlanningContext(conn, "example").library_clips())
            finally:
                conn.close()

        self.assertEqual(
            clips,
            [
                {
                    "clip_id": "UgkxPlanningClip",
                    "title": "Clip title",
                    "source_video_id": "abcdefghijk",
                    "source_title": "Source title",
                    "start_ms": 1_000,
                    "end_ms": 2_000,
                    "availability": "active",
                }
            ],
        )

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
            {
                "plugins": {
                    "subtitles": {
                        "enabled": False,
                        "name": "YT Subtitles",
                    }
                }
            },
            entry_points=[entry_point],
        )

        self.assertEqual(entry_point.load_count, 0)
        self.assertEqual(
            manager.statuses(),
            [
                {
                    "id": "subtitles",
                    "name": "YT Subtitles",
                    "enabled": False,
                    "state": "disabled",
                    "message": "Plugin is disabled",
                }
            ],
        )

    def test_plugin_statuses_preserve_configured_order(self) -> None:
        manager = PluginManager(
            {
                "plugins": {
                    "subtitles": {"enabled": False},
                    "llm": {"enabled": False},
                }
            },
            entry_points=[],
        )

        self.assertEqual(
            [status["id"] for status in manager.statuses()],
            ["subtitles", "llm"],
        )

    def test_enabled_plugin_loads_with_versioned_context_and_routes_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin = FakePlugin()
            entry_point = FakeEntryPoint(lambda: plugin)
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
            mutation_status, mutation_payload = manager.handle_api(
                "subtitles",
                "POST",
                "messages",
                {"draft": ["1"]},
                {"message": "hello"},
            )

            self.assertEqual(entry_point.load_count, 1)
            self.assertEqual(status["state"], "ready")
            self.assertEqual(status["apiVersion"], PLUGIN_API_VERSION)
            self.assertEqual(status["requiredHostFeatures"], [])
            self.assertEqual(plugin.context.host_features, PLUGIN_HOST_FEATURES)
            self.assertEqual(status["browserAssets"], list(FakePlugin.browser_assets))
            self.assertEqual(
                status["adminMetrics"],
                [
                    {
                        "id": "items",
                        "label": "Items",
                        "value": 1234,
                        "format": "integer",
                        "description": "",
                    },
                    {
                        "id": "database-size",
                        "label": "Database size",
                        "value": 4096,
                        "format": "bytes",
                        "description": "Current plugin database file size.",
                    },
                ],
            )
            self.assertEqual(response_status, 200)
            self.assertEqual(payload, {"query": "history"})
            self.assertEqual(mutation_status, 201)
            self.assertEqual(
                mutation_payload,
                {"body": {"message": "hello"}, "query": {"draft": ["1"]}},
            )
            asset_status, content_type, body = manager.handle_browser_asset(
                "subtitles", "browser.js"
            )
            video_ids, search_match_ids = manager.filter_videos(
                "subtitles", "history"
            )
            clip_ids, clip_search_match_ids = manager.filter_clips(
                "subtitles",
                "history",
                (
                    {
                        "clip_id": "clip-available",
                        "source_video_id": "available",
                        "start_ms": 1_000,
                        "end_ms": 2_000,
                    },
                    {
                        "clip_id": "clip-other",
                        "source_video_id": "other",
                        "start_ms": 1_000,
                        "end_ms": 2_000,
                    },
                ),
            )
            projections = manager.project_videos(
                "subtitles",
                {"available", "unavailable"},
            )
            projected_video = manager.projected_video("available")
            playlist_groups = manager.project_playlist_groups(
                {"PLparent", "PLchild"}
            )
            parent_playlist_ids = manager.playlist_ids_for_group(
                "plugin:subtitles:parent"
            )
            channel_groups = manager.project_channel_groups(
                {"UCparent", "UCchild", "UCgrandchild"}
            )
            parent_channel_ids = manager.channel_ids_for_group(
                "plugin-channel:subtitles:channels"
            )
            self.assertEqual(asset_status, 200)
            self.assertEqual(content_type, "text/javascript; charset=utf-8")
            self.assertEqual(body, b"/* browser.js */")
            self.assertEqual(video_ids, frozenset({"available", "unavailable"}))
            self.assertEqual(search_match_ids, frozenset({"unavailable"}))
            self.assertEqual(clip_ids, frozenset({"clip-available"}))
            self.assertEqual(clip_search_match_ids, frozenset({"clip-available"}))
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
            self.assertEqual(
                [group["group_key"] for group in playlist_groups["groups"]],
                [
                    "plugin:subtitles:parent",
                    "plugin:subtitles:child",
                    "plugin:subtitles:other",
                ],
            )
            self.assertEqual(
                [
                    membership["playlist_id"]
                    for membership in playlist_groups["memberships"]
                ],
                ["PLparent", "PLchild"],
            )
            self.assertEqual(playlist_groups["errors"], [])
            self.assertEqual(
                parent_playlist_ids,
                frozenset({"PLparent", "PLchild"}),
            )
            self.assertEqual(
                [group["group_key"] for group in channel_groups["groups"]],
                [
                    "plugin-channel:subtitles:channels",
                    "plugin-channel:subtitles:science",
                    "plugin-channel:subtitles:space",
                    "plugin-channel:subtitles:other-channels",
                ],
            )
            self.assertEqual(
                [
                    membership["channel_id"]
                    for membership in channel_groups["memberships"]
                ],
                ["UCparent", "UCchild", "UCgrandchild"],
            )
            self.assertEqual(channel_groups["errors"], [])
            self.assertEqual(
                parent_channel_ids,
                frozenset({"UCparent", "UCchild", "UCgrandchild"}),
            )

    def test_youtube_session_factory_is_exposed_through_plugin_context(self) -> None:
        plugin = FakePlugin()
        expected_session = object()
        factory = Mock(return_value=expected_session)
        PluginManager(
            {"plugins": {"subtitles": {"enabled": True}}},
            entry_points=[FakeEntryPoint(lambda: plugin)],
            youtube_session_factory=factory,
        )

        session = plugin.context.youtube_video_session("abcdefghijk")

        self.assertIs(session, expected_session)
        factory.assert_called_once_with("abcdefghijk")

    def test_youtube_session_injects_host_context_and_bounds_transport(self) -> None:
        opener = Mock(spec=urllib.request.OpenerDirector)
        jar = CookieJar()
        session = PluginYoutubeSession(
            video_id="abcdefghijk",
            initial_data={"welcome": {"continuation": "opaque"}},
            opener=opener,
            cookie_jar=jar,
            api_key="public-key",
            client_version="1.2.3",
            client_context={"client": {"clientName": "WEB"}},
            referer="https://www.youtube.com/watch?v=abcdefghijk",
        )

        with patch(
            "yt_library.core.request_youtubei_json",
            return_value={"actions": []},
        ) as request:
            response = session.request_json(
                "get_panel",
                {"continuation": "opaque"},
                click_tracking_params="tracking",
            )

        self.assertEqual(response, {"actions": []})
        sent = request.call_args.args[3]
        self.assertEqual(sent["continuation"], "opaque")
        self.assertEqual(sent["context"]["client"]["clientName"], "WEB")
        self.assertEqual(
            sent["context"]["clickTracking"]["clickTrackingParams"],
            "tracking",
        )
        self.assertNotIn("Authorization", sent)
        copied = session.initial_data
        copied["welcome"] = {}
        self.assertEqual(
            session.initial_data,
            {"welcome": {"continuation": "opaque"}},
        )
        with self.assertRaisesRegex(ValueError, "host-owned"):
            session.request_json("get_panel", {"context": {}})
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            session.request_json("browse", {})

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

    def test_invalid_playlist_group_projection_is_contained(self) -> None:
        class InvalidGroupPlugin(FakePlugin):
            def project_playlist_groups(self):
                return {
                    "groups": [
                        {
                            "group_key": "parent",
                            "name": "Parent",
                            "parent_key": "missing",
                            "position": 0,
                        }
                    ],
                    "memberships": [],
                }

        manager = PluginManager(
            {"plugins": {"subtitles": {"enabled": True}}},
            entry_points=[FakeEntryPoint(InvalidGroupPlugin)],
        )

        projection = manager.project_playlist_groups()

        self.assertEqual(projection["groups"], [])
        self.assertEqual(projection["memberships"], [])
        self.assertEqual(projection["errors"][0]["pluginId"], "subtitles")
        self.assertIn("missing parent", projection["errors"][0]["message"])

    def test_plugin_can_project_one_group_for_unmatched_known_playlists(self) -> None:
        class UnmatchedGroupPlugin(FakePlugin):
            def project_playlist_groups(self):
                projection = super().project_playlist_groups()
                projection["groups"].append(
                    {
                        "group_key": "uncategorized",
                        "name": "Uncategorized",
                        "parent_key": None,
                        "position": 2,
                        "include_unmatched": True,
                    }
                )
                return projection

        manager = PluginManager(
            {"plugins": {"subtitles": {"enabled": True}}},
            entry_points=[FakeEntryPoint(UnmatchedGroupPlugin)],
        )

        projection = manager.project_playlist_groups(
            {"PLparent", "PLchild", "PLuncategorized"}
        )
        identifiers = manager.playlist_ids_for_group(
            "plugin:subtitles:uncategorized",
            {"PLparent", "PLchild", "PLuncategorized"},
        )

        unmatched_group = next(
            group
            for group in projection["groups"]
            if group["group_key"] == "plugin:subtitles:uncategorized"
        )
        self.assertTrue(unmatched_group["include_unmatched"])
        self.assertEqual(
            [
                membership["playlist_id"]
                for membership in projection["memberships"]
                if membership["group_key"] == "plugin:subtitles:uncategorized"
            ],
            ["PLuncategorized"],
        )
        self.assertEqual(identifiers, frozenset({"PLuncategorized"}))

    def test_multiple_unmatched_groups_are_contained(self) -> None:
        class InvalidGroupPlugin(FakePlugin):
            def project_playlist_groups(self):
                return {
                    "groups": [
                        {
                            "group_key": "first",
                            "name": "First",
                            "include_unmatched": True,
                        },
                        {
                            "group_key": "second",
                            "name": "Second",
                            "include_unmatched": True,
                        },
                    ],
                    "memberships": [],
                }

        manager = PluginManager(
            {"plugins": {"subtitles": {"enabled": True}}},
            entry_points=[FakeEntryPoint(InvalidGroupPlugin)],
        )

        projection = manager.project_playlist_groups({"PLuncategorized"})

        self.assertEqual(projection["groups"], [])
        self.assertIn("multiple unmatched", projection["errors"][0]["message"])

    def test_invalid_channel_group_projection_is_contained(self) -> None:
        class InvalidGroupPlugin(FakePlugin):
            def project_channel_groups(self):
                return {
                    "groups": [
                        {
                            "group_key": "parent",
                            "name": "Parent",
                            "parent_key": None,
                            "position": 0,
                        }
                    ],
                    "memberships": [
                        {
                            "group_key": "parent",
                            "channel_id": "UCduplicate",
                            "position": 0,
                        },
                        {
                            "group_key": "parent",
                            "channel_id": "UCduplicate",
                            "position": 1,
                        },
                    ],
                }

        manager = PluginManager(
            {"plugins": {"subtitles": {"enabled": True}}},
            entry_points=[FakeEntryPoint(InvalidGroupPlugin)],
        )

        projection = manager.project_channel_groups()

        self.assertEqual(projection["groups"], [])
        self.assertEqual(projection["memberships"], [])
        self.assertEqual(projection["errors"][0]["pluginId"], "subtitles")
        self.assertIn("duplicate channel memberships", projection["errors"][0]["message"])

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

    def test_plugin_required_host_features_are_negotiated(self) -> None:
        class SupportedPlugin(FakePlugin):
            required_host_features = {"youtube_ytdlp_v1"}

        class UnsupportedPlugin(FakePlugin):
            plugin_id = "unsupported"
            required_host_features = {"future_youtube_service"}

        manager = PluginManager(
            {
                "plugins": {
                    "subtitles": {"enabled": True},
                    "unsupported": {"enabled": True},
                }
            },
            entry_points=[
                FakeEntryPoint(SupportedPlugin),
                type(
                    "UnsupportedEntryPoint",
                    (),
                    {"name": "unsupported", "load": lambda self: UnsupportedPlugin},
                )(),
            ],
        )

        statuses = {status["id"]: status for status in manager.statuses()}
        self.assertEqual(statuses["subtitles"]["state"], "ready")
        self.assertEqual(
            statuses["subtitles"]["requiredHostFeatures"],
            ["youtube_ytdlp_v1"],
        )
        self.assertEqual(statuses["unsupported"]["state"], "incompatible")
        self.assertIn("future_youtube_service", statuses["unsupported"]["message"])

    def test_youtube_plugin_runtime_owns_ytdlp_network_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            cookie_file = root / "youtube-cookies.txt"
            cookie_file.write_text("configured cookies", encoding="utf-8")
            observed: dict[str, object] = {}

            class FakeYoutubeDL:
                def __init__(self, options):
                    observed["options"] = options

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def extract_info(self, url, *, download):
                    options = observed["options"]
                    working_cookie = Path(options["cookiefile"])
                    observed["cookiePath"] = working_cookie
                    observed["cookieText"] = working_cookie.read_text(encoding="utf-8")
                    working_cookie.write_text("yt-dlp mutation", encoding="utf-8")
                    observed["url"] = url
                    observed["download"] = download
                    return {"id": "abcdefghijk", "title": "Example"}

            runtime = PluginWorkerRuntime(
                db_path,
                run_id="run",
                queue_id=1,
                plugin_id="subtitles",
                worker_id="fetch",
                subject_id="abcdefghijk",
                stop_event=threading.Event(),
                service="youtube",
                cookie_file=cookie_file,
                proxy_url="socks5h://127.0.0.1:1080",
            )
            with patch.dict(
                sys.modules,
                {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYoutubeDL)},
            ):
                info = runtime.run_youtube_ytdlp(
                    "abcdefghijk",
                    {"skip_download": True, "outtmpl": str(root / "%(id)s.%(ext)s")},
                    download=True,
                )

            options = observed["options"]
            self.assertEqual(info["id"], "abcdefghijk")
            self.assertEqual(observed["cookieText"], "configured cookies")
            self.assertFalse(Path(observed["cookiePath"]).exists())
            self.assertEqual(cookie_file.read_text(encoding="utf-8"), "configured cookies")
            self.assertEqual(options["proxy"], "socks5h://127.0.0.1:1080")
            self.assertEqual(options["retries"], 2)
            self.assertEqual(options["fragment_retries"], 3)
            self.assertEqual(observed["download"], True)
            self.assertEqual(
                observed["url"],
                "https://www.youtube.com/watch?v=abcdefghijk",
            )
            with self.assertRaisesRegex(ValueError, "host policy"):
                runtime.run_youtube_ytdlp(
                    "abcdefghijk",
                    {"proxy": "socks5h://127.0.0.1:9999"},
                    download=False,
                )

    def test_youtube_plugin_runtime_converts_stop_hook_to_host_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            stop_event = threading.Event()
            observed: dict[str, object] = {}

            class FakeYoutubeDL:
                def __init__(self, options):
                    observed["options"] = options

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def extract_info(self, url, *, download):
                    stop_event.set()
                    observed["options"]["progress_hooks"][0]({})
                    raise AssertionError("stop hook did not cancel")

            runtime = PluginWorkerRuntime(
                db_path,
                run_id="run",
                queue_id=1,
                plugin_id="subtitles",
                worker_id="fetch",
                subject_id="abcdefghijk",
                stop_event=stop_event,
                service="youtube",
            )
            with patch.dict(
                sys.modules,
                {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYoutubeDL)},
            ):
                with self.assertRaises(PluginWorkerStopped):
                    runtime.run_youtube_ytdlp(
                        "abcdefghijk",
                        {"skip_download": True},
                        download=True,
                    )

    def test_invalid_plugin_admin_action_is_nonfatal(self) -> None:
        class InvalidAdminPlugin(FakePlugin):
            worker_processes = (
                {
                    **FakePlugin.worker_processes[0],
                    "adminActions": [
                        {
                            "id": "invalid",
                            "placement": "database",
                            "buttonLabel": "Invalid",
                        }
                    ],
                },
            )

        manager = PluginManager(
            {"plugins": {"subtitles": {"enabled": True}}},
            entry_points=[FakeEntryPoint(InvalidAdminPlugin)],
        )

        status = manager.statuses()[0]
        self.assertEqual(status["state"], "error")
        self.assertIn("Invalid plugin admin placement", status["message"])

    def test_invalid_plugin_admin_metric_is_nonfatal(self) -> None:
        class InvalidAdminMetricPlugin(FakePlugin):
            def status(self):
                return {
                    "state": "ready",
                    "adminMetrics": [
                        {
                            "id": "database-size",
                            "label": "Database size",
                            "value": -1,
                            "format": "bytes",
                        }
                    ],
                }

        manager = PluginManager(
            {"plugins": {"subtitles": {"enabled": True}}},
            entry_points=[FakeEntryPoint(InvalidAdminMetricPlugin)],
        )

        status = manager.statuses()[0]
        self.assertEqual(status["state"], "error")
        self.assertIn("value must be a nonnegative integer", status["message"])

    def test_plugin_process_plans_queues_runs_and_logs_host_owned_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(conn, "abcdefghijk", title="Example video")
            finally:
                conn.close()
            manager = PluginManager(
                {"plugins": {"subtitles": {"enabled": True}}},
                db_path=db_path,
                entry_points=[FakeEntryPoint(FakePlugin)],
            )
            conn = core.connect(db_path)
            try:
                with conn:
                    queued = manager.enqueue_process(
                        conn,
                        "subtitles",
                        "fetch",
                        {"source": ["admin"]},
                        manual=True,
                    )
                row = dict(core.worker_queue_rows(conn, limit=1)[0])
                queue_log = dict(
                    conn.execute(
                        "SELECT * FROM plugin_worker_log WHERE level = 'queue info'"
                    ).fetchone()
                )
            finally:
                conn.close()

            self.assertEqual(queued["inserted"], 1)
            self.assertEqual(row["worker_type"], "plugin")
            self.assertEqual(row["source_key"], "subtitles")
            self.assertEqual(row["task_type"], "fetch")
            self.assertEqual(row["plugin_subject_id"], "abcdefghijk")
            self.assertEqual(row["payload_json"], '{"example":true}')
            self.assertEqual(queue_log["subject_id"], "abcdefghijk")

            worker = PluginTaskWorker()
            self.assertTrue(worker.start(db_path, manager, row)["started"])
            deadline = time.monotonic() + 5
            while worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(worker.is_alive())

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_count(conn), 0)
                run = dict(
                    conn.execute(
                        "SELECT * FROM plugin_worker_runs ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()
                )
                logs = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM plugin_worker_log ORDER BY id"
                    )
                ]
                page, total = core.worker_log_page(
                    conn,
                    source="plugin:subtitles",
                )
            finally:
                conn.close()
            self.assertEqual(run["status"], "complete")
            self.assertEqual(run["outcome"], "found")
            self.assertEqual(run["found"], 1)
            self.assertTrue(any(log["message"] == "Processed abcdefghijk" for log in logs))
            self.assertEqual(total, len(logs))
            self.assertTrue(all(row["source"] == "plugin:subtitles" for row in page))
            queue_page_log = next(row for row in page if row["level"] == "queue info")
            self.assertEqual(queue_page_log["identifier"], "abcdefghijk")
            self.assertEqual(queue_page_log["subject_id"], "Example video")

            status = manager.statuses()[0]
            process = status["workerProcesses"][0]
            self.assertEqual(process["service"], "youtube")
            self.assertEqual(process["latestRun"]["outcome"], "found")
            self.assertEqual(
                [action["placement"] for action in process["adminActions"]],
                ["plugin", "videos"],
            )
            targeted_action = process["adminActions"][1]
            self.assertEqual(targeted_action["id"], "fetch-video")
            self.assertEqual(targeted_action["inputs"][0]["name"], "video_id")
            self.assertTrue(targeted_action["inputs"][0]["required"])

    def test_host_cancellation_interrupts_plugin_run_and_retains_queue_row(self) -> None:
        class CancelledPlugin(FakePlugin):
            def run_worker(self, worker_id, task, runtime):
                raise PluginWorkerStopped("Stopped during host retrieval")

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(conn, "abcdefghijk", title="Example video")
            finally:
                conn.close()
            manager = PluginManager(
                {"plugins": {"subtitles": {"enabled": True}}},
                db_path=db_path,
                entry_points=[FakeEntryPoint(CancelledPlugin)],
            )
            conn = core.connect(db_path)
            try:
                with conn:
                    manager.enqueue_process(
                        conn,
                        "subtitles",
                        "fetch",
                        {},
                        manual=True,
                    )
                row = dict(core.worker_queue_rows(conn, limit=1)[0])
            finally:
                conn.close()

            worker = PluginTaskWorker()
            self.assertTrue(worker.start(db_path, manager, row)["started"])
            deadline = time.monotonic() + 5
            while worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)

            conn = core.connect(db_path)
            try:
                queue_count = core.worker_queue_count(conn)
                run = conn.execute(
                    "SELECT status, outcome FROM plugin_worker_runs"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(queue_count, 1)
            self.assertEqual(tuple(run), ("interrupted", "cancelled"))

    def test_plugin_bulk_queue_log_has_no_misleading_single_subject(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(conn, "abcdefghijk", title="First video")
                    core.upsert_video(conn, "lmnopqrstuv", title="Second video")
            finally:
                conn.close()
            manager = PluginManager(
                {"plugins": {"subtitles": {"enabled": True}}},
                db_path=db_path,
                entry_points=[FakeEntryPoint(FakePlugin)],
            )
            conn = core.connect(db_path)
            try:
                with conn:
                    queued = manager.enqueue_process(
                        conn,
                        "subtitles",
                        "fetch",
                        {},
                        manual=True,
                    )
                queue_log = conn.execute(
                    "SELECT subject_id FROM plugin_worker_log WHERE level = 'queue info'"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(queued["planned"], 2)
            self.assertEqual(queue_log["subject_id"], "")

    def test_plugin_process_can_hook_library_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(conn, "abcdefghijk", title="Example video")
            finally:
                conn.close()
            manager = PluginManager(
                {"plugins": {"subtitles": {"enabled": True}}},
                db_path=db_path,
                entry_points=[FakeEntryPoint(FakePlugin)],
            )
            conn = core.connect(db_path)
            try:
                with conn:
                    results = manager.enqueue_hook(conn, "library_update")
                row = conn.execute(
                    "SELECT worker_type, source_key, manual FROM worker_queue"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(results[0]["inserted"], 1)
            self.assertEqual(tuple(row), ("plugin", "subtitles", 0))

    def test_plugin_hook_forwards_event_parameters_to_planner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(conn, "abcdefghijk", title="Example video")
            finally:
                conn.close()
            plugin = FakePlugin()
            manager = PluginManager(
                {"plugins": {"subtitles": {"enabled": True}}},
                db_path=db_path,
                entry_points=[FakeEntryPoint(lambda: plugin)],
            )
            conn = core.connect(db_path)
            try:
                with conn:
                    manager.enqueue_hook(
                        conn,
                        "video_scan",
                        {"video_id": ["abcdefghijk"]},
                    )
            finally:
                conn.close()

            self.assertEqual(
                plugin.planned_params,
                {
                    "hook": "video_scan",
                    "video_id": ["abcdefghijk"],
                },
            )

    def test_plugin_hook_failure_is_contained_and_rolls_back_partial_plan(self) -> None:
        class FailingHookPlugin(FakePlugin):
            def plan_worker(self, worker_id, context, params):
                yield {
                    "task_id": "abcdefghijk",
                    "subject_id": "abcdefghijk",
                    "video_id": "abcdefghijk",
                    "title": "Partial task",
                    "payload": {},
                }
                raise RuntimeError("planner broke")

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            manager = PluginManager(
                {"plugins": {"subtitles": {"enabled": True}}},
                db_path=db_path,
                entry_points=[FakeEntryPoint(FailingHookPlugin)],
            )
            conn = core.connect(db_path)
            try:
                with conn:
                    results = manager.enqueue_hook(
                        conn,
                        "video_scan",
                        {"video_id": ["abcdefghijk"]},
                    )
                queued = conn.execute(
                    "SELECT COUNT(*) FROM worker_queue WHERE worker_type = 'plugin'"
                ).fetchone()[0]
                log = conn.execute(
                    """
                    SELECT level, message
                    FROM plugin_worker_log
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(queued, 0)
            self.assertIn("planner broke", results[0]["error"])
            self.assertEqual(log["level"], "queue error")
            self.assertIn("video_scan hook planning failed", log["message"])

    def test_common_dispatcher_runs_plugin_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(conn, "abcdefghijk", title="Example video")
            finally:
                conn.close()
            manager = PluginManager(
                {"plugins": {"subtitles": {"enabled": True}}},
                db_path=db_path,
                entry_points=[FakeEntryPoint(FakePlugin)],
            )
            conn = core.connect(db_path)
            try:
                with conn:
                    manager.enqueue_process(
                        conn,
                        "subtitles",
                        "fetch",
                        {},
                        manual=True,
                    )
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            dispatcher_config = load_config(root / "config.json")
            dispatcher_config.update(
                {
                    "job_dispatch_delay_seconds": 0,
                    "youtube_max_in_flight": 2,
                }
            )
            started = dispatcher.start(
                db_path,
                root / "cookies.txt",
                root / "thumbs",
                dispatcher_config,
                manager,
            )
            self.assertTrue(started["started"])
            deadline = time.monotonic() + 5
            while dispatcher.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(dispatcher.is_alive())

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_count(conn), 0)
                run = conn.execute(
                    "SELECT status, outcome FROM plugin_worker_runs"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(tuple(run), ("complete", "found"))


if __name__ == "__main__":
    unittest.main()
