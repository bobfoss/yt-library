from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from yt_library import core
from yt_library.config import load_config
from yt_library.plugins import PLUGIN_API_VERSION, PluginManager, PluginTaskWorker
from yt_library.workers import WorkerQueueDispatcher

from tests.support import migrated_connection


class FakePlugin:
    plugin_id = "subtitles"
    plugin_name = "Test Subtitles"
    plugin_version = "1.2.3"
    plugin_api_version = PLUGIN_API_VERSION
    capabilities = {"subtitle_search", "playlist_groups"}
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
            "hooks": ["library_update", "video_scan"],
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
            playlist_groups = manager.project_playlist_groups(
                {"PLparent", "PLchild"}
            )
            parent_playlist_ids = manager.playlist_ids_for_group(
                "plugin:subtitles:parent"
            )
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
