from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from yt_library import cli
from yt_library.config import (
    configured_admin_advanced,
    configured_archivarix_max_in_flight,
    configured_archivarix_request_timeout,
    configured_archivarix_retry_attempts,
    configured_archivarix_retry_backoff,
    configured_archivarix_stream_timeout,
    configured_dispatch_mode,
    configured_display_timezone,
    configured_filter_preferences,
    configured_history_card_layout,
    configured_job_dispatch_delay,
    configured_page_size,
    configured_partial_completion_min_percent,
    configured_playlist_card_layout,
    configured_proxy,
    configured_proxy_address,
    configured_request_delay_range,
    configured_search_card_layout,
    configured_sort_preferences,
    configured_update_frequency,
    configured_update_hour_minute,
    configured_update_time,
    configured_use_proxy,
    configured_youtube_max_in_flight,
    effective_display_timezone,
    ensure_config_file,
    ensure_directory,
    load_config,
    next_update_at,
    save_config,
    valid_update_frequency,
    valid_update_hour_minute,
    valid_update_time,
)


class ConfigTests(unittest.TestCase):
    def test_config_resolves_paths_relative_to_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings" / "yt_library.config.json"
            config_path.parent.mkdir()
            config_path.write_text(
                json.dumps(
                    {
                        "database": "data/library.sqlite3",
                        "youtube_cookies": "secrets/youtube.txt",
                        "cookies": "legacy-cookies.txt",
                        "pockettube_export": "legacy-pockettube.json",
                        "display_timezone": "America/Los_Angeles",
                        "proxy": "socks5h://127.0.0.1:1080",
                        "youtube_proxy": "socks5h://legacy-proxy:1080",
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

            from yt_library.config import config_path as resolve_config_path

            self.assertEqual(
                resolve_config_path(config, "database").resolve(),
                (config_path.parent / "data" / "library.sqlite3").resolve(),
            )
            self.assertEqual(config["display_timezone"], "America/Los_Angeles")
            self.assertTrue(config["use_proxy"])
            self.assertTrue(configured_use_proxy(config))
            self.assertEqual(
                configured_proxy_address(config),
                "socks5h://127.0.0.1:1080",
            )
            self.assertEqual(
                configured_proxy(config),
                "socks5h://127.0.0.1:1080",
            )
            self.assertEqual(configured_dispatch_mode(config), "delay")
            self.assertEqual(configured_job_dispatch_delay(config), 5.0)
            self.assertEqual(configured_request_delay_range(config), (6.0, 10.0))
            self.assertEqual(configured_youtube_max_in_flight(config), 10)
            self.assertEqual(configured_archivarix_max_in_flight(config), 1)
            self.assertEqual(configured_archivarix_request_timeout(config), 15.0)
            self.assertEqual(configured_archivarix_stream_timeout(config), 30.0)
            self.assertEqual(configured_archivarix_retry_attempts(config), 3)
            self.assertEqual(configured_archivarix_retry_backoff(config), 2.0)
            self.assertNotIn("cookies", config)
            self.assertNotIn("pockettube_export", config)
            self.assertNotIn("youtube_proxy", config)
            self.assertEqual(
                resolve_config_path(config, "youtube_cookies").resolve(),
                (config_path.parent / "secrets" / "youtube.txt").resolve(),
            )

    def test_configured_display_timezone_rejects_invalid_names(self) -> None:
        self.assertEqual(
            configured_display_timezone({"display_timezone": "America/Los_Angeles"}),
            "America/Los_Angeles",
        )
        self.assertEqual(configured_display_timezone({"display_timezone": ""}), "")
        self.assertEqual(
            configured_display_timezone({"display_timezone": "Pacific Standard Time"}),
            "UTC",
        )
        self.assertEqual(effective_display_timezone({"display_timezone": ""}), "UTC")
        self.assertEqual(configured_search_card_layout({}), "grid")
        self.assertEqual(configured_playlist_card_layout({}), "grid")
        self.assertEqual(configured_history_card_layout({}), "compact")
        self.assertEqual(configured_page_size({}), 100)
        self.assertEqual(configured_page_size({"page_size": 250}), 250)
        self.assertEqual(configured_page_size({"page_size": 42}), 100)
        self.assertEqual(configured_partial_completion_min_percent({}), 1)
        self.assertEqual(
            configured_partial_completion_min_percent(
                {"partial_completion_min_percent": 65}
            ),
            65,
        )
        self.assertEqual(
            configured_partial_completion_min_percent(
                {"partial_completion_min_percent": 500}
            ),
            99,
        )
        self.assertEqual(configured_filter_preferences({}), {})
        self.assertEqual(
            configured_filter_preferences(
                {
                    "filter_preferences": {
                        "videos.unavailable": True,
                        "completion.partial_below_minimum": False,
                        "channels.terminated": 1,
                        "unknown.filter": True,
                    }
                }
            ),
            {"videos.unavailable": True},
        )
        self.assertEqual(configured_sort_preferences({}), {})
        self.assertEqual(
            configured_sort_preferences(
                {
                    "sort_preferences": {
                        "search": "most_watched",
                        "liked-videos": "oldest",
                        "playlist": "playlist_order",
                        "playlist-group": "invalid",
                        "unknown-context": "title",
                    }
                }
            ),
            {
                "search": "most_watched",
                "liked-videos": "oldest",
                "playlist": "playlist_order",
            },
        )
        self.assertEqual(
            configured_search_card_layout({"search_card_layout": "detailed"}),
            "detailed",
        )
        self.assertEqual(
            configured_playlist_card_layout({"playlist_card_layout": "compact"}),
            "compact",
        )
        self.assertEqual(
            configured_history_card_layout({"history_card_layout": "invalid"}),
            "compact",
        )
        self.assertEqual(
            configured_job_dispatch_delay({"job_dispatch_delay_seconds": -1}),
            0.0,
        )
        self.assertEqual(configured_proxy({"proxy": ""}), "")
        self.assertEqual(
            configured_proxy(
                {
                    "use_proxy": False,
                    "proxy": "socks5h://127.0.0.1:1080",
                }
            ),
            "",
        )
        self.assertFalse(
            configured_use_proxy(
                {
                    "use_proxy": False,
                    "proxy": "socks5h://127.0.0.1:1080",
                }
            )
        )
        with self.assertRaises(ValueError):
            configured_proxy({"proxy": "http://127.0.0.1:1080"})
        self.assertEqual(
            configured_dispatch_mode({"request_jitter_enabled": "yes"}),
            "throttle",
        )
        self.assertEqual(
            configured_request_delay_range(
                {
                    "request_delay_min_seconds": 6,
                    "request_delay_max_seconds": 2,
                }
            ),
            (6.0, 6.0),
        )
        self.assertEqual(configured_youtube_max_in_flight({"youtube_max_in_flight": 0}), 1)
        self.assertEqual(configured_youtube_max_in_flight({"youtube_max_in_flight": 5000}), 100)
        self.assertEqual(configured_archivarix_max_in_flight({"archivarix_max_in_flight": 5000}), 20)
        self.assertEqual(configured_archivarix_request_timeout({"archivarix_request_timeout_seconds": 0}), 1.0)
        self.assertEqual(configured_archivarix_stream_timeout({"archivarix_stream_timeout_seconds": 5000}), 300.0)
        self.assertEqual(configured_archivarix_retry_attempts({"archivarix_retry_attempts": 0}), 1)
        self.assertEqual(configured_archivarix_retry_attempts({"archivarix_retry_attempts": 500}), 10)
        self.assertEqual(configured_archivarix_retry_backoff({"archivarix_retry_backoff_seconds": -1}), 0.0)

    def test_daily_update_schedule_uses_display_timezone(self) -> None:
        config = {
            "display_timezone": "America/Los_Angeles",
            "update_frequency": "daily",
            "update_time": "09:30",
        }
        now = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)

        self.assertEqual(configured_update_frequency(config), "daily")
        self.assertEqual(configured_update_time(config), "09:30")
        self.assertTrue(valid_update_time("23:59"))
        self.assertFalse(valid_update_time("24:00"))
        self.assertEqual(
            next_update_at(config, now),
            datetime(2026, 7, 31, 16, 30, tzinfo=timezone.utc),
        )
        config["update_time"] = "07:30"
        self.assertEqual(
            next_update_at(config, now),
            datetime(2026, 8, 1, 14, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(configured_update_frequency({}), "off")
        self.assertFalse(configured_admin_advanced({}))
        self.assertTrue(configured_admin_advanced({"admin_advanced": "yes"}))
        self.assertEqual(configured_update_time({}), "03:00")
        self.assertEqual(configured_update_time({"update_time": "later"}), "03:00")
        self.assertTrue(valid_update_frequency("hourly"))
        self.assertFalse(valid_update_frequency("weekly"))

    def test_hourly_update_schedule_uses_configured_minute(self) -> None:
        config = {"update_frequency": "hourly", "update_hour_minute": 17}

        self.assertEqual(
            next_update_at(
                config,
                datetime(2026, 7, 31, 15, 42, 30, tzinfo=timezone.utc),
            ),
            datetime(2026, 7, 31, 16, 17, tzinfo=timezone.utc),
        )
        self.assertEqual(
            next_update_at(
                config,
                datetime(2026, 7, 31, 15, 5, tzinfo=timezone.utc),
            ),
            datetime(2026, 7, 31, 15, 17, tzinfo=timezone.utc),
        )
        self.assertEqual(configured_update_hour_minute(config), 17)
        self.assertEqual(configured_update_hour_minute({}), 0)
        self.assertTrue(valid_update_hour_minute("59"))
        self.assertFalse(valid_update_hour_minute("60"))

    def test_load_config_rejects_invalid_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "use_proxy": False,
                        "proxy": "http://127.0.0.1:1080",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "socks5"):
                load_config(config_path)

    def test_load_config_migrates_legacy_dispatch_and_request_delays(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "request_jitter_enabled": True,
                        "youtube_request_interval_seconds": 4,
                        "archivarix_request_interval_seconds": 7,
                        "youtube_request_delay_min_seconds": 2,
                        "youtube_request_delay_max_seconds": 4,
                        "archivarix_request_delay_min_seconds": 6,
                        "archivarix_request_delay_max_seconds": 10,
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(configured_dispatch_mode(config), "throttle")
            self.assertEqual(configured_job_dispatch_delay(config), 7.0)
            self.assertEqual(configured_request_delay_range(config), (6.0, 10.0))

            save_config(config)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["dispatch_mode"], "throttle")
            self.assertEqual(payload["job_dispatch_delay_seconds"], 7.0)
            self.assertEqual(payload["request_delay_min_seconds"], 6.0)
            self.assertEqual(payload["request_delay_max_seconds"], 10.0)
            self.assertNotIn("request_jitter_enabled", payload)
            self.assertNotIn("youtube_request_interval_seconds", payload)
            self.assertNotIn("archivarix_request_delay_max_seconds", payload)

    def test_load_config_migrates_history_schedule_to_update_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "history_fetch_daily": True,
                        "history_fetch_time": "04:30",
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)
            self.assertEqual(configured_update_frequency(config), "daily")
            self.assertEqual(configured_update_time(config), "04:30")

            save_config(config)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["update_frequency"], "daily")
            self.assertEqual(payload["update_time"], "04:30")
            self.assertNotIn("history_fetch_daily", payload)
            self.assertNotIn("history_fetch_time", payload)

    def test_load_config_migrates_daily_update_to_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config_path.write_text(
                json.dumps({"update_daily": True, "update_time": "10:34"}),
                encoding="utf-8",
            )

            config = load_config(config_path)
            self.assertEqual(configured_update_frequency(config), "daily")

            save_config(config)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["update_frequency"], "daily")
            self.assertEqual(payload["update_time"], "10:34")
            self.assertNotIn("update_daily", payload)

    def test_migrate_creates_default_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            db_path = Path(temp_dir) / "library.sqlite3"

            cli.main(["--config", str(config_path), "migrate", "--db", str(db_path)])

            self.assertTrue(config_path.exists())
            self.assertTrue(db_path.exists())
            self.assertTrue((Path(temp_dir) / "takeout").is_dir())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["display_timezone"], "")
            self.assertEqual(payload["search_card_layout"], "grid")
            self.assertEqual(payload["playlist_card_layout"], "grid")
            self.assertEqual(payload["history_card_layout"], "compact")
            self.assertEqual(payload["sort_preferences"], {})
            self.assertEqual(payload["page_size"], 100)
            self.assertEqual(payload["partial_completion_min_percent"], 1)
            self.assertEqual(payload["filter_preferences"], {})
            self.assertEqual(payload["update_frequency"], "off")
            self.assertEqual(payload["update_hour_minute"], 0)
            self.assertEqual(payload["update_time"], "03:00")
            self.assertFalse(payload["admin_advanced"])
            self.assertEqual(payload["host"], "127.0.0.1")
            self.assertEqual(payload["youtube_cookies"], "yt_cookies.txt")
            self.assertEqual(payload["archivarix_cookies"], "archivarix_cookies.txt")
            self.assertFalse(payload["use_proxy"])
            self.assertEqual(payload["proxy"], "")
            self.assertEqual(payload["dispatch_mode"], "delay")
            self.assertNotIn("youtube_proxy", payload)
            self.assertEqual(payload["job_dispatch_delay_seconds"], 5.0)
            self.assertEqual(payload["request_delay_min_seconds"], 6.0)
            self.assertEqual(payload["request_delay_max_seconds"], 10.0)
            self.assertEqual(payload["youtube_max_in_flight"], 10)
            self.assertEqual(payload["archivarix_max_in_flight"], 1)
            self.assertEqual(payload["archivarix_request_timeout_seconds"], 15.0)
            self.assertEqual(payload["archivarix_stream_timeout_seconds"], 30.0)
            self.assertEqual(payload["archivarix_retry_attempts"], 3)
            self.assertEqual(payload["archivarix_retry_backoff_seconds"], 2.0)
            self.assertNotIn("cookies", payload)
            self.assertNotIn("pockettube_export", payload)

    def test_new_config_creates_custom_takeout_directory_relative_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings" / "yt_library.config.json"
            config_path.parent.mkdir()
            config = load_config(config_path)
            config["takeout_dir"] = "imports/takeout"

            ensure_config_file(config)

            self.assertTrue(config_path.exists())
            self.assertTrue((config_path.parent / "imports" / "takeout").is_dir())

    def test_ensure_directory_recreates_missing_takeout_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            takeout_dir = Path(temp_dir) / "nested" / "takeout"

            created = ensure_directory(takeout_dir)

            self.assertEqual(created, takeout_dir)
            self.assertTrue(takeout_dir.is_dir())

    def test_cli_defaults_to_serve_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            with patch("yt_library.cli.serve") as serve:
                result = cli.main(["--config", str(config_path)])

            self.assertEqual(result, 0)
            args = serve.call_args.args[0]
            self.assertEqual(args.command, "serve")
            self.assertEqual(Path(args.db).resolve(), (config_path.parent / "yt_library.sqlite3").resolve())
            self.assertEqual(Path(args.cookies).resolve(), (config_path.parent / "yt_cookies.txt").resolve())
            self.assertEqual(args.host, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
