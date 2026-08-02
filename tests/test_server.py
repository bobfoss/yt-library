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

    def test_admin_template_exposes_service_and_proxy_controls(self) -> None:
        self.assertIn('id="initializeLibrary"', server.ADMIN_HTML)
        self.assertIn('id="initializeControls" class="controls initialize-controls"', server.ADMIN_HTML)
        self.assertIn('id="initializeStatus"', server.ADMIN_HTML)
        self.assertIn("'/api/admin/initialize'", server.ADMIN_HTML)
        self.assertIn("usually take a significant amount of time", server.ADMIN_HTML)
        self.assertIn("initialize-needed", server.ADMIN_HTML)
        self.assertIn("initialize-complete", server.ADMIN_HTML)
        self.assertIn(".initialize-controls.initialization-complete", server.ADMIN_HTML)
        self.assertLess(
            server.ADMIN_HTML.index('id="initializeLibrary"'),
            server.ADMIN_HTML.index("<h2>Videos</h2>"),
        )
        self.assertIn('id="themeToggle"', server.ADMIN_HTML)
        self.assertIn('id="advancedToggle"', server.ADMIN_HTML)
        self.assertIn('aria-label="Show advanced admin controls"', server.ADMIN_HTML)
        self.assertIn("body:not(.advanced-enabled) .advanced-only", server.ADMIN_HTML)
        self.assertIn("'/api/admin/advanced'", server.ADMIN_HTML)
        self.assertEqual(
            server.ADMIN_HTML.count('<section class="workstream advanced-only">'),
            4,
        )
        self.assertIn('<fieldset class="dispatch-settings advanced-only">', server.ADMIN_HTML)
        self.assertIn('<div class="controls capacity-controls advanced-only">', server.ADMIN_HTML)
        self.assertIn('<div class="controls history-controls advanced-only">', server.ADMIN_HTML)
        self.assertIn('id="updateLibrary"', server.ADMIN_HTML)
        self.assertIn('id="updateDaily"', server.ADMIN_HTML)
        self.assertIn('id="updateTime"', server.ADMIN_HTML)
        self.assertIn('id="updateScheduleStatus"', server.ADMIN_HTML)
        self.assertIn("'/api/admin/update/start'", server.ADMIN_HTML)
        self.assertIn("'/api/admin/update-schedule'", server.ADMIN_HTML)
        self.assertLess(
            server.ADMIN_HTML.index("<h2>Update</h2>"),
            server.ADMIN_HTML.index("<h2>Videos</h2>"),
        )
        self.assertIn('aria-label="Use dark theme"', server.ADMIN_HTML)
        self.assertIn('<span>Light</span>', server.ADMIN_HTML)
        self.assertIn('<span>Dark</span>', server.ADMIN_HTML)
        self.assertIn('id="serviceStatus"', server.ADMIN_HTML)
        self.assertIn("`Running${service.pid ? ` (${service.pid})` : ''}`", server.ADMIN_HTML)
        self.assertIn('id="restartService"', server.ADMIN_HTML)
        self.assertLess(
            server.ADMIN_HTML.index('id="serviceStatus"'),
            server.ADMIN_HTML.index('id="advancedToggle"'),
        )
        self.assertLess(
            server.ADMIN_HTML.index('id="restartService"'),
            server.ADMIN_HTML.index('id="advancedToggle"'),
        )
        self.assertIn('id="useProxy"', server.ADMIN_HTML)
        self.assertIn('id="proxyUrl"', server.ADMIN_HTML)
        self.assertIn('id="retryProxy"', server.ADMIN_HTML)
        self.assertIn('id="proxyBlock"', server.ADMIN_HTML)
        self.assertIn('<option value="queue">Queue</option>', server.ADMIN_HTML)
        self.assertIn("startsWith('queue ')", server.ADMIN_HTML)
        self.assertIn('id="logPanel"', server.ADMIN_HTML)
        self.assertIn("/api/admin/logs?${params}", server.ADMIN_HTML)
        self.assertIn("fields.logPanel.addEventListener('scroll', loadMoreLogsIfNeeded", server.ADMIN_HTML)
        self.assertNotIn("logState.rows.slice(0, 120)", server.ADMIN_HTML)
        self.assertIn('id="saveSettings"', server.ADMIN_HTML)
        self.assertIn('aria-label="Cookie files"', server.ADMIN_HTML)
        self.assertNotIn('data-advanced-tab="controls"', server.ADMIN_HTML)
        self.assertEqual(server.ADMIN_HTML.count('data-advanced-tab="'), 3)
        self.assertLess(
            server.ADMIN_HTML.index("<h2>Update</h2>"),
            server.ADMIN_HTML.index("<h2>Cookies</h2>"),
        )
        self.assertLess(
            server.ADMIN_HTML.index("<h2>Cookies</h2>"),
            server.ADMIN_HTML.index("<h2>Videos</h2>"),
        )
        self.assertNotIn('id="syncAccountDates"', server.ADMIN_HTML)
        self.assertNotIn("'/api/admin/account/start'", server.ADMIN_HTML)
        self.assertIn(
            'class="advanced-tab-pane cookie-editor" data-advanced-pane="youtube">',
            server.ADMIN_HTML,
        )
        self.assertIn("<legend>Dispatch mode</legend>", server.ADMIN_HTML)
        self.assertIn('id="dispatchModeDelay"', server.ADMIN_HTML)
        self.assertIn('id="dispatchModeThrottle"', server.ADMIN_HTML)
        self.assertIn('id="jobDispatchDelay"', server.ADMIN_HTML)
        self.assertIn('id="requestDelayMin"', server.ADMIN_HTML)
        self.assertIn('id="requestDelayMax"', server.ADMIN_HTML)
        self.assertIn('id="youtubeMaxInFlight"', server.ADMIN_HTML)
        self.assertIn('id="archivarixMaxInFlight"', server.ADMIN_HTML)
        self.assertIn("syncDispatchModeInputs();", server.ADMIN_HTML)
        self.assertIn("field.addEventListener('blur', flushDispatchSettingsSave);", server.ADMIN_HTML)
        self.assertIn("including requests made by yt-dlp", server.ADMIN_HTML)
        self.assertEqual(server.ADMIN_HTML.count("<th>ID</th>"), 2)
        self.assertNotIn("<th>Video ID</th>", server.ADMIN_HTML)
        self.assertIn("return row.channel_id || row.video_id || '';", server.ADMIN_HTML)
        self.assertNotIn('id="historyFetchDaily"', server.ADMIN_HTML)
        self.assertNotIn("'/api/admin/history-schedule'", server.ADMIN_HTML)
        self.assertIn('type="time" value="03:00" disabled', server.ADMIN_HTML)

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
            handler.path = "/api/admin/update-schedule?enabled=1&at=04%3A30"
            handler.config_data = config
            handler.send_json = Mock()

            with patch.object(
                server.UPDATE_SCHEDULER,
                "schedule_changed",
            ) as schedule_changed:
                handler.do_POST()

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            response = handler.send_json.call_args.args[0]

        self.assertTrue(payload["update_daily"])
        self.assertEqual(payload["update_time"], "04:30")
        self.assertTrue(response["settings"]["updateDaily"])
        self.assertEqual(response["settings"]["updateTime"], "04:30")
        schedule_changed.assert_called_once_with(config)

    def test_update_schedule_endpoint_rejects_invalid_time(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/admin/update-schedule?enabled=1&at=25%3A00"
        handler.config_data = load_config(Path("missing-test-config.json"))
        handler.send_json = Mock()

        handler.do_POST()

        response = handler.send_json.call_args.args[0]
        self.assertIn("HH:MM", response["error"])
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
        self.assertIn("return row.playlist_id || row.video_id || '';", server.ADMIN_HTML)
        self.assertIn("identifier: log.display_id || log.playlist_id || ''", server.ADMIN_HTML)
        self.assertIn(".id-col { width: 280px; }", server.ADMIN_HTML)
        self.assertIn(".subject-col { width: 490px; }", server.ADMIN_HTML)
        self.assertIn(".queue-source-col { width: 280px; }", server.ADMIN_HTML)
        self.assertIn(".log-panel > table { display: none; }", server.ADMIN_HTML)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto;", server.ADMIN_HTML)
        self.assertIn('class="message log-message-cell"', server.ADMIN_HTML)
        self.assertEqual(server.ADMIN_HTML.count('<col class="id-col">'), 4)
        self.assertEqual(server.ADMIN_HTML.count('<col class="subject-col">'), 4)
        self.assertEqual(server.ADMIN_HTML.count('<col class="queue-source-col">'), 2)
        self.assertIn("const redundantSuffix = ` (via ${log.identifier})`;", server.ADMIN_HTML)
        self.assertIn(':root[data-theme="light"]', server.ADMIN_HTML)
        self.assertIn(':root[data-theme="light"]', server.INDEX_HTML)
        self.assertIn('<script src="/theme.js"></script>', server.INDEX_HTML)
        self.assertIn('.video-card-channel { margin-top: 0; margin-bottom: 7px; font-weight: 650; }', server.INDEX_HTML)
        self.assertNotIn('.video-card-channel .creator-link { color:', server.INDEX_HTML)
        self.assertEqual(server.INDEX_HTML.count('<input type="checkbox" data-meta-all-filter='), 1)
        self.assertEqual(server.INDEX_HTML.count('<input type="checkbox" data-meta-child-filter='), 2)
        self.assertIn("const videoMetaFilterDefinitions = [", server.INDEX_HTML)
        self.assertIn("const reactionMetaFilterDefinitions = [", server.INDEX_HTML)
        self.assertLess(
            server.ADMIN_HTML.index('id="fetchVideoMetadata"'),
            server.ADMIN_HTML.index('id="videoMetadataStaleDays"'),
        )
        self.assertLess(
            server.ADMIN_HTML.index('id="fetchChannelMetadata"'),
            server.ADMIN_HTML.index('id="channelMetadataStaleDays"'),
        )
        self.assertLess(
            server.ADMIN_HTML.index('id="channelMetadataForce"'),
            server.ADMIN_HTML.index('id="backfillChannelAccount"'),
        )
        self.assertIn('id="backfillVideoVisibility"', server.ADMIN_HTML)
        self.assertIn('id="backfillPlaylistMetadata"', server.ADMIN_HTML)
        self.assertIn('id="backfillChannelAccount"', server.ADMIN_HTML)
        self.assertIn("/api/admin/feature-backfill/start", server.ADMIN_HTML)
        self.assertIn("reactions: { none: true, liked: true, disliked: true }", server.INDEX_HTML)
        self.assertIn("partial_below_minimum: defaultPartialBelowMinimumEnabled()", server.INDEX_HTML)
        self.assertIn(
            "membership: { member: true, non_member: true }",
            server.INDEX_HTML,
        )
        self.assertIn(
            "channelSubscription: { subscribed: true, non_subscribed: true }",
            server.INDEX_HTML,
        )
        self.assertIn("terminated: filterPreferenceEnabled(filterPreferenceKeys.terminatedChannels)", server.INDEX_HTML)
        self.assertIn(
            "playlistVisibility: { private: true, public: true, unlisted: true, unknown: true }",
            server.INDEX_HTML,
        )
        self.assertIn(
            "playlistOwnership: { mine: true, others: true, ownership_unknown: true }",
            server.INDEX_HTML,
        )
        self.assertIn("removed: filterPreferenceEnabled(filterPreferenceKeys.removedPlaylists)", server.INDEX_HTML)
        self.assertIn("const searchOptInMetaFilters = [", server.INDEX_HTML)
        self.assertIn(
            "groupName: 'videos', key: 'unavailable', paramName: 'unavailable'",
            server.INDEX_HTML,
        )
        self.assertIn(
            "groupName: 'completion', key: 'partial_below_minimum', paramName: 'partial_below'",
            server.INDEX_HTML,
        )
        self.assertIn(
            "groupName: 'playlistStatus', key: 'removed', paramName: 'removed'",
            server.INDEX_HTML,
        )
        self.assertIn(
            "groupName: 'channelStatus', key: 'terminated', paramName: 'terminated'",
            server.INDEX_HTML,
        )
        self.assertIn(
            "if (searchMetaVisibility[groupName][key]) params.set(paramName, '1');",
            server.INDEX_HTML,
        )
        self.assertIn("resetSearchMetaVisibility();", server.INDEX_HTML)
        self.assertIn(
            "params.get(paramName) === '1' || legacySelected",
            server.INDEX_HTML,
        )
        self.assertIn("Search For", server.INDEX_HTML)
        self.assertIn('id="search-for-filters"', server.INDEX_HTML)
        self.assertNotIn("playlist_videos\" checked> Playlist videos", server.INDEX_HTML)
        self.assertIn("video_reaction: metaFilterParamValue(searchMetaVisibility.reactions)", server.INDEX_HTML)
        self.assertIn(
            "video_completion: metaFilterParamValue(searchMetaVisibility.completion)",
            server.INDEX_HTML,
        )
        self.assertIn(
            "video_completion_min_percent: String(partialCompletionMinimumPercent)",
            server.INDEX_HTML,
        )
        self.assertNotIn("params.set('vmin'", server.INDEX_HTML)
        self.assertIn("pageConfig.partialCompletionMinPercent", server.INDEX_HTML)
        self.assertIn("/api/settings/partial-completion-minimum", server.INDEX_HTML)
        self.assertIn("pageConfig.filterPreferences", server.INDEX_HTML)
        self.assertIn("/api/settings/filter-preference", server.INDEX_HTML)
        self.assertIn("saveSearchOptInPreferences(searchKindFacetKeys(searchKindFilter));", server.INDEX_HTML)
        self.assertIn("saveSearchOptInPreferences([facetKey]);", server.INDEX_HTML)
        self.assertIn('aria-label="Minimum partial completion percentage"', server.INDEX_HTML)
        self.assertIn('class="completion-partial-toggle"', server.INDEX_HTML)
        self.assertIn("searchForFilters.addEventListener('input', scheduleCompletionMinimumInput)", server.INDEX_HTML)
        self.assertIn(
            "video_playlist_membership: metaFilterParamValue(searchMetaVisibility.membership)",
            server.INDEX_HTML,
        )
        self.assertIn(
            "channel_subscription: metaFilterParamValue(searchMetaVisibility.channelSubscription)",
            server.INDEX_HTML,
        )
        self.assertIn(
            "channel_status: metaFilterParamValue(searchMetaVisibility.channelStatus)",
            server.INDEX_HTML,
        )
        self.assertIn(
            "const channelSubscriptionMetaFilterDefinitions = [",
            server.INDEX_HTML,
        )
        self.assertIn("const channelStatusMetaFilterDefinitions = [", server.INDEX_HTML)
        self.assertIn(
            "const playlistVisibilityMetaFilterDefinitions = [",
            server.INDEX_HTML,
        )
        self.assertIn(
            "const playlistOwnershipMetaFilterDefinitions = [",
            server.INDEX_HTML,
        )
        self.assertIn(
            "const playlistStatusMetaFilterDefinitions = [",
            server.INDEX_HTML,
        )
        self.assertIn("function completionMetaFilterDefinitions(", server.INDEX_HTML)
        self.assertIn("partial_below_minimum", server.INDEX_HTML)
        self.assertIn("label: `partial \\u2264 ${boundedMinimum - 1}%`", server.INDEX_HTML)
        self.assertIn("!allMetaFilterChildrenChecked(metaAllFilter)", server.INDEX_HTML)
        self.assertIn(
            "const playlistMembershipMetaFilterDefinitions = [",
            server.INDEX_HTML,
        )
        self.assertIn("function metaFilterControlsHtml({", server.INDEX_HTML)
        self.assertIn(
            "function searchMetaFiltersHtml(",
            server.INDEX_HTML,
        )
        self.assertIn("filterAttribute: 'search-meta-filter'", server.INDEX_HTML)
        self.assertIn("groupName: `search-${key}`", server.INDEX_HTML)
        self.assertIn('data-search-meta-progress="${kind}"', server.INDEX_HTML)
        self.assertIn("flex: 0 0 1.4em", server.INDEX_HTML)
        self.assertIn("function animateProgressDots(update)", server.INDEX_HTML)
        self.assertIn("function showSearchMetaProgress(groupName)", server.INDEX_HTML)
        self.assertIn(
            "pendingSearchMetaGroups.add(progressGroup);\n"
            "      showSearchHeaderProgress();",
            server.INDEX_HTML,
        )
        self.assertIn(
            "const progressGroup = searchKindForFacet(groupName);",
            server.INDEX_HTML,
        )
        self.assertIn(
            "const active = pendingSearchMetaGroups.has(dots.dataset.searchMetaProgress);",
            server.INDEX_HTML,
        )
        self.assertIn(
            "if (searchMetaProgressTimer === null) {",
            server.INDEX_HTML,
        )
        show_progress_start = server.INDEX_HTML.index(
            "function showSearchMetaProgress(groupName)"
        )
        show_progress_end = server.INDEX_HTML.index(
            "function stopSearchHeaderProgress(progressToken = null)"
        )
        self.assertNotIn(
            "stopSearchMetaProgress();",
            server.INDEX_HTML[show_progress_start:show_progress_end],
        )
        self.assertIn("pendingSearchMetaGroups.clear();", server.INDEX_HTML)
        self.assertIn("allLabel: 'Availability'", server.INDEX_HTML)
        self.assertIn("allLabel: 'Reactions'", server.INDEX_HTML)
        self.assertIn("allLabel: 'Completion'", server.INDEX_HTML)
        self.assertIn("allLabel: 'Playlist membership'", server.INDEX_HTML)
        self.assertIn("allLabel: 'Subscription'", server.INDEX_HTML)
        self.assertIn("allLabel: 'Status'", server.INDEX_HTML)
        self.assertIn("kindHtml('Videos', 'videos'", server.INDEX_HTML)
        self.assertIn(
            "const searchVideoFacetKeys = ['videos', 'reactions', 'completion', 'membership'];",
            server.INDEX_HTML,
        )
        self.assertIn(
            "const searchChannelFacetKeys = ['channelSubscription', 'channelStatus'];",
            server.INDEX_HTML,
        )
        self.assertIn(
            "const searchPlaylistFacetKeys = ['playlistVisibility', 'playlistOwnership', 'playlistStatus'];",
            server.INDEX_HTML,
        )
        self.assertIn("function setSearchKindFilter(kind, checked)", server.INDEX_HTML)
        self.assertIn(
            "root.querySelectorAll(`[data-meta-child-filter=\"${groupName}\"]`)",
            server.INDEX_HTML,
        )
        self.assertIn("function syncSearchKindFilter(kind)", server.INDEX_HTML)
        self.assertIn("function restoreEmptySearchKindFacets(facetKey)", server.INDEX_HTML)
        self.assertIn(
            "Object.assign(searchMetaVisibility[siblingKey], defaults);",
            server.INDEX_HTML,
        )
        self.assertIn(
            "input.checked = Boolean(defaults[filterName]);",
            server.INDEX_HTML,
        )
        self.assertIn('data-search-kind-filter="${kind}"', server.INDEX_HTML)
        self.assertIn(
            '<span class="count">${kindEnabled ? filterCountText(count) : \'\'}</span>',
            server.INDEX_HTML,
        )
        self.assertIn("allLabel: 'Visibility', kind: 'playlists'", server.INDEX_HTML)
        self.assertIn("allLabel: 'Ownership', kind: 'playlists'", server.INDEX_HTML)
        self.assertIn("allLabel: 'Status', kind: 'playlists'", server.INDEX_HTML)
        self.assertIn(
            "searchForFilters.querySelectorAll(`[data-search-kind-facet=\"${kind}\"]`)",
            server.INDEX_HTML,
        )
        count_position = server.INDEX_HTML.index(
            '<span class="count">${kindEnabled ? filterCountText(count) : \'\'}</span>'
        )
        progress_position = server.INDEX_HTML.index(
            '<span class="search-meta-progress" data-search-meta-progress="${kind}"',
            count_position,
        )
        self.assertLess(count_position, progress_position)
        self.assertIn("function renderSearchMetaFilters({", server.INDEX_HTML)
        self.assertIn(
            "searchForFilters.innerHTML = searchMetaFiltersHtml(",
            server.INDEX_HTML,
        )
        self.assertIn("renderSearchMetaFilters(payload);", server.INDEX_HTML)
        self.assertIn(
            "return count === null || count === undefined ? ''",
            server.INDEX_HTML,
        )
        initial_filter_position = server.INDEX_HTML.rindex("renderSearchMetaFilters();")
        initial_load_position = server.INDEX_HTML.index(
            "loadData().catch(error => {", initial_filter_position
        )
        self.assertLess(initial_filter_position, initial_load_position)
        self.assertIn("searchForFilters.addEventListener('change', handleMetaChange);", server.INDEX_HTML)
        self.assertNotIn(
            ".filter(({ counts }) => Number(counts?.total || 0) > 0)",
            server.INDEX_HTML,
        )
        self.assertIn('id="search-progress-status"', server.INDEX_HTML)
        self.assertIn(
            '<div class="toolbar-heading">\n'
            '            <h2 id="view-title" class="title"></h2>\n'
            '            <div id="search-progress-status"',
            server.INDEX_HTML,
        )
        self.assertIn("function progressMessageAnimation(container, labelText)", server.INDEX_HTML)
        self.assertIn("function showSearchHeaderProgress()", server.INDEX_HTML)
        self.assertIn(
            "progressMessageAnimation(searchProgressStatus, 'Loading')",
            server.INDEX_HTML,
        )
        self.assertIn("loadData({ preserveSearchContent })", server.INDEX_HTML)
        self.assertIn("}).finally(stopSearchHeaderProgress);", server.INDEX_HTML)
        self.assertIn("await render();", server.INDEX_HTML)
        self.assertIn(
            "const searchKindFilter = target.dataset.searchKindFilter;",
            server.INDEX_HTML,
        )
        self.assertIn("syncSearchKindFilter(searchKindForFacet(facetKey));", server.INDEX_HTML)
        self.assertIn(
            "if (target.checked) restoreEmptySearchKindFacets(facetKey);",
            server.INDEX_HTML,
        )
        self.assertIn(
            "if (target.checked) restoreEmptySearchKindFacets(groupName);",
            server.INDEX_HTML,
        )
        self.assertIn("showSearchMetaProgress(groupName);", server.INDEX_HTML)
        self.assertIn(
            "if (selected !== '__search__') {\n"
            "        searchResultsRendered = false;\n"
            "        stopSearchMetaProgress();\n"
            "      }",
            server.INDEX_HTML,
        )
        self.assertLess(
            server.INDEX_HTML.index("kindHtml('Playlists', 'playlists'"),
            server.INDEX_HTML.index("kindHtml('Channels', 'channels'"),
        )
        self.assertLess(
            server.INDEX_HTML.index("const playlistSection = sectionFor('Playlists');"),
            server.INDEX_HTML.index("const channelSection = sectionFor('Channels');"),
        )
        self.assertIn(
            "['videos', 'reactions', 'completion', 'membership', 'playlistVisibility', 'playlistOwnership', 'playlistStatus', 'channelSubscription', 'channelStatus']",
            server.INDEX_HTML,
        )
        self.assertLess(
            server.INDEX_HTML.index('id="view-meta"'),
            server.INDEX_HTML.index('id="refresh"'),
        )
        self.assertIn("const searchPresetDefinitions = {", server.INDEX_HTML)
        self.assertIn("videos: { kind: 'videos', sort: 'newest' }", server.INDEX_HTML)
        self.assertIn("function activateSearchPreset(preset, groupKey = '')", server.INDEX_HTML)
        self.assertIn("function activateSearchFromHistory({ resetMetaVisibility = false } = {})", server.INDEX_HTML)
        self.assertIn("function syncSearchFiltersForSelection()", server.INDEX_HTML)
        self.assertIn('id="search-filters" class="filters"', server.INDEX_HTML)
        self.assertIn(".filters.view-inactive { opacity: .42; }", server.INDEX_HTML)
        self.assertIn(
            "const activatedFromHistory = searchFilterInteraction",
            server.INDEX_HTML,
        )
        self.assertIn("syncSearchHashAndRender(!activatedFromHistory);", server.INDEX_HTML)
        self.assertIn(
            "activateSearchFromHistory({ resetMetaVisibility: true });",
            server.INDEX_HTML,
        )
        self.assertIn("function reconcileSearchPreset()", server.INDEX_HTML)
        self.assertIn("function syncSidebarSelection()", server.INDEX_HTML)
        self.assertIn(
            "presetButton('videos', 'Videos', counts.videos || 0)",
            server.INDEX_HTML,
        )
        self.assertIn(
            "presetButton('all-playlists', 'Playlists', counts.playlists || 0)",
            server.INDEX_HTML,
        )
        self.assertIn("button.dataset.preset = 'playlist-group';", server.INDEX_HTML)
        self.assertNotIn("Playlists with unavailable", server.INDEX_HTML)
        self.assertIn("{ key: 'public', label: 'public', visibilityIcon: true }", server.INDEX_HTML)
        self.assertIn("{ key: 'unlisted', label: 'unlisted', visibilityIcon: true }", server.INDEX_HTML)
        self.assertIn("{ key: 'unknown', label: 'unknown' }", server.INDEX_HTML)
        self.assertIn("unavailable: filterPreferenceEnabled(filterPreferenceKeys.unavailableVideos)", server.INDEX_HTML)
        self.assertIn("value === 'videos' ? 'public' : value", server.INDEX_HTML)
        self.assertNotIn("include_videos=", Path(server.__file__).read_text(encoding="utf-8"))
        self.assertIn("let videoMetaCountsCache = new Map();", server.INDEX_HTML)
        self.assertIn("let omniMetaCountsCache = new Map();", server.INDEX_HTML)
        self.assertIn("let renderedOmniSearchQuery = '';", server.INDEX_HTML)
        self.assertIn("let searchResultsRendered = false;", server.INDEX_HTML)
        self.assertIn("let searchResultsSort = 'newest';", server.INDEX_HTML)
        self.assertIn("function defaultSearchResultsSort(", server.INDEX_HTML)
        self.assertIn(
            "searchSortExplicit = searchSortOptions.has(requestedSort);",
            server.INDEX_HTML,
        )
        self.assertIn("return '__search__';", server.INDEX_HTML)
        self.assertNotIn("Enter a search query.", server.INDEX_HTML)
        self.assertNotIn("Search results", server.INDEX_HTML)
        self.assertIn(
            "title.textContent = '';\n"
            "          meta.textContent = '';\n"
            "          renderSearchMetaFilters();\n"
            "          showSearchHeaderProgress();\n"
            "          showSearchProgress();",
            server.INDEX_HTML,
        )
        self.assertIn("showSearchProgress({ preserveContent: true });", server.INDEX_HTML)
        self.assertIn(
            "renderedOmniSearchQuery === query\n"
            "          && searchResultsRendered",
            server.INDEX_HTML,
        )
        self.assertNotIn("progressMessageAnimation(empty, 'Searching')", server.INDEX_HTML)
        self.assertIn("grid.setAttribute('aria-busy', 'true');", server.INDEX_HTML)
        self.assertIn(
            "const metaCountsKey = JSON.stringify([\n"
            "        scope,\n"
            "        playlistId,\n"
            "        channelId,\n"
            "        query,\n"
            "        partialMinimumPercent,",
            server.INDEX_HTML,
        )
        self.assertIn("const kindsValue = selectedSearchResultKinds().join(',') || '__none__';", server.INDEX_HTML)
        self.assertIn("playlist_group_key: searchPlaylistGroupKey,", server.INDEX_HTML)
        self.assertIn("pageConfig.searchCardLayout", server.INDEX_HTML)
        self.assertIn("pageConfig.playlistCardLayout", server.INDEX_HTML)
        self.assertIn("pageConfig.historyCardLayout", server.INDEX_HTML)
        self.assertIn("pageConfig.sortPreferences", server.INDEX_HTML)
        self.assertIn("pageConfig.pageSize", server.INDEX_HTML)
        self.assertIn(": 'compact';", server.INDEX_HTML)
        self.assertNotIn("params.set('layout'", server.INDEX_HTML)
        self.assertNotIn("params.set('size'", server.INDEX_HTML)
        self.assertNotIn("params.get('size'", server.INDEX_HTML)
        self.assertIn("persistCardLayoutPreference(context, layout)", server.INDEX_HTML)
        self.assertIn("persistSortPreference(context, value)", server.INDEX_HTML)
        self.assertIn("function searchSortPreferenceContext(", server.INDEX_HTML)
        self.assertIn("preferredSearchResultsSort('', preset)", server.INDEX_HTML)
        self.assertIn("persistPageSizePreference(nextPageSize)", server.INDEX_HTML)
        self.assertIn("layoutContext: 'history',", server.INDEX_HTML)
        self.assertIn("cardLayoutHtml(playlistCardLayout, 'playlist')", server.INDEX_HTML)
        self.assertIn("applyPlaylistCardLayout();", server.INDEX_HTML)
        self.assertIn("function rightPanelListMetaHtml(", server.INDEX_HTML)
        self.assertIn("data-card-layout=", server.INDEX_HTML)
        self.assertIn("data-card-layout-context=", server.INDEX_HTML)
        self.assertIn(".search-grid.layout-compact .card { grid-template-columns: 200px minmax(0, 1fr); }", server.INDEX_HTML)
        self.assertIn(".search-grid.layout-compact .video-availability,", server.INDEX_HTML)
        self.assertIn("aspect-ratio: 16 / 9;", server.INDEX_HTML)
        self.assertIn("end.setDate(end.getDate() + (53 * 7) - 1);", server.INDEX_HTML)
        self.assertIn("rangeDateLabel(range.displayEnd)", server.INDEX_HTML)
        self.assertIn("function syncHistoryActivityYearWithRows(rows, preferredDate = '')", server.INDEX_HTML)
        self.assertIn("const currentRange = historyActivityRange(historyActivityYearOffset)", server.INDEX_HTML)
        self.assertIn("function shiftedHistoryDateKey(dateKey, yearDelta)", server.INDEX_HTML)
        self.assertIn("setHistoryPageFromOffset(targetDay.watch_date", server.INDEX_HTML)
        self.assertIn("let historyActivitySyncEnabled = true;", server.INDEX_HTML)
        self.assertIn("syncToggle.dataset.historySync = '';", server.INDEX_HTML)
        self.assertIn("if (!historyActivitySyncEnabled)", server.INDEX_HTML)
        self.assertIn("async function setHistoryActivitySync(enabled)", server.INDEX_HTML)
        self.assertIn("const activity = historyActivitySyncEnabled", server.INDEX_HTML)
        self.assertLess(server.INDEX_HTML.index('id="history-nav"'), server.INDEX_HTML.index('id="search-nav"'))
        self.assertIn('class="primary-nav-divider"', server.INDEX_HTML)
        self.assertNotIn("navButton('__history__', 'History'", server.INDEX_HTML)
        self.assertIn(
            "completionCounts: videoCompletionCountsCache.get(metaCountsKey)",
            server.INDEX_HTML,
        )
        self.assertNotIn('data-filter="members_only_videos"', server.INDEX_HTML)
        self.assertIn(".badge.members-only-badge", server.INDEX_HTML)
        self.assertIn("'subscriber_only', 'members only'", server.VIDEO_CARD_JS)
        self.assertIn("members-only-icon", server.VIDEO_CARD_JS)
        self.assertIn("M6 .5a5.5 5.5 0 100 11", server.VIDEO_CARD_JS)
        self.assertIn("membersOnlyIconHtml,", server.VIDEO_CARD_JS)
        self.assertIn("thumbIconHtml,", server.VIDEO_CARD_JS)
        self.assertIn("decoratorHtml: membersOnlyIconHtml()", server.INDEX_HTML)
        self.assertIn("decoratorHtml: thumbIconHtml('like', false)", server.INDEX_HTML)
        self.assertIn("decoratorHtml: thumbIconHtml('dislike', false)", server.INDEX_HTML)
        self.assertIn("meta-filter-decorated", server.INDEX_HTML)
        self.assertIn(".search-meta-facet .meta-filter-count { font-size: 12px; font-weight: 400; }", server.INDEX_HTML)
        self.assertIn(".search-meta-kind.kind-disabled > .search-meta-row-title { opacity: .42; }", server.INDEX_HTML)
        self.assertIn("counts: searchKindEnabled(kind) ? counts : null", server.INDEX_HTML)
        self.assertIn("beginSidebarNavigationProgress()", server.INDEX_HTML)
        self.assertIn("finishSidebarNavigationProgress(progressToken)", server.INDEX_HTML)
        self.assertIn('class="meta-filter-count">${countText}</span>', server.INDEX_HTML)
        self.assertIn(
            '${escapeHtml(value)} <span class="meta-filter-count">${countText}</span>',
            server.INDEX_HTML,
        )
        self.assertIn(
            '${escapeHtml(label)} <span class="meta-filter-count">${filterCountText(metaFilterCount(counts, key))}</span>',
            server.INDEX_HTML,
        )
        self.assertIn(
            "filterCountText(metaFilterCount(counts, key))",
            server.INDEX_HTML,
        )
        self.assertIn(
            "M9 18c.226 0 .448-.012.667-.037A8.001 8.001 0 018.07 16H7",
            server.INDEX_HTML,
        )
        self.assertNotIn("M3 3l18 18", server.INDEX_HTML)
        self.assertIn('class="video-availability"', server.INDEX_HTML)
        self.assertIn("availabilityHtml: videoAvailabilityHtml(video)", server.INDEX_HTML)
        self.assertIn("watchDateHtml: watched", server.INDEX_HTML)
        self.assertIn("function latestWatchedAtLabel(video)", server.INDEX_HTML)
        self.assertIn("function latestWatchDateHtml(video)", server.INDEX_HTML)
        self.assertEqual(
            server.INDEX_HTML.count("latestWatchDateHtml: latestWatchDateHtml("),
            1,
        )
        self.assertIn(
            "latestWatchDateHtml: options.latestWatchDateHtml || '',",
            server.INDEX_HTML,
        )
        self.assertIn(
            "Last watched ${escapeHtml(watchedAt)}",
            server.INDEX_HTML,
        )
        self.assertIn(
            "${options.watchDateHtml || ''}\n"
            "      ${options.availabilityHtml || ''}\n"
            "      ${options.latestWatchDateHtml || ''}\n"
            "      ${options.watchedHtml || ''}",
            server.VIDEO_CARD_JS,
        )
        self.assertIn(
            "${detailRowHtml(options.details)}\n"
            "      ${options.recoveryHtml || ''}\n"
            "      ${options.watchDateHtml || ''}",
            server.VIDEO_CARD_JS,
        )
        self.assertIn(
            "if (status === 'NOT_FOUND') return 'Archivarix: No results found';",
            server.INDEX_HTML,
        )
        self.assertEqual(server.INDEX_HTML.count("return videoCardFor({"), 1)
        self.assertEqual(
            server.INDEX_HTML.count("recoveryHtml: archivarixStatusHtml(video)"),
            1,
        )
        self.assertNotIn("{ label: archivarixStatusLabel(video) },", server.INDEX_HTML)
        detail_card_start = server.INDEX_HTML.index("function videoDetailCardFor(video)")
        detail_card_end = server.INDEX_HTML.index("function channelDetailCardFor(channel)")
        detail_card_html = server.INDEX_HTML[detail_card_start:detail_card_end]
        self.assertIn("${latestWatchDateHtml(video)}", detail_card_html)
        self.assertLess(
            detail_card_html.index("video.video_id ?"),
            detail_card_html.index("${archivarixStatusHtml(video)}"),
        )
        render_start = server.INDEX_HTML.index("async function render()")
        video_route_start = server.INDEX_HTML.index(
            "if (selected.startsWith('__video__:'))", render_start
        )
        video_route_end = server.INDEX_HTML.index(
            "if (selected.startsWith('__channel__:'))", video_route_start
        )
        video_route_html = server.INDEX_HTML[video_route_start:video_route_end]
        self.assertLess(
            video_route_html.index("grid.className = 'grid';"),
            video_route_html.index("grid.replaceChildren(videoDetailCardFor(video));"),
        )
        video_card_channel = (
            '${options.channelHtml ? `<div class="details video-card-channel">'
            "${options.channelHtml}</div>` : ''}"
        )
        self.assertIn(video_card_channel, server.VIDEO_CARD_JS)
        self.assertLess(
            server.VIDEO_CARD_JS.index(video_card_channel),
            server.VIDEO_CARD_JS.index("${titleHtml(options)}"),
        )
        playlist_card_start = server.INDEX_HTML.index("function cardFor(playlist, options = {})")
        playlist_card_end = server.INDEX_HTML.index("function playlistStatusLabelHtml(playlist)")
        playlist_card_html = server.INDEX_HTML[playlist_card_start:playlist_card_end]
        self.assertIn(
            'headerHtml: owner ? `<div class="details video-card-channel">${owner}</div>` : \'\',',
            playlist_card_html,
        )
        self.assertNotIn('${owner ? `<div class="details">${owner}</div>` : \'\'}', playlist_card_html)
        self.assertLess(
            server.COLLECTION_CARD_JS.index("${options.headerHtml || ''}"),
            server.COLLECTION_CARD_JS.index('<div class="title-row">'),
        )
        self.assertIn(".creator-link { color: var(--muted);", server.INDEX_HTML)
        self.assertIn(
            "return usefulMetadataTitle(video) || video.title || '';",
            server.INDEX_HTML,
        )
        self.assertNotIn(
            "return usefulMetadataTitle(video) || video.title || video.video_id;",
            server.INDEX_HTML,
        )
        self.assertIn(
            "${channelName ? `<div class=\"details video-card-channel\">"
            "${creatorHtml(video.metadata_channel_thumbnail_path, channelName, channelUrl)}"
            "</div>` : ''}\n"
            '            <div class="title-row">',
            server.INDEX_HTML,
        )
        self.assertIn(
            "return row.current_title && row.current_title !== row.video_id ? "
            "row.current_title : '';",
            server.ADMIN_HTML,
        )
        self.assertNotIn(
            "{ label: String(video.availability || '').toLowerCase() === 'unlisted'",
            server.INDEX_HTML,
        )
        self.assertIn("syncMetaFilterGroup('playlist-videos')", server.INDEX_HTML)
        self.assertEqual(
            server.INDEX_HTML.count("syncMetaFilterGroup('playlist-completion')"),
            2,
        )
        self.assertEqual(
            server.INDEX_HTML.count("completion: playlistCompletionVisibility"),
            1,
        )
        self.assertIn("filterAttribute: 'playlist-completion-filter'", server.INDEX_HTML)
        self.assertIn("params.completion_min_percent = String(partialMinimumPercent)", server.INDEX_HTML)
        self.assertIn("'video-collection-top'", server.INDEX_HTML)
        self.assertIn(
            ".view-top.video-collection-top #view-meta",
            server.INDEX_HTML,
        )
        self.assertIn(
            ".video-filter-groups.has-search .video-filter-stack",
            server.INDEX_HTML,
        )
        self.assertIn("gap: 0;", server.INDEX_HTML)
        self.assertIn('<span class="video-filter-stack">', server.INDEX_HTML)
        self.assertIn(
            '<span class="video-filter-facet video-filter-availability">',
            server.INDEX_HTML,
        )
        self.assertIn(
            '<span class="video-filter-separator" aria-hidden="true">|</span>',
            server.INDEX_HTML,
        )
        self.assertIn(
            "definitions: playlistVideoAvailabilityFilterDefinitions",
            server.INDEX_HTML,
        )
        self.assertIn(
            "groupName: 'playlist-removed'",
            server.INDEX_HTML,
        )
        self.assertIn(
            "groupName === 'playlist-videos' ? new Set(['removed']) : new Set()",
            server.INDEX_HTML,
        )
        self.assertIn(
            '<span class="video-filter-facet video-filter-completion">',
            server.INDEX_HTML,
        )
        self.assertIn("syncMetaFilterGroup(`search-${key}`)", server.INDEX_HTML)
        self.assertIn(
            "function syncFilterGroup(parent, childFilters, dimChildrenWhenUnchecked = true)",
            server.INDEX_HTML,
        )
        self.assertIn(
            'root.querySelectorAll(`[data-meta-child-filter="${groupName}"]`)],\n'
            "        false,",
            server.INDEX_HTML,
        )
        self.assertIn("storedTheme() || 'dark'", server.THEME_JS)
        self.assertIn("fields.themeToggle.checked ? 'dark' : 'light'", server.ADMIN_HTML)
        self.assertIn("function formatDate(value)", server.TIMEZONE_JS)
        self.assertIn("formatDate,", server.TIMEZONE_JS)
        self.assertIn("function channelDatesHtml(channel)", server.INDEX_HTML)
        self.assertIn('class="details channel-first-seen"', server.INDEX_HTML)
        self.assertIn("Subscribed ${escapeHtml(subscribedDate)}", server.INDEX_HTML)
        self.assertEqual(server.INDEX_HTML.count("${channelDatesHtml(channel)}"), 2)
        self.assertIn("function channelNotificationHtml(channel)", server.INDEX_HTML)
        self.assertEqual(server.INDEX_HTML.count("${channelNotificationHtml(channel)}"), 2)
        self.assertIn("All notifications", server.INDEX_HTML)
        self.assertIn("Personalized notifications", server.INDEX_HTML)
        self.assertIn("No notifications", server.INDEX_HTML)
        self.assertIn(
            "M19.395 1.196a1 1 0 00-.199 1.4A9 9 0 0121 8",
            server.INDEX_HTML,
        )
        self.assertIn(
            "M16 19a4 4 0 11-8 0H4.765C3.21 19",
            server.INDEX_HTML,
        )
        self.assertIn(
            "M12 1a7 7 0 00-6.213 3.774l1.719 1.032",
            server.INDEX_HTML,
        )
        self.assertIn('id="fetchVideoMetadata"', server.ADMIN_HTML)
        self.assertIn('id="fetchChannelMetadata"', server.ADMIN_HTML)
        self.assertNotIn('id="backfillChannelFirstSeen"', server.ADMIN_HTML)
        self.assertIn("kind: 'video'", server.ADMIN_HTML)
        self.assertIn("kind: 'channel'", server.ADMIN_HTML)
        self.assertLess(
            server.ADMIN_HTML.index("<h2>Videos</h2>"),
            server.ADMIN_HTML.index("<h2>Playlists</h2>"),
        )
        self.assertLess(
            server.ADMIN_HTML.index("<h2>Playlists</h2>"),
            server.ADMIN_HTML.index("<h2>Channels</h2>"),
        )
        self.assertLess(
            server.ADMIN_HTML.index("<h2>Channels</h2>"),
            server.ADMIN_HTML.index("<h2>History</h2>"),
        )

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
