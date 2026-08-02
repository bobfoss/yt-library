from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from yt_library import core, server, workers
from yt_library.config import load_config

from tests.support import migrated_connection


class AdminServerTests(unittest.TestCase):
    def test_sort_preference_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/settings/sort?context=liked-videos&value=most_watched"
            handler.config_data = config
            handler.send_json = Mock()

            handler.do_POST()

            expected = {"liked-videos": "most_watched"}
            self.assertEqual(config["sort_preferences"], expected)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["sort_preferences"], expected)
            handler.send_json.assert_called_once_with(
                {
                    "ok": True,
                    "context": "liked-videos",
                    "sort": "most_watched",
                }
            )

    def test_sort_preference_rejects_mismatched_regime(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/sort?context=playlist&value=newest"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["sort_preferences"], {})
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_page_size_preference_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/settings/page-size?value=250"
            handler.config_data = config
            handler.send_json = Mock()

            handler.do_POST()

            self.assertEqual(config["page_size"], 250)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["page_size"], 250)
            handler.send_json.assert_called_once_with({"ok": True, "pageSize": 250})

    def test_page_size_preference_rejects_unknown_values(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/page-size?value=42"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["page_size"], 100)
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_partial_completion_minimum_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/settings/partial-completion-minimum?value=65"
            handler.config_data = config
            handler.send_json = Mock()

            handler.do_POST()

            self.assertEqual(config["partial_completion_min_percent"], 65)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["partial_completion_min_percent"], 65)
            handler.send_json.assert_called_once_with(
                {"ok": True, "partialCompletionMinPercent": 65}
            )

    def test_partial_completion_minimum_rejects_out_of_range_value(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/partial-completion-minimum?value=100"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["partial_completion_min_percent"], 1)
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_filter_preference_saves_and_removes_sparse_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.config_data = config
            handler.send_json = Mock()

            handler.path = (
                "/api/settings/filter-preference?"
                "key=completion.partial_below_minimum&enabled=1"
            )
            handler.do_POST()

            expected = {"completion.partial_below_minimum": True}
            self.assertEqual(config["filter_preferences"], expected)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["filter_preferences"], expected)
            handler.send_json.assert_called_with(
                {
                    "ok": True,
                    "key": "completion.partial_below_minimum",
                    "enabled": True,
                    "filterPreferences": expected,
                }
            )

            handler.path = (
                "/api/settings/filter-preference?"
                "key=completion.partial_below_minimum&enabled=0"
            )
            handler.do_POST()

            self.assertEqual(config["filter_preferences"], {})
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["filter_preferences"], {})

    def test_filter_preference_rejects_unknown_key(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/filter-preference?key=videos.public&enabled=1"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["filter_preferences"], {})
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_layout_preference_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/settings/layout?context=history&value=detailed"
            handler.config_data = config
            handler.send_json = Mock()

            handler.do_POST()

            self.assertEqual(config["history_card_layout"], "detailed")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["history_card_layout"], "detailed")
            self.assertEqual(payload["search_card_layout"], "grid")
            handler.send_json.assert_called_once_with(
                {"ok": True, "context": "history", "layout": "detailed"}
            )

            handler.path = "/api/settings/layout?context=playlist&value=compact"
            handler.send_json.reset_mock()
            handler.do_POST()

            self.assertEqual(config["playlist_card_layout"], "compact")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["playlist_card_layout"], "compact")
            handler.send_json.assert_called_once_with(
                {"ok": True, "context": "playlist", "layout": "compact"}
            )

    def test_layout_preference_rejects_unknown_values(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/layout?context=search&value=wide"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["search_card_layout"], "grid")
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_dispatch_settings_save_config_and_reconfigure_live_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/dispatch-settings?" + urllib.parse.urlencode(
                {
                    "dispatch_mode": "throttle",
                    "job_dispatch_delay_seconds": "5",
                    "request_delay_min_seconds": "6",
                    "request_delay_max_seconds": "10",
                    "youtube_max_in_flight": "8",
                    "archivarix_max_in_flight": "2",
                }
            )
            handler.config_data = config
            handler.send_json = Mock()

            with (
                patch.object(
                    server.WORKER_QUEUE_DISPATCHER,
                    "update_dispatch_settings",
                ) as update_settings,
                patch("yt_library.server.configure_request_pacing") as configure_pacing,
            ):
                handler.do_POST()

            update_settings.assert_called_once_with("throttle", 5.0, 8, 2)
            configure_pacing.assert_called_once_with(config)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["dispatch_mode"], "throttle")
            self.assertEqual(payload["job_dispatch_delay_seconds"], 5.0)
            self.assertEqual(payload["request_delay_min_seconds"], 6.0)
            self.assertEqual(payload["request_delay_max_seconds"], 10.0)
            self.assertEqual(payload["youtube_max_in_flight"], 8)
            self.assertEqual(payload["archivarix_max_in_flight"], 2)
            response = handler.send_json.call_args.args[0]
            self.assertEqual(
                response["dispatchSettings"]["dispatch_mode"],
                "throttle",
            )
            self.assertEqual(
                response["dispatchSettings"]["effective_job_dispatch_delay_seconds"],
                0.0,
            )

    def test_dispatch_settings_reject_throttle_maximum_below_minimum(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/admin/dispatch-settings?" + urllib.parse.urlencode(
            {
                "dispatch_mode": "throttle",
                "job_dispatch_delay_seconds": "5",
                "request_delay_min_seconds": "10",
                "request_delay_max_seconds": "6",
                "youtube_max_in_flight": "8",
                "archivarix_max_in_flight": "2",
            }
        )
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        response = handler.send_json.call_args.args[0]
        self.assertIn("maximum", response["error"])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_admin_settings_save_proxy_and_schedule_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            db_path = Path(temp_dir) / "library.sqlite3"
            config = load_config(config_path)
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    core.set_external_service_block(
                        conn,
                        "proxy",
                        "proxy_unavailable",
                        "Old proxy failed",
                    )
            finally:
                conn.close()
            request_restart = Mock(return_value=True)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/settings?" + urllib.parse.urlencode(
                {
                    "display_timezone": "America/Los_Angeles",
                    "use_proxy": "1",
                    "proxy": "socks5h://127.0.0.1:1081",
                }
            )
            handler.db_path = db_path
            handler.config_data = config
            handler.service_started_at = "2026-07-28T12:00:00Z"
            handler.restart_pending = lambda: request_restart.called
            handler.request_restart = request_restart
            handler.send_json = Mock()

            handler.do_POST()

            request_restart.assert_called_once_with()
            self.assertEqual(config["display_timezone"], "America/Los_Angeles")
            self.assertTrue(config["use_proxy"])
            self.assertEqual(config["proxy"], "socks5h://127.0.0.1:1081")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["use_proxy"])
            self.assertEqual(payload["proxy"], "socks5h://127.0.0.1:1081")
            conn = core.connect(db_path)
            try:
                self.assertFalse(core.external_service_block(conn, "proxy")["blocked"])
            finally:
                conn.close()
            response = handler.send_json.call_args.args[0]
            self.assertTrue(response["restartScheduled"])
            self.assertEqual(response["service"]["status"], "restarting")

    def test_admin_settings_reject_enabled_proxy_without_an_address(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = (
            "/api/admin/settings?"
            + urllib.parse.urlencode(
                {
                    "display_timezone": "UTC",
                    "use_proxy": "1",
                    "proxy": "",
                }
            )
        )
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        response = handler.send_json.call_args.args[0]
        self.assertIn("SOCKS5 proxy URL", response["error"])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_admin_advanced_setting_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/advanced?enabled=1"
            handler.config_data = config
            handler.send_json = Mock()

            handler.do_POST()

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            response = handler.send_json.call_args.args[0]

        self.assertTrue(config["admin_advanced"])
        self.assertTrue(payload["admin_advanced"])
        self.assertTrue(response["settings"]["adminAdvanced"])

    def test_update_schedule_endpoint_saves_and_updates_live_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/update-schedule?frequency=hourly&at=04%3A30"
            handler.config_data = config
            handler.send_json = Mock()

            with patch.object(
                server.UPDATE_SCHEDULER,
                "schedule_changed",
            ) as schedule_changed:
                handler.do_POST()

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            response = handler.send_json.call_args.args[0]

        self.assertEqual(payload["update_frequency"], "hourly")
        self.assertEqual(payload["update_time"], "04:30")
        self.assertEqual(response["settings"]["updateFrequency"], "hourly")
        self.assertEqual(response["settings"]["updateTime"], "04:30")
        schedule_changed.assert_called_once_with(config)

    def test_update_schedule_endpoint_rejects_invalid_time(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/admin/update-schedule?frequency=daily&at=25%3A00"
        handler.config_data = load_config(Path("missing-test-config.json"))
        handler.send_json = Mock()

        handler.do_POST()

        response = handler.send_json.call_args.args[0]
        self.assertIn("HH:MM", response["error"])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_update_schedule_endpoint_rejects_invalid_frequency(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/admin/update-schedule?frequency=weekly&at=04%3A30"
        handler.config_data = load_config(Path("missing-test-config.json"))
        handler.send_json = Mock()

        handler.do_POST()

        response = handler.send_json.call_args.args[0]
        self.assertIn("frequency", response["error"])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_scheduled_update_queues_incremental_work_and_starts_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            cookie_file = Path(temp_dir) / "cookies.txt"
            video_thumbs = Path(temp_dir) / "video_thumbs"
            config = load_config(Path(temp_dir) / "config.json")

            with patch.object(
                server.WORKER_QUEUE_DISPATCHER,
                "start",
                return_value={"started": True},
            ) as start_dispatcher:
                result = server.enqueue_library_update(
                    db_path,
                    cookie_file,
                    video_thumbs,
                    config,
                    scheduled=True,
                )

            conn = core.connect(db_path)
            try:
                queue_rows = conn.execute(
                    """
                    SELECT subject_key, task_type, manual
                    FROM worker_queue
                    ORDER BY subject_key
                    """
                ).fetchall()
                log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(result["dispatcher"], {"started": True})
        self.assertEqual(result["queue"]["inserted"], 4)
        self.assertEqual(
            {row["subject_key"] for row in queue_rows},
            {"account:sync", "history:recent", "playlist:discover-current", "playlist:scan:LL"},
        )
        self.assertEqual(tuple(log)[0], "queue info")
        self.assertIn("Scheduled update queued", tuple(log)[1])
        start_dispatcher.assert_called_once_with(
            db_path,
            cookie_file,
            video_thumbs,
            config,
        )

    def test_proxy_retry_clears_hold_and_starts_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.set_external_service_block(
                        conn,
                        "proxy",
                        "proxy_unavailable",
                        "SOCKS5 proxy is unavailable",
                    )
            finally:
                conn.close()

            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/proxy/retry"
            handler.db_path = db_path
            handler.cookie_file = Path(temp_dir) / "youtube-cookies.txt"
            handler.video_thumbs = Path(temp_dir) / "video-thumbs"
            handler.config_data = load_config(Path(temp_dir) / "config.json")
            handler.send_json = Mock()

            blocked_when_started: list[bool] = []

            def recover_proxy(*_args, **_kwargs):
                conn = core.connect(db_path)
                try:
                    blocked_when_started.append(
                        core.external_service_block(conn, "proxy")["blocked"]
                    )
                    with conn:
                        core.clear_external_service_block(conn, "proxy")
                finally:
                    conn.close()
                return {"started": True}

            with patch.object(
                workers.WORKER_QUEUE_DISPATCHER,
                "start",
                side_effect=recover_proxy,
            ) as start:
                handler.do_POST()

            self.assertEqual(blocked_when_started, [True])
            start.assert_called_once_with(
                db_path,
                handler.cookie_file,
                handler.video_thumbs,
                handler.config_data,
            )
            conn = core.connect(db_path)
            try:
                self.assertFalse(core.external_service_block(conn, "proxy")["blocked"])
                retry_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(retry_log["level"], "queue info")
                self.assertIn("Proxy retry requested", retry_log["message"])
            finally:
                conn.close()
            response = handler.send_json.call_args.args[0]
            self.assertTrue(response["ok"])
            self.assertTrue(response["cleared"])
            self.assertFalse(response["proxyBlock"]["blocked"])

    def test_feature_backfill_endpoint_queues_selected_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "backfillvid",
                        title="Backfill video",
                        availability="public",
                    )
            finally:
                conn.close()

            handler = object.__new__(server.LibraryHandler)
            handler.path = (
                "/api/admin/feature-backfill/start"
                "?kind=video_visibility&limit=1"
            )
            handler.db_path = db_path
            handler.cookie_file = Path(temp_dir) / "cookies.txt"
            handler.video_thumbs = Path(temp_dir) / "video_thumbs"
            handler.config_data = {}
            handler.send_json = Mock()

            with patch.object(
                server.WORKER_QUEUE_DISPATCHER,
                "start",
                return_value={"started": True},
            ) as start_dispatcher:
                handler.do_POST()

            response = handler.send_json.call_args.args[0]
            conn = core.connect(db_path)
            try:
                queued = conn.execute(
                    """
                    SELECT video_id
                    FROM worker_queue
                    WHERE worker_type = 'metadata'
                    """
                ).fetchone()["video_id"]
            finally:
                conn.close()

        self.assertEqual(response["queue"]["kind"], "video_visibility")
        self.assertEqual(response["queue"]["inserted"], 1)
        self.assertEqual(queued, "backfillvid")
        start_dispatcher.assert_called_once()

    def test_initialize_endpoint_queues_initial_work_and_starts_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()

            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/initialize"
            handler.db_path = db_path
            handler.cookie_file = Path(temp_dir) / "cookies.txt"
            handler.video_thumbs = Path(temp_dir) / "video_thumbs"
            handler.config_data = {}
            handler.send_json = Mock()

            with patch.object(
                server.WORKER_QUEUE_DISPATCHER,
                "start",
                return_value={"started": True},
            ) as start_dispatcher:
                handler.do_POST()

            response = handler.send_json.call_args.args[0]
            conn = core.connect(db_path)
            try:
                subjects = {
                    row["subject_key"]
                    for row in conn.execute("SELECT subject_key FROM worker_queue")
                }
            finally:
                conn.close()

        self.assertTrue(response["ok"])
        self.assertFalse(response["queue"]["had_data"])
        self.assertEqual(response["queue"]["inserted"], 3)
        self.assertEqual(subjects, {"account:sync", "history:verify", "playlist:scan:LL"})
        start_dispatcher.assert_called_once_with(
            db_path,
            handler.cookie_file,
            handler.video_thumbs,
            handler.config_data,
        )

    def test_fetch_history_also_queues_personal_activity_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()

            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/live-history/start"
            handler.db_path = db_path
            handler.cookie_file = Path(temp_dir) / "cookies.txt"
            handler.video_thumbs = Path(temp_dir) / "video_thumbs"
            handler.config_data = {}
            handler.send_json = Mock()

            with patch.object(
                server.WORKER_QUEUE_DISPATCHER,
                "start",
                return_value={"started": True},
            ) as start_dispatcher:
                handler.do_POST()

            conn = core.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT subject_key, current_title
                    FROM worker_queue
                    ORDER BY subject_key
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(
            {(row["subject_key"], row["current_title"]) for row in rows},
            {
                ("account:sync", "Collect personal activity"),
                ("history:recent", "Fetch history"),
            },
        )
        start_dispatcher.assert_called_once_with(
            db_path,
            handler.cookie_file,
            handler.video_thumbs,
            handler.config_data,
        )

    def test_service_replacement_uses_dedicated_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(server, "ROOT", root),
                patch.object(server.subprocess, "Popen") as popen,
            ):
                server.launch_service_replacement()

            kwargs = popen.call_args.kwargs
            self.assertEqual(
                Path(kwargs["stdout"].name),
                root / ".codex" / "service-logs" / "yt-library.out.log",
            )
            self.assertEqual(
                Path(kwargs["stderr"].name),
                root / ".codex" / "service-logs" / "yt-library.err.log",
            )
            self.assertTrue(kwargs["stdout"].closed)
            self.assertTrue(kwargs["stderr"].closed)


if __name__ == "__main__":
    unittest.main()
