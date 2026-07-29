from __future__ import annotations

import argparse
import tempfile
import time
import sqlite3
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import unittest
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from yt_library import cli
from yt_library import core
from yt_library import network
from yt_library import server
from yt_library import workers
from yt_library.config import (
    configured_archivarix_max_in_flight,
    configured_archivarix_request_timeout,
    configured_archivarix_retry_attempts,
    configured_archivarix_retry_backoff,
    configured_archivarix_stream_timeout,
    configured_dispatch_mode,
    configured_display_timezone,
    configured_job_dispatch_delay,
    configured_proxy_address,
    configured_request_delay_range,
    configured_use_proxy,
    configured_youtube_max_in_flight,
    configured_proxy,
    effective_display_timezone,
    ensure_config_file,
    ensure_directory,
    load_config,
    save_config,
)
from yt_library.workers import (
    LiveHistoryWorker,
    MetadataWorker,
    PlaceholderRecoveryWorker,
    PlaylistScanWorker,
    WorkerQueueDispatcher,
)


def migrated_connection(db_path: Path):
    core.migrate_database(db_path)
    return core.connect(db_path)


class CoreHelperTests(unittest.TestCase):
    def test_socks5_proxy_parser_supports_remote_dns_and_credentials(self) -> None:
        proxy = network.parse_socks5_proxy_url(
            "socks5h://user%20name:pass%2Fword@127.0.0.1:1080"
        )

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.host, "127.0.0.1")
        self.assertEqual(proxy.port, 1080)
        self.assertTrue(proxy.remote_dns)
        self.assertEqual(proxy.username, "user name")
        self.assertEqual(proxy.password, "pass/word")
        self.assertFalse(
            network.parse_socks5_proxy_url("socks5://localhost:1081").remote_dns
        )
        self.assertIsNone(network.parse_socks5_proxy_url(""))

    def test_socks5_proxy_parser_rejects_unsupported_or_ambiguous_urls(self) -> None:
        invalid_values = (
            "http://127.0.0.1:1080",
            "socks5://127.0.0.1",
            "socks5://127.0.0.1:1080/path",
            "socks5://:1080",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    network.parse_socks5_proxy_url(value)

    def test_socks5_connection_routes_destination_through_pysocks(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        fake_socket = object()

        class FakeSocks:
            SOCKS5 = 2

            @staticmethod
            def create_connection(*args, **kwargs):
                calls.append((args, kwargs))
                return fake_socket

        proxy = network.parse_socks5_proxy_url(
            "socks5h://proxy-user:proxy-pass@localhost:1080"
        )
        connection = network._Socks5HTTPConnection(
            "www.youtube.com",
            port=80,
            timeout=12,
            socks_module=FakeSocks,
            proxy=proxy,
        )

        connection.connect()

        self.assertIs(connection.sock, fake_socket)
        self.assertEqual(calls[0][0], (("www.youtube.com", 80),))
        self.assertEqual(calls[0][1]["proxy_type"], FakeSocks.SOCKS5)
        self.assertEqual(calls[0][1]["proxy_addr"], "localhost")
        self.assertEqual(calls[0][1]["proxy_port"], 1080)
        self.assertTrue(calls[0][1]["proxy_rdns"])
        self.assertEqual(calls[0][1]["proxy_username"], "proxy-user")
        self.assertEqual(calls[0][1]["proxy_password"], "proxy-pass")

    def test_socks5_connection_promotes_proxy_connection_failure(self) -> None:
        class ProxyConnectionError(OSError):
            pass

        class FakeSocks:
            SOCKS5 = 2

            @staticmethod
            def create_connection(*_args, **_kwargs):
                raise ProxyConnectionError(
                    "Error connecting to SOCKS5 proxy "
                    "socks5h://secret-user:secret-pass@127.0.0.1:1081"
                )

        proxy = network.parse_socks5_proxy_url(
            "socks5h://secret-user:secret-pass@127.0.0.1:1081"
        )
        connection = network._Socks5HTTPConnection(
            "www.youtube.com",
            port=80,
            timeout=12,
            socks_module=FakeSocks,
            proxy=proxy,
        )

        with self.assertRaises(network.ProxyUnavailableError) as raised:
            connection.connect()

        self.assertIn("127.0.0.1:1081", str(raised.exception))
        self.assertNotIn("secret-user", str(raised.exception))
        self.assertNotIn("secret-pass", str(raised.exception))
        self.assertIsInstance(
            network.proxy_unavailable_error(
                ConnectionResetError("connection reset"),
                "socks5h://127.0.0.1:1081",
            ),
            network.ProxyUnavailableError,
        )

    def test_proxy_failure_classifier_ignores_non_proxy_request_errors(self) -> None:
        self.assertIsNone(
            network.proxy_unavailable_error(
                urllib.error.URLError("YouTube request timed out"),
                "socks5h://127.0.0.1:1081",
            )
        )
        self.assertIsNone(
            network.proxy_unavailable_error(
                RuntimeError("Error connecting to SOCKS5 proxy"),
                "",
            )
        )

    def test_proxy_failure_classifier_recognizes_ytdlp_proxy_errors(self) -> None:
        class ProxyError(Exception):
            pass

        ProxyError.__module__ = "yt_dlp.networking.exceptions"
        error = network.proxy_unavailable_error(
            ProxyError("proxy transport failed"),
            "socks5h://127.0.0.1:1081",
        )

        self.assertIsInstance(error, network.ProxyUnavailableError)
        self.assertIn("127.0.0.1:1081", str(error))

    def test_socks5_https_handler_uses_its_tls_context(self) -> None:
        class FakeSocks:
            SOCKS5 = 2

        proxy = network.parse_socks5_proxy_url("socks5h://localhost:1080")
        handler = network._Socks5HTTPSHandler(FakeSocks, proxy)
        request = urllib.request.Request("https://www.youtube.com/")

        with patch.object(handler, "do_open", return_value="response") as do_open:
            response = handler.https_open(request)

        self.assertEqual(response, "response")
        do_open.assert_called_once_with(
            handler._connection,
            request,
            context=handler._context,
        )

    def test_ytdlp_proxy_options_preserve_supported_proxy_url(self) -> None:
        self.assertEqual(
            network.ytdlp_proxy_options("socks5://127.0.0.1:1080"),
            {"proxy": "socks5://127.0.0.1:1080"},
        )
        self.assertEqual(network.ytdlp_proxy_options(""), {})

    def test_proxy_probe_connects_through_configured_socks5_proxy(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        class FakeSocket:
            closed = False

            def close(self) -> None:
                self.closed = True

        fake_socket = FakeSocket()

        class FakeSocks:
            SOCKS5 = 2

            @staticmethod
            def create_connection(*args, **kwargs):
                calls.append((args, kwargs))
                return fake_socket

        with patch("yt_library.network._load_socks_module", return_value=FakeSocks):
            available, message = network.probe_socks5_proxy(
                "socks5h://proxy-user:proxy-pass@localhost:1081",
                timeout=2,
            )

        self.assertTrue(available)
        self.assertEqual(message, "")
        self.assertEqual(calls[0][0], (("www.youtube.com", 443),))
        self.assertEqual(calls[0][1]["timeout"], 2)
        self.assertTrue(calls[0][1]["proxy_rdns"])
        self.assertTrue(fake_socket.closed)

    def test_proxy_probe_reports_sanitized_connection_failure(self) -> None:
        class ProxyConnectionError(OSError):
            pass

        class FakeSocks:
            SOCKS5 = 2

            @staticmethod
            def create_connection(*_args, **_kwargs):
                raise ProxyConnectionError(
                    "Error connecting to SOCKS5 proxy "
                    "socks5h://secret-user:secret-pass@127.0.0.1:1081"
                )

        with patch("yt_library.network._load_socks_module", return_value=FakeSocks):
            available, message = network.probe_socks5_proxy(
                "socks5h://secret-user:secret-pass@127.0.0.1:1081"
            )

        self.assertFalse(available)
        self.assertIn("127.0.0.1:1081", message)
        self.assertNotIn("secret-user", message)
        self.assertNotIn("secret-pass", message)

    def test_metadata_worker_passes_proxy_to_poll_run(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        captured_args: list[object] = []
        worker = MetadataWorker()

        def hold_worker(*args) -> None:
            captured_args.extend(args)
            entered.set()
            release.wait(2)

        with patch.object(worker, "_run", side_effect=hold_worker):
            result = worker.start(
                Path("library.sqlite3"),
                Path("cookies.txt"),
                Path("thumbs"),
                delay=0,
                limit=1,
                force=False,
                stale_days=30,
                proxy_url="socks5h://127.0.0.1:1080",
            )
            self.assertTrue(result["started"])
            self.assertTrue(entered.wait(1))
            self.assertEqual(captured_args[-1], "socks5h://127.0.0.1:1080")
            release.set()
            deadline = time.time() + 1
            while worker.is_alive() and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(worker.is_alive())

    def test_dispatcher_passes_general_proxy_to_queue_run(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        captured_args: list[object] = []
        dispatcher = WorkerQueueDispatcher()

        def hold_dispatcher(*args) -> None:
            captured_args.extend(args)
            entered.set()
            release.wait(2)

        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "config.json")
            config["use_proxy"] = True
            config["proxy"] = "socks5h://127.0.0.1:1080"
            core.migrate_database(Path(temp_dir) / "library.sqlite3")
            with patch.object(dispatcher, "_run", side_effect=hold_dispatcher):
                result = dispatcher.start(
                    Path(temp_dir) / "library.sqlite3",
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    config,
                )
                self.assertTrue(result["started"])
                self.assertTrue(entered.wait(1))
                self.assertEqual(captured_args[-1], "socks5h://127.0.0.1:1080")
                release.set()
                deadline = time.time() + 1
                while dispatcher.is_alive() and time.time() < deadline:
                    time.sleep(0.01)
                self.assertFalse(dispatcher.is_alive())

    def test_dispatcher_start_clears_proxy_hold_after_successful_probe(self) -> None:
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
            config = load_config(Path(temp_dir) / "config.json")
            config["use_proxy"] = True
            config["proxy"] = "socks5h://127.0.0.1:1081"
            dispatcher = WorkerQueueDispatcher()

            with (
                patch(
                    "yt_library.workers.probe_socks5_proxy",
                    return_value=(True, ""),
                ) as probe,
                patch.object(
                    dispatcher,
                    "_start_background",
                    return_value={"started": True},
                ) as start_background,
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    config,
                )

            self.assertTrue(result["started"])
            probe.assert_called_once_with("socks5h://127.0.0.1:1081")
            start_background.assert_called_once()
            conn = core.connect(db_path)
            try:
                self.assertFalse(core.external_service_block(conn, "proxy")["blocked"])
                recovery_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(recovery_log["level"], "queue info")
                self.assertIn("Proxy connectivity restored", recovery_log["message"])
            finally:
                conn.close()

    def test_dispatcher_start_retains_proxy_hold_after_failed_probe(self) -> None:
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
            config = load_config(Path(temp_dir) / "config.json")
            config["use_proxy"] = True
            config["proxy"] = "socks5h://127.0.0.1:1081"
            dispatcher = WorkerQueueDispatcher()
            failure = "SOCKS5 proxy 127.0.0.1:1081 is unavailable"

            with (
                patch(
                    "yt_library.workers.probe_socks5_proxy",
                    return_value=(False, failure),
                ),
                patch.object(dispatcher, "_start_background") as start_background,
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    config,
                )

            self.assertFalse(result["started"])
            self.assertTrue(result["blocked"])
            self.assertEqual(result["message"], failure)
            start_background.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertTrue(core.external_service_block(conn, "proxy")["blocked"])
                failure_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(failure_log["level"], "queue error")
                self.assertIn("proxy is unavailable", failure_log["message"])
            finally:
                conn.close()

    def test_placeholder_worker_passes_general_proxy_to_run(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        captured_args: list[object] = []
        worker = PlaceholderRecoveryWorker()

        def hold_worker(*args) -> None:
            captured_args.extend(args)
            entered.set()
            release.wait(2)

        with patch.object(worker, "_run", side_effect=hold_worker):
            result = worker.start(
                Path("library.sqlite3"),
                Path("archivarix-cookies.txt"),
                Path("thumbs"),
                proxy_url="socks5h://127.0.0.1:1080",
            )
            self.assertTrue(result["started"])
            self.assertTrue(entered.wait(1))
            self.assertEqual(captured_args[-1], "socks5h://127.0.0.1:1080")
            release.set()
            deadline = time.time() + 1
            while worker.is_alive() and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(worker.is_alive())

    def test_local_asset_path_maps_external_thumbnail_storage_to_web_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            app_root = temp_root / "release"
            external_video_thumbs = temp_root / "storage" / "video_thumbs"
            thumbnail = external_video_thumbs / "video-id.jpg"
            thumbnail.parent.mkdir(parents=True)
            thumbnail.write_bytes(b"thumbnail")

            with (
                patch.object(core, "ROOT", app_root),
                patch.object(core, "DEFAULT_VIDEO_THUMB_DIR", external_video_thumbs),
            ):
                self.assertEqual(
                    core.local_asset_path(thumbnail),
                    "video_thumbs/video-id.jpg",
                )

    def test_request_pacer_spaces_request_starts(self) -> None:
        clock = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        pacer = core.RequestPacer(
            6.0,
            10.0,
            monotonic=lambda: clock[0],
            sleep=sleep,
            uniform=lambda minimum, maximum: 8.0,
        )

        pacer.wait()
        clock[0] = 2.0
        pacer.wait()

        self.assertEqual(sleeps, [6.0])
        self.assertEqual(clock[0], 8.0)

    def test_youtube_request_url_matching_excludes_unrelated_hosts(self) -> None:
        self.assertTrue(core.is_youtube_request_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(core.is_youtube_request_url("https://i.ytimg.com/vi/abc/hqdefault.jpg"))
        self.assertTrue(core.is_youtube_request_url("https://rr1.googlevideo.com/videoplayback"))
        self.assertTrue(core.is_youtube_request_url("https://yt3.ggpht.com/avatar"))
        self.assertTrue(core.is_youtube_request_url("https://yt3.googleusercontent.com/avatar"))
        self.assertFalse(core.is_youtube_request_url("https://youtube.com.example.test/watch?v=abc"))
        self.assertFalse(core.is_youtube_request_url("https://archivarix.com/search"))

    def test_archivarix_request_url_matching_includes_api_and_archive_hosts(self) -> None:
        self.assertTrue(core.is_archivarix_request_url("https://tube.archivarix.net/api/search"))
        self.assertTrue(core.is_archivarix_request_url("https://web.archive.org/web/example"))
        self.assertFalse(core.is_archivarix_request_url("https://archivarix.net.example.test/search"))
        self.assertFalse(core.is_archivarix_request_url("https://www.youtube.com/watch?v=abc"))

    def test_request_pacing_routes_both_sites_to_one_global_pacer(self) -> None:
        opener = Mock()
        request_pacer = Mock()
        with patch.object(core, "_request_pacer", request_pacer):
            core.open_with_request_pacing(
                opener,
                urllib.request.Request("https://yt3.ggpht.com/avatar"),
                timeout=12,
            )
            core.open_with_request_pacing(
                opener,
                urllib.request.Request("https://tube.archivarix.net/api/search"),
                timeout=18,
            )
            core.open_with_request_pacing(
                opener,
                urllib.request.Request("https://example.test/resource"),
                timeout=6,
            )

        self.assertEqual(request_pacer.wait.call_count, 2)
        self.assertEqual(opener.open.call_count, 3)

    def test_ytdlp_urlopen_uses_the_shared_request_pacer(self) -> None:
        class BaseYoutubeDL:
            def __init__(self, options):
                self.options = options

            def urlopen(self, request):
                return f"opened:{request}"

        class FakeYtdlpModule:
            YoutubeDL = BaseYoutubeDL

        request_pacer = Mock()
        with patch.object(core, "_request_pacer", request_pacer):
            ydl = core.request_paced_youtube_dl(
                FakeYtdlpModule,
                {"quiet": True},
            )
            result = ydl.urlopen("playlist-request")

        request_pacer.wait.assert_called_once_with()
        self.assertEqual(result, "opened:playlist-request")
        self.assertEqual(ydl.options, {"quiet": True})

    def test_placeholder_recovery_exposes_its_persisted_run_id(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        worker = PlaceholderRecoveryWorker()

        def hold_worker(*_args) -> None:
            entered.set()
            release.wait(2)

        with patch.object(worker, "_run", side_effect=hold_worker):
            result = worker.start(Path("library.sqlite3"), Path("cookies.txt"), Path("thumbs"))
            self.assertTrue(entered.wait(1))
            self.assertTrue(result["started"])
            self.assertTrue(result["run_id"])

            stopped = worker.stop()
            self.assertEqual(stopped["run_id"], result["run_id"])
            release.set()
            deadline = time.time() + 1
            while worker.is_alive() and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(worker.is_alive())

    def test_thread_worker_lifecycle_rejects_duplicate_start_and_reports_stopping(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        worker = MetadataWorker()

        def hold_worker(*_args) -> None:
            entered.set()
            release.wait(2)

        with patch.object(worker, "_run", side_effect=hold_worker):
            first = worker.start(
                Path("library.sqlite3"),
                Path("cookies.txt"),
                Path("thumbs"),
                delay=0,
                limit=1,
                force=False,
                stale_days=30,
            )
            self.assertTrue(entered.wait(1))
            duplicate = worker.start(
                Path("library.sqlite3"),
                Path("cookies.txt"),
                Path("thumbs"),
                delay=0,
                limit=1,
                force=False,
                stale_days=30,
            )

            self.assertTrue(first["started"])
            self.assertFalse(duplicate["started"])
            self.assertEqual(duplicate["run_id"], first["run_id"])
            self.assertTrue(worker.is_running())

            stopped = worker.stop()
            self.assertTrue(stopped["stopping"])
            self.assertEqual(stopped["run_id"], first["run_id"])
            self.assertFalse(worker.is_running())
            self.assertTrue(worker.is_stopping())
            self.assertTrue(worker.is_alive())

            release.set()
            deadline = time.time() + 1
            while worker.is_alive() and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(worker.is_alive())

    def test_archivarix_recovery_does_not_start_when_stop_is_requested(self) -> None:
        stop_event = threading.Event()
        stop_event.set()
        with (
            patch("yt_library.core.cache_archivarix_thumbnail") as cache_thumbnail,
            patch("yt_library.core.archivarix_lookup_video") as lookup_video,
        ):
            result = core.recover_archivarix_video(
                "abc12345678",
                Path("unused"),
                object(),
                stop_event=stop_event,
            )

        self.assertEqual(result[3:], ("stopped", "Stop requested"))
        cache_thumbnail.assert_not_called()
        lookup_video.assert_not_called()

    def test_archivarix_session_status_requires_a_current_session_cookie(self) -> None:
        class Cookie:
            def __init__(self, expires: int | None) -> None:
                self.name = "__Secure-better-auth.session_token"
                self.domain = "tube.archivarix.net"
                self.expires = expires

        with patch("yt_library.core.load_cookie_jar", return_value=[Cookie(200)]):
            self.assertEqual(core.archivarix_session_status(Path("unused"), now=100), (True, ""))
        with patch("yt_library.core.load_cookie_jar", return_value=[Cookie(100)]):
            valid, message = core.archivarix_session_status(Path("unused"), now=100)
            self.assertFalse(valid)
            self.assertIn("expired", message)
        with patch("yt_library.core.load_cookie_jar", return_value=[]):
            valid, message = core.archivarix_session_status(Path("unused"), now=100)
            self.assertFalse(valid)
            self.assertIn("missing", message)

    def test_archivarix_quota_text_is_detected(self) -> None:
        self.assertEqual(
            core.archivarix_quota_message_from_text("Limit reached: 500 searches per day"),
            "Archivarix daily search limit reached",
        )
        self.assertEqual(core.archivarix_quota_message_from_text("ordinary response"), "")

    def test_archivarix_search_uses_supplied_network_opener(self) -> None:
        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return json.dumps(
                    {"data": {"videos": [{"videoId": "proxytest1"}]}}
                ).encode("utf-8")

        class Opener:
            def __init__(self) -> None:
                self.calls: list[tuple[object, object]] = []

            def open(self, request, timeout=None):
                self.calls.append((request, timeout))
                return Response()

        opener = Opener()
        videos = core.archivarix_search_deleted(
            "Proxy test",
            page_size=25,
            opener=opener,
        )

        self.assertEqual(videos, [{"videoId": "proxytest1"}])
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0][1], 30)
        self.assertIn("tube.archivarix.net/api/fts", opener.calls[0][0].full_url)

    def test_provided_metadata_passes_general_proxy_to_channel_enrichment(self) -> None:
        conn = object()
        opener = object()
        thumb_dir = Path("thumbs")
        metadata = {
            "video_id": "abc12345678",
            "title": "Example video",
            "channel_id": "UCexample",
        }
        with (
            patch(
                "yt_library.core.resolve_metadata_target",
                return_value=("video", "abc12345678"),
            ),
            patch("yt_library.core.fetch_watch_metadata", return_value=metadata),
            patch(
                "yt_library.core.fetch_new_channel_metadata_if_needed",
                return_value=({}, "", ""),
            ) as fetch_channel,
            patch("yt_library.core.store_video_metadata"),
        ):
            core.fetch_provided_metadata(
                conn,
                opener,
                thumb_dir,
                "abc12345678",
                proxy_url="socks5h://proxy.test:1080",
            )

        fetch_channel.assert_called_once_with(
            conn,
            opener,
            thumb_dir,
            metadata,
            proxy_url="socks5h://proxy.test:1080",
        )

    def test_archivarix_timeout_errors_are_classified(self) -> None:
        self.assertTrue(core.archivarix_timeout_error(TimeoutError("read timed out")))
        self.assertTrue(
            core.archivarix_timeout_error(
                urllib.error.URLError(TimeoutError("connection timed out"))
            )
        )
        self.assertFalse(core.archivarix_timeout_error(OSError("connection reset")))

    def test_youtube_session_status_requires_a_current_login_cookie(self) -> None:
        class Cookie:
            def __init__(self, name: str, domain: str, expires: int | None) -> None:
                self.name = name
                self.domain = domain
                self.expires = expires

        with patch(
            "yt_library.core.load_cookie_jar",
            return_value=[Cookie("SID", ".youtube.com", 200)],
        ):
            self.assertEqual(core.youtube_session_status(Path("unused"), now=100), (True, ""))
        with patch(
            "yt_library.core.load_cookie_jar",
            return_value=[Cookie("SID", ".youtube.com", 100)],
        ):
            valid, message = core.youtube_session_status(Path("unused"), now=100)
            self.assertFalse(valid)
            self.assertIn("expired", message)
        with patch("yt_library.core.load_cookie_jar", return_value=[]):
            valid, message = core.youtube_session_status(Path("unused"), now=100)
            self.assertFalse(valid)
            self.assertIn("missing", message)
        with (
            patch(
                "yt_library.core.load_cookie_jar",
                return_value=[Cookie("SID", ".youtube.com", 200)],
            ),
            patch("yt_library.core.load_cookie_opener", return_value=object()),
            patch(
                "yt_library.core.request_text",
                return_value="Watch history isn't viewable when signed out",
            ),
        ):
            valid, message = core.youtube_session_status(Path("unused"), now=100, verify_remote=True)
            self.assertFalse(valid)
            self.assertIn("not accepted", message)

    def test_youtube_page_authentication_uses_logged_in_state(self) -> None:
        logged_in = 'ytcfg.set({"LOGGED_IN":true}); ServiceLogin recaptcha'
        logged_out = 'ytcfg.set({"LOGGED_IN":false});'
        self.assertIs(core.youtube_page_login_state(logged_in), True)
        self.assertIs(core.youtube_page_login_state(logged_out), False)
        self.assertIsNone(core.youtube_page_login_state("ServiceLogin"))
        self.assertTrue(core.youtube_page_is_authenticated(logged_in))
        self.assertFalse(core.youtube_page_is_authenticated(logged_out))
        self.assertFalse(core.youtube_page_is_authenticated("ServiceLogin"))
        self.assertFalse(core.youtube_page_requires_login(logged_in))
        self.assertTrue(core.youtube_page_requires_login(logged_out))
        self.assertTrue(core.youtube_page_requires_login("ServiceLogin"))
        self.assertFalse(core.youtube_page_requires_login("playlist header"))

    def test_youtube_playlist_missing_requires_authenticated_404_without_header(self) -> None:
        logged_in = 'ytcfg.set({"LOGGED_IN":true});'
        missing_error = (
            "[youtube:tab] ERROR - Requested entity was not found. "
            "Unable to download API page: HTTP Error 404: Not Found"
        )
        self.assertTrue(
            core.youtube_playlist_is_missing(
                logged_in,
                {"video_count": 0, "has_video_count": False},
                missing_error,
            )
        )
        self.assertFalse(
            core.youtube_playlist_is_missing(
                'ytcfg.set({"LOGGED_IN":false});',
                {"video_count": 0, "has_video_count": False},
                missing_error,
            )
        )
        self.assertFalse(
            core.youtube_playlist_is_missing(
                logged_in,
                {"video_count": 1, "has_video_count": True},
                missing_error,
            )
        )
        self.assertFalse(
            core.youtube_playlist_is_missing(
                logged_in,
                {"video_count": 0, "has_video_count": False},
                "Incomplete yt initial data received",
            )
        )

    def test_youtube_page_diagnostics_classify_authentication_challenges(self) -> None:
        page = """
        ytcfg.set({
          "LOGGED_IN": false,
          "INNERTUBE_CLIENT_NAME": "WEB",
          "INNERTUBE_CLIENT_VERSION": "2.20260714.00.00"
        });
        var ytInitialPlayerResponse = {
          "playabilityStatus": {
            "status": "LOGIN_REQUIRED",
            "reason": "Sign in to confirm you're not a bot"
          }
        };
        <a href="https://accounts.google.com/ServiceLogin">Sign in</a>
        """
        diagnostics = core.youtube_page_diagnostics(page, "watch page")
        self.assertIn("operation=watch page", diagnostics)
        self.assertIn("logged_in=false", diagnostics)
        self.assertIn("service_login", diagnostics)
        self.assertIn("bot_check", diagnostics)
        self.assertIn("player_status=LOGIN_REQUIRED", diagnostics)
        self.assertIn("client=WEB", diagnostics)

    def test_youtube_request_error_diagnostics_sanitize_http_failure(self) -> None:
        error = urllib.error.HTTPError(
            "https://www.youtube.com/watch?v=private-id",
            429,
            "Too Many Requests",
            {"Retry-After": "120", "Content-Type": "text/html; charset=utf-8"},
            None,
        )
        diagnostics = core.youtube_request_error_diagnostics(error, "watch metadata")
        self.assertIn("status=429", diagnostics)
        self.assertIn("retry_after=120", diagnostics)
        self.assertIn("content_type=text/html", diagnostics)
        self.assertNotIn("private-id", diagnostics)

    def test_temporary_ytdlp_cookie_file_does_not_modify_configured_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_file = Path(temp_dir) / "cookies.txt"
            cookie_file.write_text("original cookies", encoding="utf-8")

            with core.temporary_ytdlp_cookie_file(cookie_file) as working_cookie_file:
                self.assertIsNotNone(working_cookie_file)
                self.assertNotEqual(working_cookie_file, cookie_file)
                self.assertEqual(working_cookie_file.read_text(encoding="utf-8"), "original cookies")
                working_cookie_file.write_text("yt-dlp updates", encoding="utf-8")

            self.assertEqual(cookie_file.read_text(encoding="utf-8"), "original cookies")
            self.assertFalse(working_cookie_file.exists())

    def test_youtube_ytdlp_probe_diagnostics_classify_cookie_rotation(self) -> None:
        diagnostics = core.youtube_ytdlp_probe_diagnostics(
            [
                "The provided YouTube account cookies are no longer valid. "
                "They have likely been rotated in the browser.",
                "web_safari player response playability status: LOGIN_REQUIRED",
            ],
            succeeded=False,
            deno_available=True,
            ejs_available=True,
        )

        self.assertIn("yt_dlp_probe=cookies_rotated", diagnostics)
        self.assertIn("deno=available", diagnostics)
        self.assertIn("ejs=available", diagnostics)
        self.assertIn("clients=web_safari", diagnostics)

    def test_youtube_ytdlp_probe_diagnostics_classify_bot_challenge(self) -> None:
        diagnostics = core.youtube_ytdlp_probe_diagnostics(
            ["ERROR: Sign in to confirm you're not a bot"],
            succeeded=False,
            deno_available=False,
            ejs_available=False,
        )

        self.assertIn("yt_dlp_probe=bot_challenge", diagnostics)
        self.assertIn("deno=missing", diagnostics)
        self.assertIn("ejs=missing", diagnostics)

    def test_youtube_ytdlp_probe_diagnostics_classify_rejected_login(self) -> None:
        diagnostics = core.youtube_ytdlp_probe_diagnostics(
            ["Login details are needed to download this content"],
            succeeded=False,
            deno_available=True,
            ejs_available=True,
        )

        self.assertIn("yt_dlp_probe=login_required", diagnostics)

    def test_history_date_from_relative_and_month_labels(self) -> None:
        today = date(2026, 7, 6)

        self.assertEqual(core.history_date_from_label("Today", today), "2026-07-06")
        self.assertEqual(core.history_date_from_label("Yesterday", today), "2026-07-05")
        self.assertEqual(core.history_date_from_label("Monday", today), "2026-06-29")
        self.assertEqual(core.history_date_from_label("Jun 30", today), "2026-06-30")
        self.assertEqual(core.history_date_from_label("Dec 31", today), "2025-12-31")

    def test_watch_datetime_helpers_normalize_offsets(self) -> None:
        self.assertEqual(
            core.takeout_watch_datetime("July 4, 2026, 5:27:45 AM PDT"),
            "2026-07-04T12:27:45Z",
        )
        self.assertEqual(
            core.takeout_watch_datetime("2026-07-04T05:27:45.123Z"),
            "2026-07-04T05:27:45Z",
        )
        self.assertEqual(
            core.local_date_for_utc_instant("2026-07-04T05:27:45Z", "America/Los_Angeles"),
            "2026-07-03",
        )

    def test_id_and_numeric_helpers(self) -> None:
        self.assertEqual(core.extract_video_id("https://www.youtube.com/watch?v=abc-123_DEF"), "abc-123_DEF")
        self.assertEqual(core.extract_video_id("https://youtu.be/abc-123_DEF"), "abc-123_DEF")
        self.assertEqual(core.extract_video_id("https://www.youtube.com/shorts/abc-123_DEF"), "abc-123_DEF")
        self.assertEqual(core.extract_video_id("https://www.youtube.com/embed/abc-123_DEF"), "abc-123_DEF")
        self.assertEqual(
            core.youtube_channel_id_from_url("https://www.youtube.com/channel/UCvmGOqGlxOgpZDoszBbWxmA"),
            "UCvmGOqGlxOgpZDoszBbWxmA",
        )
        self.assertEqual(core.youtube_channel_ref_from_url("https://www.youtube.com/@ESSIGI"), "@ESSIGI")
        self.assertEqual(core.youtube_channel_url("@ESSIGI"), "https://www.youtube.com/@ESSIGI")
        self.assertEqual(core.youtube_channel_url("c/Example"), "https://www.youtube.com/c/Example")
        self.assertEqual(
            core.local_queue_target_from_url("http://127.0.0.1:8765/#playlist=PLexample"),
            ("playlist", "PLexample"),
        )
        self.assertEqual(
            core.local_queue_target_from_url("http://127.0.0.1:8765/#video=abc12345678"),
            ("video", "abc12345678"),
        )
        self.assertEqual(core.format_duration(65), "1:05")
        self.assertEqual(core.format_duration(3661), "1:01:01")
        self.assertEqual(core.bounded_int("140"), 100)
        self.assertEqual(core.bounded_int("-5"), 0)
        self.assertTrue(core.playlist_entry_is_unavailable("[Deleted video]"))
        self.assertTrue(core.playlist_entry_is_unavailable("Private video"))
        self.assertTrue(core.playlist_entry_is_unavailable("Regular title", "needs_auth"))
        self.assertFalse(core.playlist_entry_is_unavailable("Regular title", "public"))
        self.assertTrue(core.playlist_zero_result_is_suspicious(0, "HTTP Error 403", 1))
        self.assertFalse(core.playlist_zero_result_is_suspicious(1, "HTTP Error 403", 1))
        self.assertFalse(core.playlist_zero_result_is_suspicious(0, "", 1))
        self.assertFalse(core.playlist_zero_result_is_suspicious(0, "HTTP Error 403", 0))
        self.assertTrue(core.playlist_scan_is_incomplete(100, 101))
        self.assertFalse(core.playlist_scan_is_incomplete(101, 101))
        self.assertFalse(core.playlist_scan_is_incomplete(101, 0))
        self.assertTrue(core.playlist_scan_requires_exact_count({"visibility": "private"}))
        self.assertTrue(core.playlist_scan_requires_exact_count({"owner_channel_id": "", "visibility": ""}))
        self.assertFalse(core.playlist_scan_requires_exact_count({"owner_channel_id": "UCother"}))
        self.assertFalse(core.playlist_scan_requires_exact_count({}, known_owner_channel_id="UCother"))
        self.assertTrue(core.playlist_scan_requires_exact_count({}, known_visibility="private"))

    def test_playlist_owner_visibility_helpers(self) -> None:
        self.assertEqual(core.normalize_playlist_visibility(" Public playlist "), "public")
        self.assertEqual(core.split_playlist_owner_visibility("Private"), ("", "private"))
        self.assertEqual(core.split_playlist_owner_visibility("Gir Bot"), ("Gir Bot", ""))
        metadata = core.playlist_metadata_from_ytdlp_info(
            {"title": "Example", "uploader": "Gir Bot", "availability": "unlisted"},
            "PLexample",
        )
        self.assertEqual(metadata["owner"], "Gir Bot")
        self.assertEqual(metadata["visibility"], "")
        visibility_only = core.playlist_metadata_from_ytdlp_info(
            {"title": "Example", "availability": "unlisted"},
            "PLexample",
        )
        self.assertEqual(visibility_only["owner"], "")
        self.assertEqual(visibility_only["visibility"], "unlisted")
        with self.assertRaises(AssertionError):
            core.assert_playlist_owner_visibility({"owner_channel_id": "UCmine", "visibility": "public"})

    def test_extract_playlist_metadata_reads_page_header_count_and_visibility(self) -> None:
        initial_data = {
            "header": {
                "pageHeaderRenderer": {
                    "content": {
                        "pageHeaderViewModel": {
                            "metadata": {
                                "contentMetadataViewModel": {
                                    "metadataRows": [
                                        {
                                            "metadataParts": [
                                                {"text": {"content": "Playlist"}},
                                                {"text": {"content": "Unlisted"}},
                                                {"text": {"content": "150 videos"}},
                                                {"text": {"content": "143 views"}},
                                            ]
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }
        html = f"<script>var ytInitialData = {json.dumps(initial_data)};</script>"

        metadata = core.extract_playlist_metadata(html, "PLexample")

        self.assertEqual(metadata["video_count"], 150)
        self.assertTrue(metadata["has_video_count"])
        self.assertNotIn("view_count", metadata)
        self.assertEqual(metadata["visibility"], "unlisted")
        self.assertEqual(metadata["owner"], "")
        self.assertFalse(core.extract_playlist_metadata("<html></html>", "PLexample")["has_video_count"])

        owner_data = {
            "header": {
                "playlistHeaderRenderer": {
                    "title": {"simpleText": "Foreign Playlist"},
                    "ownerText": {
                        "runs": [
                            {
                                "text": "Other Channel",
                                "navigationEndpoint": {
                                    "browseEndpoint": {
                                        "browseId": "UCabcdefghijklmnopqrstuv",
                                    }
                                },
                            }
                        ]
                    },
                    "numVideosText": {"simpleText": "2 videos"},
                }
            }
        }
        owner_html = f"<script>var ytInitialData = {json.dumps(owner_data)};</script>"
        owner_metadata = core.extract_playlist_metadata(owner_html, "PLforeign")
        self.assertEqual(owner_metadata["owner"], "Other Channel")
        self.assertEqual(owner_metadata["owner_channel_id"], "UCabcdefghijklmnopqrstuv")

        attributed_data = {
            "header": {
                "pageHeaderRenderer": {
                    "content": {
                        "pageHeaderViewModel": {
                            "metadata": {
                                "contentMetadataViewModel": {
                                    "metadataRows": [
                                        {
                                            "metadataParts": [
                                                {
                                                    "avatarStack": {
                                                        "avatarStackViewModel": {
                                                            "text": {
                                                                "content": "by alt Tabby",
                                                                "commandRuns": [
                                                                    {
                                                                        "onTap": {
                                                                            "innertubeCommand": {
                                                                                "browseEndpoint": {
                                                                                    "browseId": "UC9M9ViKcwu5rdRwLDmernrg",
                                                                                    "canonicalBaseUrl": "/@alttabby3633",
                                                                                }
                                                                            }
                                                                        }
                                                                    }
                                                                ],
                                                            },
                                                            "avatar": {
                                                                "avatarViewModel": {
                                                                    "image": {
                                                                        "sources": [
                                                                            {
                                                                                "url": "https://yt3.example/small.jpg",
                                                                                "width": 48,
                                                                            },
                                                                            {
                                                                                "url": "https://yt3.example/large.jpg",
                                                                                "width": 176,
                                                                            },
                                                                        ]
                                                                    }
                                                                }
                                                            },
                                                        }
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            "metadataParts": [
                                                {"text": {"content": "Playlist"}},
                                                {"text": {"content": "361 videos"}},
                                                {"text": {"content": "320 views"}},
                                            ]
                                        },
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }
        attributed_html = f"<script>var ytInitialData = {json.dumps(attributed_data)};</script>"
        attributed_metadata = core.extract_playlist_metadata(attributed_html, "PLforeign")
        self.assertEqual(attributed_metadata["owner"], "alt Tabby")
        self.assertEqual(attributed_metadata["owner_channel_id"], "UC9M9ViKcwu5rdRwLDmernrg")
        self.assertEqual(attributed_metadata["owner_thumbnail_url"], "https://yt3.example/large.jpg")
        self.assertEqual(attributed_metadata["video_count"], 361)
        self.assertFalse(core.playlist_scan_requires_exact_count(attributed_metadata))

    def test_playlist_continuation_prefers_command_executor_token(self) -> None:
        data = {
            "continuationItemRenderer": {
                "continuationEndpoint": {
                    "continuationCommand": {"token": "wrong-token"},
                    "commandExecutorCommand": {
                        "commands": [
                            {"playlistVotingRefreshPopupCommand": {}},
                            {"continuationCommand": {"token": "playlist-token"}},
                        ]
                    },
                }
            }
        }

        self.assertEqual(core.playlist_continuation_token(data), "playlist-token")

    def test_playlist_continuation_reads_view_model_token(self) -> None:
        data = {
            "continuationItemViewModel": {
                "continuationCommand": {
                    "innertubeCommand": {
                        "continuationCommand": {"token": "view-model-token"}
                    }
                }
            }
        }

        self.assertEqual(core.playlist_continuation_token(data), "view-model-token")

    def test_parse_takeout_watch_history_json(self) -> None:
        rows = core.parse_takeout_watch_history_text(
            """
            [
              {
                "title": "Watched Example Video",
                "titleUrl": "https://www.youtube.com/watch?v=vid123",
                "subtitles": [{
                  "name": "Example Channel",
                  "url": "https://www.youtube.com/channel/UCvmGOqGlxOgpZDoszBbWxmA"
                }],
                "time": "2026-07-04T05:27:45.123Z"
              }
            ]
            """
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["video_id"], "vid123")
        self.assertEqual(rows[0]["title"], "Example Video")
        self.assertEqual(rows[0]["channel"], "Example Channel")
        self.assertEqual(rows[0]["channel_id"], "UCvmGOqGlxOgpZDoszBbWxmA")

    def test_import_history_syncs_takeout_subscriptions(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            core.ROOT = root
            try:
                db_path = root / "library.sqlite3"
                core.migrate_database(db_path)
                zip_path = root / "takeout-20260704T052745Z-001.zip"
                with zipfile.ZipFile(zip_path, "w") as zf:
                    zf.writestr(
                        "Takeout/YouTube and YouTube Music/history/watch-history.json",
                        json.dumps(
                            [
                                {
                                    "title": "Watched Example Video",
                                    "titleUrl": "https://www.youtube.com/watch?v=vid123",
                                    "subtitles": [
                                        {
                                            "name": "Example Channel",
                                            "url": "https://www.youtube.com/channel/UCvmGOqGlxOgpZDoszBbWxmA",
                                        }
                                    ],
                                    "time": "2026-07-04T05:27:45.123Z",
                                }
                            ]
                        ),
                    )
                    zf.writestr(
                        "Takeout/YouTube and YouTube Music/subscriptions/subscriptions.csv",
                        (
                            "Channel Id,Channel Url,Channel Title\n"
                            "UCsubscribed12345678901234,https://www.youtube.com/channel/UCsubscribed12345678901234,Subscribed Channel\n"
                        ),
                    )

                first_import = core.import_history(
                    argparse.Namespace(
                        db=str(db_path),
                        takeout=str(root),
                        history_key="",
                    )
                )
                conn = core.connect(db_path)
                try:
                    with conn:
                        conn.execute(
                            """
                            INSERT INTO history_events(
                              event_id, video_id, watch_date, time_precision,
                              source_type, match_type, youtube_ordinal, imported_at, updated_at
                            ) VALUES (
                              'youtube:7', 'vid123', '2026-07-03', 'date_only',
                              'youtube', 'youtube_only', 7, '2026-07-04T06:00:00Z', '2026-07-04T06:00:00Z'
                            )
                            """
                        )
                        core.rebuild_history_reconciliation(conn, "America/Los_Angeles")
                    subscribed = conn.execute(
                        "SELECT title, subscribed FROM channels WHERE channel_id = ?",
                        ("UCsubscribed12345678901234",),
                    ).fetchone()
                    history_count = conn.execute(
                        "SELECT COUNT(*) FROM history_events WHERE takeout_history_key IS NOT NULL"
                    ).fetchone()[0]
                finally:
                    conn.close()

                second_import = core.import_history(
                    argparse.Namespace(db=str(db_path), takeout=str(root), history_key="")
                )
                conn = core.connect(db_path)
                try:
                    matched_ordinal = conn.execute(
                        "SELECT youtube_ordinal FROM history_events WHERE takeout_history_key IS NOT NULL"
                    ).fetchone()[0]
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

        self.assertIsNotNone(subscribed)
        self.assertEqual(subscribed["title"], "Subscribed Channel")
        self.assertEqual(subscribed["subscribed"], 1)
        self.assertEqual(history_count, 1)
        self.assertEqual(matched_ordinal, 7)
        self.assertEqual(first_import["inserted_watch_rows"], 1)
        self.assertEqual(second_import["inserted_watch_rows"], 0)
        self.assertEqual(second_import["duplicate_watch_rows"], 1)

    def test_import_history_reads_all_takeout_zips_and_skips_duplicates(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            core.ROOT = root
            try:
                db_path = root / "library.sqlite3"
                core.migrate_database(db_path)
                duplicate = {
                    "title": "Watched Duplicate Video",
                    "titleUrl": "https://www.youtube.com/watch?v=dup123",
                    "subtitles": [{"name": "Example Channel", "url": "https://www.youtube.com/channel/UCvmGOqGlxOgpZDoszBbWxmA"}],
                    "time": "2026-07-04T05:27:45.123Z",
                }
                exports = [
                    (
                        "takeout-20260704T052745Z-001.zip",
                        [
                            duplicate,
                            {
                                "title": "Watched Older Video",
                                "titleUrl": "https://www.youtube.com/watch?v=old123",
                                "time": "2026-07-03T05:27:45.123Z",
                            },
                        ],
                    ),
                    (
                        "takeout-20260705T052745Z-001.zip",
                        [
                            duplicate,
                            {
                                "title": "Watched Newer Video",
                                "titleUrl": "https://www.youtube.com/watch?v=new123",
                                "time": "2026-07-05T05:27:45.123Z",
                            },
                        ],
                    ),
                ]
                for filename, rows in exports:
                    with zipfile.ZipFile(root / filename, "w") as zf:
                        zf.writestr(
                            "Takeout/YouTube and YouTube Music/history/watch-history.json",
                            json.dumps(rows),
                        )

                first_import = core.import_history(
                    argparse.Namespace(db=str(db_path), takeout=str(root), history_key="")
                )
                second_import = core.import_history(
                    argparse.Namespace(db=str(db_path), takeout=str(root), history_key="")
                )
                conn = core.connect(db_path)
                try:
                    rows = conn.execute(
                        """
                        SELECT video_id, watched_at, takeout_history_key
                        FROM history_events
                        WHERE takeout_history_key IS NOT NULL
                        ORDER BY watched_at
                        """
                    ).fetchall()
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

        self.assertEqual(len(rows), 3)
        self.assertEqual(first_import["inserted_watch_rows"], 3)
        self.assertEqual(first_import["duplicate_watch_rows"], 1)
        self.assertEqual(second_import["inserted_watch_rows"], 0)
        self.assertEqual(second_import["duplicate_watch_rows"], 4)
        self.assertEqual([row["video_id"] for row in rows], ["old123", "dup123", "new123"])
        self.assertEqual(
            sorted({row["takeout_history_key"] for row in rows}),
            ["20260704T052745Z", "20260705T052745Z"],
        )

    def test_extract_reaction_from_toggled_buttons(self) -> None:
        liked = {
            "segmentedLikeDislikeButtonViewModel": {
                "likeButtonViewModel": {
                    "toggleButtonViewModel": {
                        "isToggled": True,
                        "defaultIcon": {"iconType": "LIKE"},
                        "accessibilityText": "Unlike this video",
                    }
                }
            }
        }
        disliked = {
            "segmentedLikeDislikeButtonViewModel": {
                "dislikeButtonViewModel": {
                    "toggleButtonViewModel": {
                        "isToggled": True,
                        "defaultIcon": {"iconType": "DISLIKE"},
                        "accessibilityText": "Remove dislike",
                    }
                }
            }
        }

        self.assertEqual(core.extract_reaction_from_initial_data(liked), "L")
        self.assertEqual(core.extract_reaction_from_initial_data(disliked), "D")
        self.assertEqual(core.extract_reaction_from_initial_data({"isToggled": False}), "")

    def test_extract_reaction_from_like_status_entity(self) -> None:
        liked = {
            "segmentedLikeDislikeButtonViewModel": {
                "likeButtonViewModel": {
                    "likeButtonViewModel": {
                        "likeStatusEntity": {"likeStatus": "LIKE"}
                    }
                }
            }
        }
        disliked = {
            "segmentedLikeDislikeButtonViewModel": {
                "likeButtonViewModel": {
                    "likeButtonViewModel": {
                        "likeStatusEntity": {"likeStatus": "DISLIKE"}
                    }
                }
            }
        }
        indifferent = {
            "segmentedLikeDislikeButtonViewModel": {
                "likeButtonViewModel": {
                    "likeButtonViewModel": {
                        "likeStatusEntity": {"likeStatus": "INDIFFERENT"}
                    }
                }
            }
        }

        self.assertEqual(core.extract_reaction_from_initial_data(liked), "L")
        self.assertEqual(core.extract_reaction_from_initial_data(disliked), "D")
        self.assertEqual(core.extract_reaction_from_initial_data(indifferent), "")

    def test_extract_channel_handle_aliases_from_browse_endpoints(self) -> None:
        initial_data = {
            "tabs": [
                {
                    "tabRenderer": {
                        "endpoint": {
                            "commandMetadata": {
                                "webCommandMetadata": {
                                    "url": "/@DJICONmusic/featured",
                                },
                            },
                            "browseEndpoint": {
                                "browseId": "UCYrXHY9MvPNpoa3uSGatOrA",
                                "canonicalBaseUrl": "/@DJICONmusic",
                            },
                        },
                    },
                },
            ],
        }

        self.assertEqual(core.extract_channel_handle_aliases(initial_data), "@DJICONmusic")

    def test_resolve_metadata_target_for_direct_ids(self) -> None:
        self.assertEqual(core.resolve_metadata_target(None, "abc-123_DEF"), ("video", "abc-123_DEF"))
        self.assertEqual(
            core.resolve_metadata_target(None, "UCvmGOqGlxOgpZDoszBbWxmA"),
            ("channel", "UCvmGOqGlxOgpZDoszBbWxmA"),
        )

    def test_useful_video_metadata_rejects_youtube_unavailable_placeholder(self) -> None:
        self.assertFalse(
            core.useful_video_metadata(
                {
                    "title": "- YouTube",
                    "yt_status": "ERROR: Video unavailable",
                    "channel_id": "",
                }
            )
        )
        self.assertTrue(
            core.useful_video_metadata(
                {
                    "title": "Recovered title",
                    "yt_status": "DELETED_FULL_META",
                    "channel_id": "UC95ANqPeSKRNEH1CaCOs2ew",
                }
            )
        )
        self.assertTrue(
            core.useful_video_metadata(
                {
                    "title": "",
                    "yt_status": "DELETED_ID_ONLY",
                    "channel_id": "UCWglcpI-xTAXb_QYecQ2O4g",
                    "thumbnail_path": "video_thumbs/aeXIgKuX_zY.jpg",
                }
            )
        )

    def test_unavailable_watch_metadata_does_not_keep_header_channel(self) -> None:
        html = """
        <html><head><title>- YouTube</title></head><body>
        <script>
        var ytInitialPlayerResponse = {
          "playabilityStatus": {"status": "ERROR", "reason": {"simpleText": "Video unavailable"}},
          "videoDetails": {},
          "microformat": {"playerMicroformatRenderer": {}}
        };
        var ytInitialData = {
          "metadata": {"channelMetadataRenderer": {
            "externalId": "UCnUc4Kc09vNJ3yBu6-MJHTQ",
            "title": "Gir Bot",
            "ownerUrls": ["https://www.youtube.com/channel/UCnUc4Kc09vNJ3yBu6-MJHTQ"]
          }}
        };
        </script>
        </body></html>
        """

        metadata = core.extract_watch_metadata(html, "vy_t101tY1I")

        self.assertEqual(metadata["yt_status"], "ERROR: Video unavailable")
        self.assertEqual(metadata["channel_id"], "")
        self.assertEqual(metadata["channel"], "")
        self.assertEqual(metadata["channel_url"], "")
        self.assertEqual(metadata["channel_thumbnail_url"], "")

    def test_watch_metadata_exposes_raw_playability_status(self) -> None:
        html = """
        <html><body>
        <script>
        var ytInitialPlayerResponse = {
          "playabilityStatus": {"status": "OK"},
          "videoDetails": {"title": "Members video", "author": "Creator"},
          "microformat": {"playerMicroformatRenderer": {}}
        };
        var ytInitialData = {};
        </script>
        </body></html>
        """

        metadata = core.extract_watch_metadata(html, "jhtY3OsTuwk")

        self.assertEqual(metadata["yt_status"], "OK")
        self.assertEqual(metadata["playability_status"], "OK")
        self.assertEqual(core.watch_playability_value(metadata), 1)

    def test_watch_metadata_classifies_members_only_playability(self) -> None:
        html = """
        <html><body>
        <script>
        var ytInitialPlayerResponse = {
          "playabilityStatus": {
            "status": "UNPLAYABLE",
            "reason": "Join this channel to get access to members-only content like this video.",
            "errorScreen": {
              "playerLegacyDesktopYpcOfferRenderer": {
                "itemTitle": "Members-only content",
                "offerDescription": "Join this channel to get access.",
                "offerId": "sponsors_only_video"
              }
            }
          },
          "videoDetails": {
            "title": "Members video",
            "author": "Creator",
            "channelId": "UCmembers123456789012345"
          },
          "microformat": {"playerMicroformatRenderer": {}}
        };
        var ytInitialData = {};
        </script>
        </body></html>
        """

        metadata = core.extract_watch_metadata(html, "members1234")

        self.assertEqual(metadata["playability_status"], "UNPLAYABLE")
        self.assertEqual(metadata["availability"], "subscriber_only")
        self.assertEqual(core.storable_watch_playability_value(metadata), 0)
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.store_video_metadata(conn, metadata, "ok")
                stored = conn.execute(
                    """
                    SELECT is_playable, availability
                    FROM videos
                    WHERE video_id = 'members1234'
                    """
                ).fetchone()
                self.assertEqual(
                    dict(stored),
                    {"is_playable": 0, "availability": "subscriber_only"},
                )
            finally:
                conn.close()

    def test_watch_metadata_fetch_uses_only_the_direct_video_page(self) -> None:
        opener = Mock()
        video_id = "-AbC123_def"
        metadata = {
            "title": "Direct video",
            "thumbnail_url": "",
            "channel_thumbnail_url": "",
            "watch_progress_percent": "0",
            "watch_resume_seconds": "0",
        }
        with (
            patch.object(core, "request_text", return_value="watch page") as request_text,
            patch.object(core, "extract_watch_metadata", return_value=metadata),
            patch.object(core, "cache_video_thumbnail", return_value=""),
            patch.object(core, "cache_channel_thumbnail", return_value=""),
        ):
            result = core.fetch_watch_metadata(opener, video_id, Path("thumbs"))

        request_text.assert_called_once_with(
            opener,
            "https://www.youtube.com/watch?v=-AbC123_def",
        )
        self.assertEqual(result["watch_progress_percent"], "0")
        self.assertEqual(result["watch_resume_seconds"], "0")

    def test_watch_playability_updates_canonical_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                conn.execute(
                    "INSERT INTO playlists(playlist_id, title) VALUES ('PLmembers', 'Members')"
                )
                core.save_playlist_scan(
                    conn,
                    "PLmembers",
                    [
                        {
                            "playlist_id": "PLmembers",
                            "position": 1,
                            "video_id": "jhtY3OsTuwk",
                            "title": "Members video",
                            "channel_id": "",
                            "channel": "",
                            "duration_text": "",
                            "is_playable": 0,
                            "availability": "subscriber_only",
                            "url": "https://www.youtube.com/watch?v=jhtY3OsTuwk",
                        }
                    ],
                    "ok",
                    "",
                )

                changed = core.apply_watch_playability_to_playlist_rows(
                    conn,
                    "jhtY3OsTuwk",
                    {"playability_status": "OK"},
                )

                self.assertEqual(changed, 1)
                row = conn.execute(
                    """
                    SELECT is_playable, availability
                    FROM videos
                    WHERE video_id = 'jhtY3OsTuwk'
                    """
                ).fetchone()
                self.assertEqual(row["is_playable"], 1)
                self.assertEqual(row["availability"], "public")
            finally:
                conn.close()

    def test_metadata_error_playability_does_not_downgrade_known_public_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "vweQrjtAg0U",
                        title="Playlist title",
                        source="playlist",
                        is_playable=1,
                        availability="public",
                        updated_at="2026-07-10T23:20:04Z",
                    )
                    core.store_video_metadata(
                        conn,
                        {
                            "video_id": "vweQrjtAg0U",
                            "title": "Metadata title",
                            "channel_id": "UCddem5RlB3bQe99wyY49g0g",
                            "channel": "PeriscopeFilm",
                            "playability_status": "ERROR",
                            "yt_status": "ERROR: Video unavailable",
                            "watch_progress_percent": "0",
                            "watch_resume_seconds": "0",
                        },
                        "ok",
                        updated_at="2026-07-11T08:08:18Z",
                    )

                row = conn.execute(
                    """
                    SELECT title, is_playable, availability, fetched_at,
                           last_seen_available_at, last_checked_at
                    FROM videos
                    WHERE video_id = 'vweQrjtAg0U'
                    """
                ).fetchone()
                self.assertEqual(row["title"], "Metadata title")
                self.assertEqual(row["is_playable"], 1)
                self.assertEqual(row["availability"], "public")
                self.assertEqual(row["fetched_at"], "2026-07-11T08:08:18Z")
                self.assertEqual(row["last_seen_available_at"], "2026-07-10T23:20:04Z")
                self.assertEqual(row["last_checked_at"], "2026-07-11T08:08:18Z")
            finally:
                conn.close()

    def test_metadata_ok_playability_refreshes_known_public_seen_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "vweQrjtAg0U",
                        title="Playlist title",
                        source="playlist",
                        is_playable=1,
                        availability="public",
                        updated_at="2026-07-10T23:20:04Z",
                    )
                    core.store_video_metadata(
                        conn,
                        {
                            "video_id": "vweQrjtAg0U",
                            "title": "Metadata title",
                            "channel_id": "UCddem5RlB3bQe99wyY49g0g",
                            "channel": "PeriscopeFilm",
                            "playability_status": "OK",
                            "yt_status": "OK",
                            "watch_progress_percent": "0",
                            "watch_resume_seconds": "0",
                        },
                        "ok",
                        updated_at="2026-07-12T20:30:45Z",
                    )

                row = conn.execute(
                    """
                    SELECT is_playable, availability, last_seen_available_at
                    FROM videos
                    WHERE video_id = 'vweQrjtAg0U'
                    """
                ).fetchone()
                self.assertEqual(row["is_playable"], 1)
                self.assertEqual(row["availability"], "public")
                self.assertEqual(row["last_seen_available_at"], "2026-07-12T20:30:45Z")
            finally:
                conn.close()

    def test_metadata_zero_progress_does_not_replace_positive_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "vweQrjtAg0U",
                        title="Progress video",
                        watch_progress_percent=64,
                        source="metadata",
                    )
                    core.store_video_metadata(
                        conn,
                        {
                            "video_id": "vweQrjtAg0U",
                            "title": "Progress video",
                            "watch_progress_percent": "0",
                        },
                        "ok",
                    )

                progress = conn.execute(
                    """
                    SELECT watch_progress_percent
                    FROM videos
                    WHERE video_id = 'vweQrjtAg0U'
                    """
                ).fetchone()["watch_progress_percent"]
                self.assertEqual(progress, 64)

                with conn:
                    core.store_video_metadata(
                        conn,
                        {
                            "video_id": "vweQrjtAg0U",
                            "title": "Progress video",
                            "watch_progress_percent": "37",
                        },
                        "ok",
                    )

                progress = conn.execute(
                    """
                    SELECT watch_progress_percent
                    FROM videos
                    WHERE video_id = 'vweQrjtAg0U'
                    """
                ).fetchone()["watch_progress_percent"]
                self.assertEqual(progress, 37)
            finally:
                conn.close()

    def test_metadata_from_archivarix_video_includes_channel_metadata(self) -> None:
        metadata = core.metadata_from_archivarix_video(
            "Ax8Yn8DPZe0",
            {
                "title": "Why Do Windshields Have Those Small Black Dots?",
                "description": "Video description",
                "channelExternalId": "UC95ANqPeSKRNEH1CaCOs2ew",
                "channelTitle": "History of Simple Things",
                "channelUrl": "https://www.youtube.com/channel/UC95ANqPeSKRNEH1CaCOs2ew",
                "channelDescription": "Channel description",
                "channelAliases": "youtube.com/@historyofsimplethings",
                "channelThumbnailUrl": "https://yt3.example/avatar.jpg",
                "channelThumbnailPath": "video_thumbs/UC95ANqPeSKRNEH1CaCOs2ew.jpg",
                "channelId": "12345",
                "channelStatus": "deleted",
                "channelStatusReason": "Deleted/terminated channel reported by Archivarix.",
                "duration": 488,
                "viewCount": 399359,
                "uploadDate": "2025-03-20",
                "status": "DELETED_FULL_META",
            },
            "https://archive.example/thumb.jpg",
            "video_thumbs/Ax8Yn8DPZe0.jpg",
        )

        self.assertEqual(metadata["channel_id"], "UC95ANqPeSKRNEH1CaCOs2ew")
        self.assertEqual(metadata["channel"], "History of Simple Things")
        self.assertEqual(metadata["channel_description"], "Channel description")
        self.assertEqual(metadata["channel_aliases"], "youtube.com/@historyofsimplethings")
        self.assertEqual(metadata["archivarix_channel_id"], "12345")
        self.assertEqual(metadata["channel_status"], "deleted")
        self.assertEqual(metadata["duration_text"], "8:08")

    def test_playlist_match_type_helpers_keep_notes_out_of_rows(self) -> None:
        self.assertEqual(core.playlist_match_type_label("ambiguous_hidden_candidate"), "Takeout candidate")
        self.assertEqual(
            core.playlist_match_type_note("ambiguous_hidden_candidate"),
            "missing from current playable scan; hidden slot mapping is ambiguous",
        )
        self.assertEqual(
            core.reconciled_video_availability("Ax8Yn8DPZe0", "", "LIVE"),
            "public",
        )
        self.assertEqual(core.reconciled_video_availability("Ax8Yn8DPZe0", "live", ""), "public")
        self.assertEqual(core.reconciled_video_availability("Ax8Yn8DPZe0", "", "", 1), "public")
        self.assertEqual(core.reconciled_video_availability("Ax8Yn8DPZe0", "subscriber_only", "", 0), "subscriber_only")
        self.assertEqual(core.reconciled_video_availability("", "private", "LIVE"), "unknown")

    def test_history_reconciliation_labels_describe_current_fields(self) -> None:
        self.assertEqual(core.history_source_type_label("takeout_youtube"), "Takeout + YouTube")
        self.assertEqual(core.history_match_type_label("video_id_date"), "matched by video/date")
        self.assertEqual(core.history_time_quality_label("unknown"), "time unknown")
        self.assertIn("observed_at", core.history_time_quality_note("unknown"))

    def test_history_day_overlap_requires_two_complete_matching_days(self) -> None:
        tracker = core.HistoryDayOverlapTracker(
            {
                "2026-07-28": Counter({"repeat123": 1}),
                "2026-07-27": Counter({"known-a": 1, "known-b": 1}),
                "2026-07-26": Counter({"known-c": 1}),
            }
        )

        reached = tracker.add_rows(
            [
                {"video_id": "repeat123", "watch_date": "2026-07-28"},
                {"video_id": "repeat123", "watch_date": "2026-07-28"},
                {"video_id": "known-a", "watch_date": "2026-07-27"},
            ]
        )
        self.assertFalse(reached)

        reached = tracker.add_rows(
            [
                {"video_id": "known-b", "watch_date": "2026-07-27"},
                {"video_id": "known-c", "watch_date": "2026-07-26"},
                {"video_id": "older", "watch_date": "2026-07-25"},
            ]
        )

        self.assertTrue(reached)
        self.assertEqual(tracker.confirmed_days, ["2026-07-27", "2026-07-26"])

    def test_youtube_history_occurrence_counts_preserve_same_day_rewatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(conn, "repeat123", title="Repeat", source="test")
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watch_date, time_precision,
                          source_type, match_type, youtube_ordinal
                        )
                        VALUES ('legacy-position-id', 'repeat123', '2026-07-28',
                                'date_only', 'youtube', 'youtube_only', 1)
                        """
                    )
                snapshot = core.youtube_history_occurrence_snapshot(conn)

                with conn:
                    stats = core.save_youtube_history_events(
                        conn,
                        [
                            {"video_id": "repeat123", "watch_date": "2026-07-28"},
                            {"video_id": "repeat123", "watch_date": "2026-07-28"},
                        ],
                        1,
                        snapshot,
                        Counter(),
                    )

                rows = conn.execute(
                    """
                    SELECT event_id, youtube_ordinal
                    FROM history_events
                    WHERE video_id = 'repeat123' AND watch_date = '2026-07-28'
                    ORDER BY youtube_ordinal
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(stats["existing"], 1)
        self.assertEqual(stats["new"], 1)
        self.assertEqual(rows[0]["event_id"], "legacy-position-id")
        self.assertNotEqual(rows[1]["event_id"], "youtube:2")
        self.assertEqual([row["youtube_ordinal"] for row in rows], [1, 2])

    def test_youtube_takeout_match_count_is_scoped_to_the_fetched_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    for ordinal in (5, 10):
                        video_id = f"matched-{ordinal}"
                        core.upsert_video(conn, video_id, title=video_id, source="test")
                        conn.execute(
                            """
                            INSERT INTO history_events(
                              event_id, video_id, watched_at, watch_date, time_precision,
                              source_type, match_type, youtube_ordinal,
                              takeout_history_key, takeout_row_key
                            )
                            VALUES (?, ?, '2026-07-28T12:00:00Z', '2026-07-28', 'exact',
                                    'takeout_youtube', 'video_id_date', ?, 'takeout', ?)
                            """,
                            (f"takeout-{ordinal}", video_id, ordinal, f"row-{ordinal}"),
                        )

                first_batch = core.youtube_takeout_match_count(conn, 1, 5)
                second_batch = core.youtube_takeout_match_count(conn, 6, 5)
            finally:
                conn.close()

        self.assertEqual(first_batch, 1)
        self.assertEqual(second_batch, 1)

    def test_youtube_history_order_normalizes_legacy_duplicate_ordinals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    for event_id, video_id, ordinal in (
                        ("event-a", "video-a", 1),
                        ("event-b", "video-b", 1),
                        ("event-c", "video-c", 3),
                    ):
                        core.upsert_video(conn, video_id, title=video_id, source="test")
                        conn.execute(
                            """
                            INSERT INTO history_events(
                              event_id, video_id, watch_date, time_precision,
                              source_type, match_type, youtube_ordinal
                            )
                            VALUES (?, ?, '2026-07-28', 'date_only',
                                    'youtube', 'youtube_only', ?)
                            """,
                            (event_id, video_id, ordinal),
                        )
                snapshot = core.youtube_history_occurrence_snapshot(conn)

                with conn:
                    core.synchronize_youtube_history_order(
                        conn,
                        snapshot,
                        set(),
                        processed=0,
                        shift=0,
                    )

                ordinals = [
                    row["youtube_ordinal"]
                    for row in conn.execute(
                        "SELECT youtube_ordinal FROM history_events ORDER BY youtube_ordinal"
                    )
                ]
            finally:
                conn.close()

        self.assertEqual(ordinals, [1, 2, 3])

    def test_canonical_video_prefers_current_youtube_and_retains_unavailable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(conn, "video123", title="Takeout title", source="takeout")
                    core.upsert_video(conn, "video123", title="Current title", source="playlist", is_playable=1)
                    core.upsert_video(conn, "video123", title="Older export title", source="takeout")
                    core.upsert_video(
                        conn,
                        "video123",
                        title="Deleted video",
                        source="metadata",
                        is_playable=0,
                        availability="deleted",
                    )
                row = conn.execute(
                    "SELECT title, is_playable, availability FROM videos WHERE video_id = 'video123'"
                ).fetchone()
                self.assertEqual(dict(row), {"title": "Current title", "is_playable": 0, "availability": "deleted"})
            finally:
                conn.close()

    def test_refresh_exact_history_dates_uses_iana_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(conn, "video123", title="Example", source="takeout")
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watched_at, watch_date, time_precision,
                          source_type, match_type, imported_at, updated_at
                        ) VALUES (
                          'takeout:one', 'video123', '2026-07-04T05:27:45Z', '2026-07-04', 'exact',
                          'takeout', 'takeout_only', '2026-07-04T06:00:00Z', '2026-07-04T06:00:00Z'
                        )
                        """
                    )
                    core.refresh_exact_history_dates(conn, "America/Los_Angeles")
                watch_date = conn.execute(
                    "SELECT watch_date FROM history_events WHERE event_id = 'takeout:one'"
                ).fetchone()[0]
                self.assertEqual(watch_date, "2026-07-03")
            finally:
                conn.close()


class SchemaTests(unittest.TestCase):
    def test_migrate_bootstraps_exact_schema_sql_shape(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                db_path = Path(temp_dir) / "library.sqlite3"
                conn = core.connect(db_path)
                try:
                    before_tables = {
                        row["name"]
                        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                    }
                finally:
                    conn.close()
                self.assertEqual(before_tables, set())
                core.migrate_database(db_path)
                expected = sqlite3.connect(":memory:")
                expected.row_factory = sqlite3.Row
                expected.executescript(core.SCHEMA)
                actual = core.connect(db_path)
                try:
                    expected_tables = {
                        row["name"]
                        for row in expected.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    actual_tables = {
                        row["name"]
                        for row in actual.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    expected_indexes = {
                        row["name"]
                        for row in expected.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    actual_indexes = {
                        row["name"]
                        for row in actual.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    expected_columns = {
                        table: [
                            row["name"]
                            for row in expected.execute(f"PRAGMA table_info({table})")
                        ]
                        for table in expected_tables
                    }
                    actual_columns = {
                        table: [
                            row["name"]
                            for row in actual.execute(f"PRAGMA table_info({table})")
                        ]
                        for table in actual_tables
                    }
                finally:
                    expected.close()
                    actual.close()
            finally:
                core.ROOT = original_root

        self.assertEqual(actual_tables, expected_tables)
        self.assertEqual(actual_columns, expected_columns)
        self.assertEqual(actual_indexes, expected_indexes)
        self.assertIn("idx_channels_title", actual_indexes)
        self.assertIn("idx_history_events_video", actual_indexes)

    def test_migrate_is_schema_only_for_existing_legacy_tables(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                db_path = Path(temp_dir) / "library.sqlite3"
                raw = sqlite3.connect(db_path)
                try:
                    raw.execute(
                        """
                        CREATE TABLE legacy_marker (
                          value TEXT NOT NULL
                        )
                        """
                    )
                    raw.execute("INSERT INTO legacy_marker(value) VALUES ('kept')")
                    raw.commit()
                finally:
                    raw.close()

                core.migrate_database(db_path)
                conn = core.connect(db_path)
                try:
                    tables = {
                        row["name"]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    marker = conn.execute("SELECT value FROM legacy_marker").fetchone()["value"]
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

        self.assertIn("playlists", tables)
        self.assertIn("legacy_marker", tables)
        self.assertEqual(marker, "kept")

    def test_migrate_removes_legacy_app_settings_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            raw = sqlite3.connect(db_path)
            try:
                raw.execute(
                    """
                    CREATE TABLE app_settings (
                      setting_key TEXT PRIMARY KEY,
                      value TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    )
                    """
                )
                raw.execute(
                    """
                    CREATE TABLE schema_migrations (
                      version INTEGER PRIMARY KEY,
                      applied_at TEXT NOT NULL
                    )
                    """
                )
                raw.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-07-01T00:00:00Z')")
                raw.execute(
                    """
                    INSERT INTO app_settings(setting_key, value, updated_at)
                    VALUES ('display_timezone', 'America/Los_Angeles', '2026-07-01T00:00:00Z')
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                schema_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            finally:
                conn.close()

        self.assertNotIn("app_settings", tables)
        self.assertEqual(schema_version, core.SCHEMA_VERSION)

    def test_migrate_tracks_prior_archivarix_requests_from_placeholder_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(
                    """
                    CREATE TABLE schema_migrations (
                      version INTEGER PRIMARY KEY,
                      applied_at TEXT NOT NULL
                    );
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (4, '2026-07-14T00:00:00Z');
                    CREATE TABLE placeholder_recovery_worker_runs (
                      run_id TEXT PRIMARY KEY,
                      status TEXT NOT NULL DEFAULT '',
                      started_at TEXT NOT NULL,
                      finished_at TEXT,
                      total INTEGER NOT NULL DEFAULT 1,
                      processed INTEGER NOT NULL DEFAULT 0,
                      found INTEGER NOT NULL DEFAULT 0,
                      failed INTEGER NOT NULL DEFAULT 0,
                      queue_id INTEGER NOT NULL DEFAULT 0,
                      video_id TEXT NOT NULL DEFAULT '',
                      playlist_id TEXT NOT NULL DEFAULT '',
                      recovery_status TEXT NOT NULL DEFAULT '',
                      message TEXT NOT NULL DEFAULT ''
                    );
                    INSERT INTO placeholder_recovery_worker_runs(
                      run_id, status, started_at, recovery_status
                    ) VALUES
                      ('requested', 'complete', '2026-07-14T01:00:00Z', 'not_found'),
                      ('auth-failed', 'blocked', '2026-07-14T02:00:00Z', 'authentication_error'),
                      ('never-started', 'stopped', '2026-07-14T03:00:00Z', '');
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT run_id, request_started_at, request_count
                    FROM placeholder_recovery_worker_runs
                    ORDER BY started_at
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(rows[0]["request_started_at"], "2026-07-14T01:00:00Z")
        self.assertEqual(rows[0]["request_count"], 1)
        self.assertIsNone(rows[1]["request_started_at"])
        self.assertEqual(rows[1]["request_count"], 0)
        self.assertIsNone(rows[2]["request_started_at"])
        self.assertEqual(rows[2]["request_count"], 0)

    def test_migrate_marks_takeout_playlists_as_library_playlists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            legacy_schema = core.SCHEMA.replace(
                "  is_library_playlist INTEGER NOT NULL DEFAULT 0 "
                "CHECK (is_library_playlist IN (0, 1)),\n",
                "",
            )
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(legacy_schema)
                raw.execute("DELETE FROM schema_migrations")
                raw.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (6, '2026-07-28T00:00:00Z')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO playlists(playlist_id, title)
                    VALUES ('PLtakeout', 'Takeout playlist')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO videos(video_id, title)
                    VALUES ('takeoutvid1', 'Takeout video')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO playlist_items(
                      playlist_id, position, video_id, source_quality
                    )
                    VALUES ('PLtakeout', 1, 'takeoutvid1', 'takeout')
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                playlist = conn.execute(
                    """
                    SELECT is_library_playlist
                    FROM playlists
                    WHERE playlist_id = 'PLtakeout'
                    """
                ).fetchone()
                schema_version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(playlist["is_library_playlist"], 1)
        self.assertEqual(schema_version, core.SCHEMA_VERSION)

    def test_recent_channel_fetch_without_thumbnail_ages_out_of_metadata_queue(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
                try:
                    now = core.utc_now()
                    core.upsert_channel(
                        conn,
                        "UCvmGOqGlxOgpZDoszBbWxmA",
                        title="Example Channel",
                        thumbnail_path="",
                        source="test",
                        updated_at=now,
                    )
                    queued = core.metadata_queue_candidate_rows(conn, limit=10, stale_days=30)
                    self.assertEqual([row["video_id"] for row in queued], ["UCvmGOqGlxOgpZDoszBbWxmA"])

                    stats = core.rebuild_metadata_queue(conn, stale_days=30)
                    self.assertEqual(stats["inserted"], 1)
                    persisted = core.metadata_queue_rows(conn, limit=10)
                    self.assertEqual([row["video_id"] for row in persisted], ["UCvmGOqGlxOgpZDoszBbWxmA"])

                    with conn:
                        conn.execute(
                            """
                            INSERT INTO playlists(playlist_id, title)
                            VALUES ('PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4', 'Test playlist')
                            """
                        )
                        core.upsert_video(conn, "abc12345678", title="First", source="takeout")
                        core.upsert_video(conn, "def12345678", title="Second", source="takeout")
                        conn.executemany(
                            """
                            INSERT INTO playlist_items(playlist_id, position, video_id)
                            VALUES ('PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4', ?, ?)
                            """,
                            [(1, "abc12345678"), (2, "def12345678")],
                        )
                    with conn:
                        unified_youtube_playlist = core.enqueue_worker_queue_target(
                            conn,
                            "https://www.youtube.com/playlist?list=PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4",
                        )
                    self.assertEqual(unified_youtube_playlist["worker_type"], "playlist")
                    self.assertEqual(unified_youtube_playlist["source"], "youtube")
                    with conn:
                        core.clear_worker_queue(conn)
                        unified_local_playlist = core.enqueue_worker_queue_target(
                            conn,
                            "http://127.0.0.1:8765/#playlist=PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4",
                        )
                    self.assertEqual(unified_local_playlist["worker_type"], "playlist")
                    self.assertEqual(unified_local_playlist["source"], "local")
                    self.assertEqual(unified_local_playlist["queued_count"], "1")
                    self.assertEqual(core.worker_queue_type_count(conn, "playlist"), 1)
                    queued_local_rows = core.playlist_scan_queue_rows(conn, limit=10)
                    self.assertEqual(
                        [row["playlist_id"] for row in queued_local_rows],
                        ["PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4"],
                    )
                    playlist_video_rows = [
                        row
                        for row in core.metadata_queue_rows(conn, limit=10)
                        if row["metadata_source"] == "playlist"
                    ]
                    self.assertEqual(playlist_video_rows, [])

                    core.upsert_channel(
                        conn,
                        "UCvmGOqGlxOgpZDoszBbWxmA",
                        title="Example Channel",
                        thumbnail_path="",
                        fetch_status="no_metadata",
                        fetched_at=now,
                        source="test",
                        updated_at=now,
                    )
                    remaining = core.metadata_queue_candidate_rows(conn, limit=10, stale_days=30)
                    self.assertNotIn("UCvmGOqGlxOgpZDoszBbWxmA", [row["video_id"] for row in remaining])
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

    def test_history_metadata_candidates_sort_by_latest_watch_date_descending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    videos = [
                        ("old12345678", "A alphabetically first", "2026-01-01T00:00:00Z"),
                        ("new12345678", "Z alphabetically last", "2026-03-01T00:00:00Z"),
                        ("mid12345678", "M alphabetically middle", "2026-02-01T00:00:00Z"),
                    ]
                    for video_id, title, watched_at in videos:
                        core.upsert_video(conn, video_id, title=title, source="takeout")
                        conn.execute(
                            """
                            INSERT INTO history_events(
                              event_id, video_id, watched_at, watch_date, time_precision, source_type
                            )
                            VALUES (?, ?, ?, ?, 'exact', 'takeout')
                            """,
                            (f"takeout:{video_id}", video_id, watched_at, watched_at[:10]),
                        )

                candidates = core.metadata_queue_candidate_rows(conn, limit=10, stale_days=30)
                self.assertEqual(
                    [row["video_id"] for row in candidates],
                    ["new12345678", "mid12345678", "old12345678"],
                )

                with conn:
                    core.rebuild_metadata_queue(conn, stale_days=30)
                queued = core.metadata_queue_rows(conn, limit=10)
                self.assertEqual(
                    [row["video_id"] for row in queued],
                    ["new12345678", "mid12345678", "old12345678"],
                )
            finally:
                conn.close()

    def test_save_playlist_scan_updates_playlist_metadata(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
                try:
                    with conn:
                        conn.execute(
                            """
                            INSERT INTO playlists(
                              playlist_id, title, description, visibility, video_count,
                              thumbnail_url, thumbnail_path, fetch_status, fetch_error, updated_at
                            )
                            VALUES (
                              'PLrename', 'Old name', 'Old description', 'unlisted', 1,
                              'https://example.test/old.jpg', 'thumbs/PLrename.jpg',
                              'ok', '', '2026-07-01T00:00:00Z'
                            )
                            """
                        )
                        core.save_playlist_scan(
                            conn,
                            "PLrename",
                            [
                                {
                                    "playlist_id": "PLrename",
                                    "position": 1,
                                    "video_id": "abc12345678",
                                    "title": "Video",
                                    "channel_id": "",
                                    "channel": "",
                                    "duration_text": "1:00",
                                    "is_playable": 1,
                                    "availability": "LIVE",
                                    "url": "https://www.youtube.com/watch?v=abc12345678",
                                }
                            ],
                            "ok",
                            "",
                            playlist_metadata={
                                "title": "New name",
                                "description": "New description",
                                "owner": "New owner",
                                "owner_channel_id": "UCnewownerchannel123456789",
                                "visibility": "",
                                "video_count": 1,
                                "thumbnail_url": "https://example.test/new.jpg",
                                "url": "https://www.youtube.com/playlist?list=PLrename",
                            },
                        )
                    row = conn.execute(
                        "SELECT title, description, owner_channel_id, visibility, video_count, thumbnail_url, thumbnail_path FROM playlists WHERE playlist_id = 'PLrename'"
                    ).fetchone()
                    self.assertEqual(row["title"], "New name")
                    self.assertEqual(row["description"], "New description")
                    self.assertEqual(row["owner_channel_id"], "UCnewownerchannel123456789")
                    self.assertEqual(row["visibility"], "")
                    self.assertEqual(row["video_count"], 1)
                    self.assertEqual(row["thumbnail_url"], "https://example.test/new.jpg")
                    self.assertEqual(row["thumbnail_path"], "thumbs/PLrename.jpg")
                    channel = conn.execute(
                        "SELECT title, metadata_source FROM channels WHERE channel_id = 'UCnewownerchannel123456789'"
                    ).fetchone()
                    self.assertIsNotNone(channel)
                    self.assertEqual(channel["title"], "New owner")
                    self.assertEqual(channel["metadata_source"], "playlist_owner")
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

    def test_liked_video_sync_replaces_likes_without_creating_playlist_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(conn, "oldliked123", title="Old like", source="metadata")
                    core.upsert_video(conn, "disliked1234", title="Disliked", source="metadata")
                    conn.execute("UPDATE videos SET reaction = 'L' WHERE video_id = 'oldliked123'")
                    conn.execute("UPDATE videos SET reaction = 'D' WHERE video_id = 'disliked1234'")
                    count, unavailable = core.save_liked_video_reactions(
                        conn,
                        [
                            {
                                "video_id": "newliked123",
                                "title": "New like",
                                "channel_id": "UC_liked",
                                "channel": "Liked Channel",
                                "is_playable": True,
                            },
                            {
                                "video_id": "newliked123",
                                "title": "Duplicate",
                                "is_playable": True,
                            },
                        ],
                    )
                reactions = {
                    row["video_id"]: row["reaction"]
                    for row in conn.execute("SELECT video_id, reaction FROM videos")
                }
                self.assertEqual(count, 1)
                self.assertEqual(unavailable, 0)
                self.assertEqual(reactions["oldliked123"], "")
                self.assertEqual(reactions["newliked123"], "L")
                self.assertEqual(reactions["disliked1234"], "D")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlist_items").fetchone()[0], 0)

                with conn:
                    core.save_liked_video_reactions(
                        conn,
                        [{"video_id": "partial12345", "title": "Partial like", "is_playable": True}],
                        replace=False,
                    )
                merged_reactions = {
                    row["video_id"]: row["reaction"]
                    for row in conn.execute("SELECT video_id, reaction FROM videos WHERE reaction <> ''")
                }
                self.assertEqual(merged_reactions["newliked123"], "L")
                self.assertEqual(merged_reactions["partial12345"], "L")
                self.assertEqual(merged_reactions["disliked1234"], "D")
            finally:
                conn.close()

    def test_playlist_queue_rebuild_includes_liked_video_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLregular', 'Regular')")
                    stats = core.rebuild_playlist_scan_queue(conn, force=True)
                rows = core.playlist_scan_queue_rows(conn)
                self.assertEqual(stats["inserted"], 2)
                self.assertEqual([row["playlist_id"] for row in rows], ["LL", "PLregular"])
                self.assertEqual(rows[0]["title"], "Liked videos")
                with conn:
                    core.clear_playlist_scan_queue(conn)
                    core.enqueue_playlist_scan_item(conn, "LL", title="LL", manual=True)
                self.assertEqual(core.playlist_scan_queue_rows(conn)[0]["title"], "Liked videos")
            finally:
                conn.close()

    def test_save_playlist_scan_error_preserves_existing_counts(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
                try:
                    with conn:
                        conn.execute(
                            "INSERT INTO playlists(playlist_id, title) VALUES ('PLpartial', 'Partial scan')"
                        )
                        core.save_playlist_scan(
                            conn,
                            "PLpartial",
                            [
                                {
                                    "playlist_id": "PLpartial",
                                    "position": 1,
                                    "video_id": "abc12345678",
                                    "title": "Video",
                                    "channel_id": "",
                                    "channel": "",
                                    "duration_text": "1:00",
                                    "is_playable": 1,
                                    "availability": "LIVE",
                                    "url": "https://www.youtube.com/watch?v=abc12345678",
                                }
                            ],
                            "ok",
                            "",
                        )
                        core.save_playlist_scan_error(conn, "PLpartial", "Parsed 1 videos, but playlist metadata says 2 videos")
                    row = conn.execute(
                        "SELECT video_count, unavailable_count, scan_status, scan_error FROM playlist_scans WHERE playlist_id = 'PLpartial'"
                    ).fetchone()
                    self.assertEqual(row["video_count"], 1)
                    self.assertEqual(row["unavailable_count"], 0)
                    self.assertEqual(row["scan_status"], "error")
                    self.assertIn("metadata says 2", row["scan_error"])
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id = 'PLpartial'").fetchone()[0],
                        1,
                    )
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

    def test_save_playlist_missing_status_preserves_playlist_and_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, visibility, is_library_playlist
                        )
                        VALUES ('PLmissing', 'Missing', 'private', 1)
                        """
                    )
                    core.upsert_video(conn, "keptvideo01", title="Kept video", source="playlist")
                    conn.execute(
                        """
                        INSERT INTO playlist_items(
                          playlist_id, position, video_id, membership_state,
                          source_quality, match_type
                        )
                        VALUES (
                          'PLmissing', 1, 'keptvideo01', 'retained_unavailable',
                          'takeout', 'ambiguous_hidden_candidate'
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count, scan_status
                        ) VALUES ('PLmissing', '2026-07-28T00:00:00Z', 1, 0, 'ok')
                        """
                    )
                    self.assertEqual(
                        core.playlist_missing_status(conn, "PLmissing"),
                        "removed",
                    )
                    counts = core.save_playlist_missing_status(
                        conn,
                        "PLmissing",
                        "removed",
                        "authenticated YouTube 404",
                    )
                self.assertEqual(counts, (1, 0))
                playlist = conn.execute(
                    """
                    SELECT fetch_status, fetch_error
                    FROM playlists
                    WHERE playlist_id = 'PLmissing'
                    """
                ).fetchone()
                self.assertEqual(playlist["fetch_status"], "removed")
                self.assertIn("404", playlist["fetch_error"])
                scan = conn.execute(
                    """
                    SELECT video_count, scan_status
                    FROM playlist_scans
                    WHERE playlist_id = 'PLmissing'
                    """
                ).fetchone()
                self.assertEqual(dict(scan), {"video_count": 1, "scan_status": "removed"})
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM playlist_items WHERE playlist_id = 'PLmissing'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT title FROM videos WHERE video_id = 'keptvideo01'"
                    ).fetchone()[0],
                    "Kept video",
                )
                self.assertEqual(core.playlist_scan_candidate_rows(conn), [])
                self.assertEqual(
                    [row["playlist_id"] for row in core.playlist_scan_candidate_rows(conn, force=True)],
                    ["PLmissing"],
                )
            finally:
                conn.close()

    def test_playlist_missing_status_uses_unavailable_without_ownership_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(playlist_id, title, owner_channel_id)
                        VALUES ('PLforeign', 'Foreign', NULL)
                        """
                    )
                self.assertEqual(
                    core.playlist_missing_status(conn, "PLforeign"),
                    "unavailable",
                )
            finally:
                conn.close()

    def test_playlist_missing_status_uses_removed_for_library_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, visibility, is_library_playlist
                        )
                        VALUES ('PLlibrary', 'Library', 'private', 1)
                        """
                    )
                self.assertEqual(
                    core.playlist_missing_status(conn, "PLlibrary"),
                    "removed",
                )
            finally:
                conn.close()

    def test_recovered_live_video_is_playable(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
                try:
                    with conn:
                        conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('pl1', 'Playlist')")
                        core.save_video_recovery(
                            conn,
                            "KRhofr57Na8",
                            {"title": "Can You Safely Drink Your Own Pee?", "status": "LIVE"},
                            "found",
                            "",
                        )
                        conn.execute(
                            """
                            INSERT INTO playlist_items(
                              playlist_id, position, video_id, membership_state, source_quality, match_type
                            ) VALUES ('pl1', 1, 'KRhofr57Na8', 'retained_unavailable', 'takeout', 'ambiguous_hidden_candidate')
                            """
                        )

                    row = conn.execute(
                        """
                        SELECT is_playable, availability
                        FROM videos
                        WHERE video_id = 'KRhofr57Na8'
                        """
                    ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["is_playable"], 1)
                    self.assertEqual(row["availability"], "public")
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root


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


class AdminServerTests(unittest.TestCase):
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
        self.assertIn('id="themeToggle"', server.ADMIN_HTML)
        self.assertIn('aria-label="Use dark theme"', server.ADMIN_HTML)
        self.assertIn('<span>Light</span>', server.ADMIN_HTML)
        self.assertIn('<span>Dark</span>', server.ADMIN_HTML)
        self.assertIn('id="serviceStatus"', server.ADMIN_HTML)
        self.assertIn("`Running${service.pid ? ` (${service.pid})` : ''}`", server.ADMIN_HTML)
        self.assertIn('id="restartService"', server.ADMIN_HTML)
        self.assertIn('id="useProxy"', server.ADMIN_HTML)
        self.assertIn('id="proxyUrl"', server.ADMIN_HTML)
        self.assertIn('id="retryProxy"', server.ADMIN_HTML)
        self.assertIn('id="proxyBlock"', server.ADMIN_HTML)
        self.assertIn('<option value="queue">Queue</option>', server.ADMIN_HTML)
        self.assertIn("startsWith('queue ')", server.ADMIN_HTML)
        self.assertIn('id="saveSettings"', server.ADMIN_HTML)
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
        self.assertEqual(server.INDEX_HTML.count('<input type="checkbox" data-meta-all-filter='), 3)
        self.assertEqual(server.INDEX_HTML.count('<input type="checkbox" data-meta-child-filter='), 10)
        self.assertEqual(server.INDEX_HTML.count("videoStatusFiltersHtml({"), 4)
        self.assertIn("['videos', 'available', '']", server.INDEX_HTML)
        self.assertIn('data-filter="members_only_videos"', server.INDEX_HTML)
        self.assertIn(".badge.members-only-badge", server.INDEX_HTML)
        self.assertIn("'subscriber_only', 'members only'", server.VIDEO_CARD_JS)
        self.assertIn("members-only-icon", server.VIDEO_CARD_JS)
        self.assertIn("M6 .5a5.5 5.5 0 100 11", server.VIDEO_CARD_JS)
        self.assertIn("syncMetaFilterGroup('playlist-videos')", server.INDEX_HTML)
        self.assertIn("syncMetaFilterGroup('liked-videos')", server.INDEX_HTML)
        self.assertIn("syncMetaFilterGroup('channels')", server.INDEX_HTML)
        self.assertIn("syncMetaFilterGroup('playlist-list')", server.INDEX_HTML)
        self.assertIn("storedTheme() || 'dark'", server.THEME_JS)
        self.assertIn("fields.themeToggle.checked ? 'dark' : 'light'", server.ADMIN_HTML)

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


class WorkerQueueTests(unittest.TestCase):
    def test_recent_history_uses_small_batch_and_stops_after_two_matching_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            existing = [
                ("known-a", "2026-07-27"),
                ("known-b", "2026-07-27"),
                ("known-c", "2026-07-26"),
                ("known-d", "2026-07-26"),
            ]
            try:
                with conn:
                    for ordinal, (video_id, watch_date) in enumerate(existing, start=1):
                        core.upsert_video(conn, video_id, title=video_id, source="test")
                        if ordinal == 1:
                            conn.execute(
                                """
                                INSERT INTO history_events(
                                  event_id, video_id, watched_at, watch_date, time_precision,
                                  source_type, match_type, youtube_ordinal,
                                  takeout_history_key, takeout_row_key
                                )
                                VALUES (?, ?, '2026-07-27T12:00:00Z', ?, 'exact',
                                        'takeout_youtube', 'video_id_date', ?,
                                        'takeout', 'known-a-row')
                                """,
                                (f"existing-{ordinal}", video_id, watch_date, ordinal),
                            )
                        else:
                            conn.execute(
                                """
                                INSERT INTO history_events(
                                  event_id, video_id, watch_date, time_precision,
                                  source_type, match_type, youtube_ordinal
                                )
                                VALUES (?, ?, ?, 'date_only', 'youtube', 'youtube_only', ?)
                                """,
                                (f"existing-{ordinal}", video_id, watch_date, ordinal),
                            )
            finally:
                conn.close()

            fetched_rows = [
                {"video_id": "repeat-current", "watch_date": "2026-07-28"}
                for _ in range(195)
            ]
            fetched_rows.extend(
                {"video_id": video_id, "watch_date": watch_date}
                for video_id, watch_date in existing
            )
            fetched_rows.append({"video_id": "older-new", "watch_date": "2026-07-25"})

            worker = LiveHistoryWorker()
            with patch.object(workers, "fetch_youtube_history_web", return_value=fetched_rows) as fetch:
                worker._run(
                    "recent-history-run",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    "recent",
                    "UTC",
                )

            second_worker = LiveHistoryWorker()
            with patch.object(workers, "fetch_youtube_history_web", return_value=fetched_rows) as second_fetch:
                second_worker._run(
                    "second-recent-history-run",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    "recent",
                    "UTC",
                )

            conn = core.connect(db_path)
            try:
                run = conn.execute(
                    "SELECT * FROM live_history_worker_runs WHERE run_id = 'recent-history-run'"
                ).fetchone()
                second_run = conn.execute(
                    "SELECT * FROM live_history_worker_runs WHERE run_id = 'second-recent-history-run'"
                ).fetchone()
                logs = conn.execute(
                    """
                    SELECT video_id, message FROM live_history_worker_log
                    WHERE run_id = 'recent-history-run'
                    ORDER BY rowid
                    """
                ).fetchall()
                event_counts = conn.execute(
                    """
                    SELECT COUNT(*) AS events,
                           COUNT(DISTINCT youtube_ordinal) AS distinct_ordinals,
                           MIN(youtube_ordinal) AS first_ordinal,
                           MAX(youtube_ordinal) AS last_ordinal
                    FROM history_events
                    WHERE youtube_ordinal IS NOT NULL
                    """
                ).fetchone()
                reconciled = conn.execute(
                    """
                    SELECT time_precision, watched_at, youtube_ordinal
                    FROM history_events
                    WHERE event_id = 'existing-1'
                    """
                ).fetchone()
                queued_metadata = core.metadata_queue_rows(conn)
            finally:
                conn.close()

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second_fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs["limit"], core.RECENT_HISTORY_BATCH_SIZE)
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["processed"], 200)
        self.assertEqual(run["found"], 196)
        self.assertEqual(run["skipped"], 4)
        self.assertIn("2 matching complete days", run["message"])
        self.assertTrue(
            any(
                "196 new watches, 4 existing watches, 1 Takeout matches" in message
                for message in (row["message"] for row in logs)
            )
        )
        self.assertTrue(all(row["video_id"] == "" for row in logs))
        self.assertEqual(second_run["found"], 0)
        self.assertEqual(second_run["skipped"], 200)
        self.assertEqual(dict(event_counts), {
            "events": 200,
            "distinct_ordinals": 200,
            "first_ordinal": 1,
            "last_ordinal": 200,
        })
        self.assertEqual(reconciled["time_precision"], "exact")
        self.assertEqual(reconciled["watched_at"], "2026-07-27T12:00:00Z")
        self.assertEqual(reconciled["youtube_ordinal"], 196)
        self.assertIn("2 metadata queued", run["message"])
        self.assertEqual(
            [row["video_id"] for row in queued_metadata],
            ["repeat-current", "older-new"],
        )

    def test_dispatcher_caps_concurrent_metadata_tasks_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    for index in range(3):
                        core.enqueue_metadata_item(
                            conn,
                            video_id=f"concurrent{index}",
                            current_title=f"Concurrent {index}",
                            metadata_source="history",
                            priority=index,
                        )
            finally:
                conn.close()

            release = threading.Event()
            two_started = threading.Event()
            state_lock = threading.Lock()
            active = 0
            peak = 0
            started = 0

            def fetch_metadata(_opener, video_id, _thumb_dir, **_kwargs):
                nonlocal active, peak, started
                with state_lock:
                    active += 1
                    started += 1
                    peak = max(peak, active)
                    if started >= 2:
                        two_started.set()
                release.wait(2)
                with state_lock:
                    active -= 1
                return {
                    "video_id": video_id,
                    "title": video_id,
                    "duration_text": "1:00",
                    "yt_status": "OK",
                }

            dispatcher = WorkerQueueDispatcher()
            config = load_config(Path(temp_dir) / "config.json")
            config.update(
                {
                    "dispatch_mode": "throttle",
                    "job_dispatch_delay_seconds": 10.0,
                    "youtube_max_in_flight": 2,
                    "archivarix_max_in_flight": 1,
                }
            )
            with (
                patch("yt_library.workers.fetch_watch_metadata", side_effect=fetch_metadata),
                patch("yt_library.workers.fetch_new_channel_metadata_if_needed", return_value=({}, "", "")),
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "missing-youtube-cookies.txt",
                    Path(temp_dir) / "thumbs",
                    config,
                )
                self.assertTrue(result["started"])
                self.assertTrue(two_started.wait(2))
                time.sleep(0.1)
                with state_lock:
                    self.assertEqual(peak, 2)
                    self.assertEqual(started, 2)
                release.set()
                deadline = time.time() + 3
                while dispatcher.is_running() and time.time() < deadline:
                    time.sleep(0.05)

            self.assertFalse(dispatcher.is_running())
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_count(conn), 0)
            finally:
                conn.close()

    def test_dispatcher_settings_changes_apply_during_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    for index in range(2):
                        core.enqueue_metadata_item(
                            conn,
                            video_id=f"retimed{index}",
                            current_title=f"Retimed {index}",
                            metadata_source="history",
                            priority=index,
                        )
            finally:
                conn.close()

            first_started = threading.Event()
            second_started = threading.Event()
            started_at: list[float] = []

            def fetch_metadata(_opener, video_id, _thumb_dir, **_kwargs):
                started_at.append(time.monotonic())
                if len(started_at) == 1:
                    first_started.set()
                else:
                    second_started.set()
                return {
                    "video_id": video_id,
                    "title": video_id,
                    "duration_text": "1:00",
                    "yt_status": "OK",
                }

            dispatcher = WorkerQueueDispatcher()
            config = load_config(Path(temp_dir) / "config.json")
            config.update(
                {
                    "dispatch_mode": "delay",
                    "job_dispatch_delay_seconds": 10.0,
                    "youtube_max_in_flight": 1,
                }
            )
            with (
                patch("yt_library.workers.fetch_watch_metadata", side_effect=fetch_metadata),
                patch("yt_library.workers.fetch_new_channel_metadata_if_needed", return_value=({}, "", "")),
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "missing-youtube-cookies.txt",
                    Path(temp_dir) / "thumbs",
                    config,
                )
                self.assertTrue(result["started"])
                self.assertTrue(first_started.wait(1))
                self.assertFalse(second_started.wait(0.2))

                settings = dispatcher.update_dispatch_settings(
                    "delay",
                    0.0,
                    2,
                    1,
                )
                self.assertEqual(
                    settings,
                    {
                        "dispatch_mode": "delay",
                        "job_dispatch_delay_seconds": 0.0,
                        "effective_job_dispatch_delay_seconds": 0.0,
                        "youtube_max_in_flight": 2,
                        "archivarix_max_in_flight": 1,
                    },
                )
                self.assertTrue(second_started.wait(1))

                deadline = time.time() + 2
                while dispatcher.is_running() and time.time() < deadline:
                    time.sleep(0.02)

            self.assertFalse(dispatcher.is_running())
            self.assertEqual(len(started_at), 2)
            self.assertLess(started_at[1] - started_at[0], 2.0)

    def test_dispatch_delay_is_global_across_worker_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLglobal', 'Global')"
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="global-delay-video",
                        current_title="Global delay video",
                        priority=0,
                    )
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLglobal",
                        priority=1,
                    )
            finally:
                conn.close()

            launches: list[tuple[str, float]] = []

            def metadata_start(
                _worker,
                worker_db_path,
                _cookie_file,
                _thumb_dir,
                **kwargs,
            ):
                launches.append(("metadata", time.monotonic()))
                queue_id = int(kwargs["queue_id"])
                worker_conn = core.connect(worker_db_path)
                try:
                    with worker_conn:
                        core.remove_worker_queue_entry(worker_conn, queue_id)
                finally:
                    worker_conn.close()
                return {"started": True, "run_id": "fake-metadata"}

            def playlist_start(worker_db_path, *_args, **_kwargs):
                launches.append(("playlist", time.monotonic()))
                worker_conn = core.connect(worker_db_path)
                try:
                    with worker_conn:
                        row = worker_conn.execute(
                            "SELECT queue_id FROM worker_queue WHERE worker_type = 'playlist'"
                        ).fetchone()
                        core.remove_worker_queue_entry(
                            worker_conn,
                            int(row["queue_id"]),
                        )
                finally:
                    worker_conn.close()
                return {"started": True, "run_id": "fake-playlist"}

            dispatcher = WorkerQueueDispatcher()
            config = load_config(Path(temp_dir) / "config.json")
            config.update(
                {
                    "dispatch_mode": "delay",
                    "job_dispatch_delay_seconds": 0.2,
                    "youtube_max_in_flight": 1,
                    "archivarix_max_in_flight": 1,
                }
            )
            with (
                patch.object(MetadataWorker, "start", new=metadata_start),
                patch.object(MetadataWorker, "is_alive", return_value=False),
                patch.object(MetadataWorker, "blocked_reason", return_value=""),
                patch.object(
                    workers.PLAYLIST_SCAN_WORKER,
                    "start",
                    side_effect=playlist_start,
                ),
                patch.object(
                    workers.PLAYLIST_SCAN_WORKER,
                    "is_running",
                    return_value=False,
                ),
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    config,
                )
                self.assertTrue(result["started"])
                deadline = time.time() + 2
                while dispatcher.is_running() and time.time() < deadline:
                    time.sleep(0.01)

            self.assertFalse(dispatcher.is_running())
            self.assertEqual([worker_type for worker_type, _ in launches], ["metadata", "playlist"])
            self.assertGreaterEqual(launches[1][1] - launches[0][1], 0.18)

    def test_youtube_authentication_block_does_not_stop_placeholder_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            youtube_cookie_file = Path(temp_dir) / "youtube-cookies.txt"
            youtube_cookie_file.write_text("provided", encoding="utf-8")
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="authblocked1",
                        current_title="Authentication blocked",
                        metadata_source="history",
                        priority=0,
                    )
                    conn.execute(
                        """
                        INSERT INTO worker_queue(
                          subject_key, worker_type, video_id, current_title,
                          priority, created_at, updated_at
                        )
                        VALUES ('placeholder:recoverme01', 'placeholder', 'recoverme01',
                                'Recover me', 0, ?, ?)
                        """,
                        (core.utc_now(), core.utc_now()),
                    )
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLyoutubeBlocked",
                        title="YouTube blocked playlist",
                        priority=1,
                    )
                    core.enqueue_history_task(conn, "recent", priority=1)
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            with (
                patch(
                    "yt_library.workers.youtube_session_status",
                    return_value=(False, "YouTube login session is not accepted by YouTube"),
                ),
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "ok")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(None, "", "", "not_found", ""),
                ),
            ):
                dispatcher._run(
                    db_path,
                    youtube_cookie_file,
                    Path(temp_dir) / "video-thumbs",
                    "UTC",
                    Path(temp_dir) / "archivarix-cookies.txt",
                    Path(temp_dir) / "archivarix-thumbs",
                    0.0,
                    1,
                    0.0,
                    1,
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "playlist"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "history"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 0)
                placeholder_run = conn.execute(
                    """
                    SELECT status, recovery_status, message
                    FROM placeholder_recovery_worker_runs
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(
                    tuple(placeholder_run),
                    ("complete", "not_found", "not found"),
                )
            finally:
                conn.close()

    def test_proxy_failure_stops_all_dispatch_and_retains_pending_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="proxyfail01",
                        current_title="Proxy failure",
                        metadata_source="history",
                        priority=0,
                    )
                    conn.execute(
                        """
                        INSERT INTO worker_queue(
                          subject_key, worker_type, video_id, current_title,
                          priority, created_at, updated_at
                        )
                        VALUES ('placeholder:proxyhold01', 'placeholder', 'proxyhold01',
                                'Proxy-held placeholder', 1, ?, ?)
                        """,
                        (core.utc_now(), core.utc_now()),
                    )
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            config = load_config(Path(temp_dir) / "config.json")
            config.update(
                {
                    "use_proxy": True,
                    "proxy": "socks5h://127.0.0.1:1081",
                    "job_dispatch_delay_seconds": 0,
                    "youtube_max_in_flight": 1,
                    "archivarix_max_in_flight": 1,
                }
            )

            def wait_for_stop(*_args, **kwargs):
                stop_event = kwargs["stop_event"]
                stop_event.wait(2)
                return None, "", "", "stopped", "Stop requested"

            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    side_effect=network.ProxyUnavailableError(
                        "SOCKS5 proxy 127.0.0.1:1081 is unavailable"
                    ),
                ),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    side_effect=wait_for_stop,
                ),
            ):
                dispatcher._run(
                    db_path,
                    Path(temp_dir) / "missing-youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    "UTC",
                    Path(temp_dir) / "archivarix-cookies.txt",
                    Path(temp_dir) / "archivarix-thumbs",
                    15.0,
                    30.0,
                    3,
                    0.0,
                    config["proxy"],
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                block = core.external_service_block(conn, "proxy")
                self.assertTrue(block["blocked"])
                self.assertEqual(block["reason_code"], "proxy_unavailable")
                self.assertEqual(block["queue_id"], 1)
                run = conn.execute(
                    """
                    SELECT status, processed, message
                    FROM metadata_worker_runs
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(run["status"], "blocked")
                self.assertEqual(run["processed"], 0)
                self.assertIn("Metadata worker paused", run["message"])
                queue_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    WHERE level = 'queue error'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(queue_log["level"], "queue error")
                self.assertIn("Worker queue paused", queue_log["message"])
                self.assertIn("pending items were retained", queue_log["message"])
            finally:
                conn.close()

    def test_playlist_proxy_failure_retains_queue_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLproxyhold', 'Proxy hold')"
                    )
                    core.enqueue_playlist_scan_item(conn, "PLproxyhold", manual=True)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.request_text",
                    side_effect=network.ProxyUnavailableError(
                        "SOCKS5 proxy 127.0.0.1:1081 is unavailable"
                    ),
                ),
            ):
                worker._run(
                    "playlist-proxy-hold",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=7,
                    record_summary=False,
                    proxy_url="socks5h://127.0.0.1:1081",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "playlist"), 1)
                self.assertTrue(core.external_service_block(conn, "proxy")["blocked"])
                run = conn.execute(
                    "SELECT status, message FROM playlist_scan_worker_runs WHERE run_id = ?",
                    ("playlist-proxy-hold",),
                ).fetchone()
                self.assertEqual(run["status"], "blocked")
                self.assertIn("Playlist scan paused", run["message"])
            finally:
                conn.close()

    def test_history_proxy_failure_retains_queue_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_history_task(conn, "recent", priority=0, manual=True)
            finally:
                conn.close()

            worker = LiveHistoryWorker()
            with patch(
                "yt_library.workers.fetch_youtube_history_web",
                side_effect=network.ProxyUnavailableError(
                    "SOCKS5 proxy 127.0.0.1:1081 is unavailable"
                ),
            ):
                worker._run(
                    "history-proxy-hold",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    "recent",
                    "UTC",
                    "socks5h://127.0.0.1:1081",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "history"), 1)
                self.assertTrue(core.external_service_block(conn, "proxy")["blocked"])
                run = conn.execute(
                    "SELECT status, message FROM live_history_worker_runs WHERE run_id = ?",
                    ("history-proxy-hold",),
                ).fetchone()
                self.assertEqual(run["status"], "blocked")
                self.assertIn("History fetch paused", run["message"])
            finally:
                conn.close()

    def test_placeholder_proxy_failure_retains_queue_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO worker_queue(
                          subject_key, worker_type, video_id, current_title,
                          priority, created_at, updated_at
                        )
                        VALUES ('placeholder:proxyhold02', 'placeholder', 'proxyhold02',
                                'Proxy-held placeholder', 0, ?, ?)
                        """,
                        (core.utc_now(), core.utc_now()),
                    )
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    side_effect=network.ProxyUnavailableError(
                        "SOCKS5 proxy 127.0.0.1:1081 is unavailable"
                    ),
                ),
            ):
                worker._run(
                    "placeholder-proxy-hold",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    proxy_url="socks5h://127.0.0.1:1081",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                self.assertTrue(core.external_service_block(conn, "proxy")["blocked"])
                self.assertTrue(core.admin_status(db_path)["proxyBlock"]["blocked"])
                self.assertFalse(core.external_service_block(conn, "archivarix")["blocked"])
                run = conn.execute(
                    """
                    SELECT status, recovery_status, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("placeholder-proxy-hold",),
                ).fetchone()
                self.assertEqual(run["status"], "blocked")
                self.assertEqual(run["recovery_status"], "proxy_unavailable")
                self.assertIn("Placeholder recovery paused", run["message"])
            finally:
                conn.close()

    def test_no_youtube_metadata_queues_archivarix_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="unavailable1",
                        current_title="Unavailable example",
                        metadata_source="history",
                        priority=7,
                    )
            finally:
                conn.close()

            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    return_value={"video_id": "unavailable1", "title": "", "yt_status": "ERROR"},
                ),
                patch("yt_library.workers.recover_archivarix_video") as recover,
            ):
                worker._run(
                    "test-archivarix-handoff",
                    db_path,
                    Path(temp_dir) / "missing-youtube-cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            recover.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 0)
                row = core.placeholder_worker_queue_rows(conn, limit=1)[0]
                self.assertEqual(row["video_id"], "unavailable1")
                self.assertEqual(row["priority"], 7)
                log = conn.execute(
                    """
                    SELECT message
                    FROM metadata_worker_log
                    WHERE run_id = 'test-archivarix-handoff'
                    """
                ).fetchone()
                self.assertEqual(
                    log["message"],
                    "no metadata from YouTube; placeholder recovery queued",
                )
            finally:
                conn.close()

    def test_metadata_worker_stops_when_cookie_authentication_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cookie_file = Path(temp_dir) / "cookies.txt"
            cookie_file.write_text("provided", encoding="utf-8")
            conn = migrated_connection(db_path)
            try:
                with conn:
                    for index in range(2):
                        core.enqueue_metadata_item(
                            conn,
                            video_id=f"authcheck{index}",
                            current_title=f"Auth check {index}",
                            metadata_source="history",
                            priority=index,
                        )
            finally:
                conn.close()

            metadata = {
                "video_id": "authcheck0",
                "title": "Authenticated metadata",
                "description": "",
                "channel_id": "",
                "channel": "",
                "channel_url": "",
                "duration_text": "1:00",
                "view_count": "",
                "upload_date": "",
                "thumbnail_url": "",
                "thumbnail_path": "",
                "channel_thumbnail_url": "",
                "channel_thumbnail_path": "",
                "reaction": "L",
                "watch_progress_percent": "0",
                "watch_resume_seconds": "0",
                "yt_status": "OK",
            }
            worker = MetadataWorker()
            with (
                patch(
                    "yt_library.workers.youtube_session_status",
                    return_value=(True, ""),
                ) as session_status,
                patch(
                    "yt_library.workers.cached_youtube_authentication_probe",
                    return_value=(
                        "yt_dlp_probe=cookies_rotated; deno=available; ejs=available"
                    ),
                ) as ytdlp_probe,
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    side_effect=[
                        metadata,
                        core.YouTubeAuthenticationError(
                            "YouTube login session is not accepted by YouTube",
                            "operation=watch page; logged_in=false; markers=bot_check",
                        ),
                    ],
                ) as fetch_metadata,
                patch("yt_library.workers.fetch_new_channel_metadata_if_needed", return_value=({}, "", "")),
            ):
                worker._run(
                    "test-auth-expired",
                    db_path,
                    cookie_file,
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=0,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            self.assertEqual(session_status.call_count, 2)
            self.assertEqual(fetch_metadata.call_count, 2)
            ytdlp_probe.assert_called_once_with(cookie_file, "")
            self.assertIn("not accepted", worker.blocked_reason())
            conn = core.connect(db_path)
            try:
                run = conn.execute(
                    "SELECT status, processed, message FROM metadata_worker_runs WHERE run_id = 'test-auth-expired'"
                ).fetchone()
                self.assertEqual(run["status"], "error")
                self.assertEqual(run["processed"], 1)
                self.assertIn("not accepted", run["message"])
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 1)
                remaining = core.metadata_queue_rows(conn)[0]
                self.assertEqual(remaining["video_id"], "authcheck1")
                debug_log = conn.execute(
                    """
                    SELECT level, video_id, message
                    FROM metadata_worker_log
                    WHERE run_id = 'test-auth-expired' AND level = 'debug'
                    """
                ).fetchone()
                self.assertEqual(debug_log["video_id"], "authcheck1")
                self.assertIn("operation=watch page", debug_log["message"])
                self.assertIn("logged_in=false", debug_log["message"])
                self.assertIn("yt_dlp_probe=cookies_rotated", debug_log["message"])
            finally:
                conn.close()

    def test_playlist_worker_caches_playlist_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            header = {
                "title": "Example",
                "video_count": 1,
                "has_video_count": True,
                "visibility": "public",
                "thumbnail_url": "https://example.test/playlist.jpg",
            }
            videos = [
                {
                    "playlist_id": "PLexample",
                    "position": 1,
                    "video_id": "abc12345678",
                    "title": "Video",
                    "channel_id": "",
                    "channel": "",
                    "duration_text": "1:00",
                    "is_playable": 1,
                    "availability": "LIVE",
                    "url": "https://www.youtube.com/watch?v=abc12345678",
                }
            ]
            opener = object()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=opener),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch("yt_library.workers.extract_playlist_metadata", return_value=header),
                patch("yt_library.workers.scan_playlist_ytdlp", return_value=(videos, {})),
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
                patch("yt_library.workers.cache_thumbnail", return_value="thumbs/PLexample.jpg") as cache_thumb,
                patch("yt_library.workers.enqueue_placeholder_recovery_targets", return_value={"inserted": 0}),
            ):
                worker._run(
                    "test-playlist-thumbnail",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=7,
                    record_summary=False,
                )

            scan_web.assert_not_called()
            cache_thumb.assert_called_once_with(
                opener,
                "PLexample",
                "https://example.test/playlist.jpg",
                core.DEFAULT_THUMB_DIR,
            )
            conn = core.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT thumbnail_url, thumbnail_path FROM playlists WHERE playlist_id = 'PLexample'"
                ).fetchone()
                self.assertEqual(row["thumbnail_url"], "https://example.test/playlist.jpg")
                self.assertEqual(row["thumbnail_path"], "thumbs/PLexample.jpg")
            finally:
                conn.close()

    def test_playlist_worker_uses_web_fallback_after_short_ytdlp_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            header = {"video_count": 2, "has_video_count": True, "visibility": "public"}
            ytdlp_videos = [{"video_id": "first"}]
            web_videos = [{"video_id": "first"}, {"video_id": "second"}]
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch("yt_library.workers.extract_playlist_metadata", return_value=header),
                patch("yt_library.workers.scan_playlist_ytdlp", return_value=(ytdlp_videos, {})),
                patch("yt_library.workers.youtube_session_status", return_value=(True, "")),
                patch("yt_library.workers.scan_playlist_videos", return_value=web_videos) as scan_web,
                patch("yt_library.workers.save_playlist_scan", return_value=(2, 0)),
                patch("yt_library.workers.enqueue_placeholder_recovery_targets", return_value={"inserted": 0}),
            ):
                worker._run(
                    "test-playlist-fallback",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=7,
                    record_summary=False,
                )

            scan_web.assert_called_once()
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log WHERE run_id = 'test-playlist-fallback'"
                ).fetchone()
                self.assertEqual(log["level"], "info")
                self.assertIn("2 videos", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_still_uses_ytdlp_in_throttle_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLthrottle', 'Throttle')"
                    )
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLthrottle",
                        manual=False,
                    )
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            header = {
                "video_count": 1,
                "has_video_count": True,
                "visibility": "private",
            }
            videos = [{"video_id": "private-video"}]
            core.configure_request_pacing(
                {
                    "dispatch_mode": "throttle",
                    "request_delay_min_seconds": 0,
                    "request_delay_max_seconds": 0,
                }
            )
            try:
                with (
                    patch("yt_library.workers.load_cookie_opener", return_value=object()),
                    patch("yt_library.workers.request_text", return_value="header page"),
                    patch(
                        "yt_library.workers.extract_playlist_metadata",
                        return_value=header,
                    ),
                    patch(
                        "yt_library.workers.scan_playlist_ytdlp",
                        return_value=(videos, {}),
                    ) as scan_ytdlp,
                    patch("yt_library.workers.scan_playlist_videos") as scan_web,
                    patch(
                        "yt_library.workers.save_playlist_scan",
                        return_value=(1, 0),
                    ),
                    patch(
                        "yt_library.workers.enqueue_placeholder_recovery_targets",
                        return_value={"inserted": 0},
                    ),
                ):
                    worker._run(
                        "test-playlist-throttle-ytdlp",
                        db_path,
                        Path(temp_dir) / "cookies.txt",
                        delay=0,
                        limit=1,
                        force=False,
                        stale_days=7,
                        record_summary=False,
                    )
            finally:
                core.configure_request_pacing({"dispatch_mode": "delay"})

            scan_ytdlp.assert_called_once_with(
                "PLthrottle",
                Path(temp_dir) / "cookies.txt",
                "",
            )
            scan_web.assert_not_called()

    def test_playlist_worker_skips_when_header_count_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch("yt_library.workers.extract_playlist_metadata", return_value={"video_count": 0, "has_video_count": False}),
                patch("yt_library.workers.scan_playlist_ytdlp", return_value=([], {})) as scan_ytdlp,
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
            ):
                worker._run(
                    "test-playlist-no-header",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=7,
                    record_summary=False,
                )

            scan_ytdlp.assert_called_once()
            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log WHERE run_id = 'test-playlist-no-header'"
                ).fetchone()
                self.assertEqual(log["level"], "error")
                self.assertIn("playlist count unavailable", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_uses_ytdlp_count_for_authenticated_header_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            videos = [{"video_id": "first"}]
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.request_text",
                    return_value='ytcfg.set({"LOGGED_IN":true}); ServiceLogin recaptcha',
                ),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value={"video_count": 0, "has_video_count": False},
                ),
                patch(
                    "yt_library.workers.scan_playlist_ytdlp",
                    return_value=(videos, {"video_count": 1, "title": "Example"}),
                ) as scan_ytdlp,
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
                patch("yt_library.workers.save_playlist_scan", return_value=(1, 0)),
                patch("yt_library.workers.enqueue_placeholder_recovery_targets", return_value={"inserted": 0}),
            ):
                worker._run(
                    "test-playlist-authenticated-header-shell",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=7,
                    record_summary=False,
                )

            scan_ytdlp.assert_called_once()
            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log "
                    "WHERE run_id = 'test-playlist-authenticated-header-shell'"
                ).fetchone()
                self.assertEqual(log["level"], "info")
                self.assertIn("1 videos", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_marks_authenticated_missing_playlist_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, visibility, is_library_playlist
                        )
                        VALUES ('PLmissing', 'Missing', 'private', 1)
                        """
                    )
                    core.upsert_video(conn, "keptvideo01", title="Kept video", source="playlist")
                    conn.execute(
                        """
                        INSERT INTO playlist_items(playlist_id, position, video_id)
                        VALUES ('PLmissing', 1, 'keptvideo01')
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count, scan_status
                        ) VALUES ('PLmissing', '2026-07-28T00:00:00Z', 1, 0, 'ok')
                        """
                    )
                    core.enqueue_playlist_scan_item(conn, "PLmissing", manual=True)
            finally:
                conn.close()

            missing_error = (
                "[youtube:tab] ERROR - Requested entity was not found. "
                "Unable to download API page: HTTP Error 404: Not Found"
            )
            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.request_text",
                    return_value='ytcfg.set({"LOGGED_IN":true});',
                ),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value={"video_count": 0, "has_video_count": False},
                ),
                patch(
                    "yt_library.workers.scan_playlist_ytdlp",
                    side_effect=RuntimeError(missing_error),
                ),
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
            ):
                worker._run(
                    "test-playlist-missing",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=7,
                    record_summary=False,
                )

            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                playlist = conn.execute(
                    """
                    SELECT fetch_status
                    FROM playlists
                    WHERE playlist_id = 'PLmissing'
                    """
                ).fetchone()
                self.assertEqual(playlist["fetch_status"], "removed")
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM playlist_items WHERE playlist_id = 'PLmissing'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT title FROM videos WHERE video_id = 'keptvideo01'"
                    ).fetchone()[0],
                    "Kept video",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM worker_queue WHERE playlist_id = 'PLmissing'"
                    ).fetchone()[0],
                    0,
                )
                log = conn.execute(
                    """
                    SELECT level, message
                    FROM playlist_scan_worker_log
                    WHERE run_id = 'test-playlist-missing'
                    """
                ).fetchone()
                self.assertEqual(log["level"], "info")
                self.assertIn("marked removed", log["message"])
                self.assertIn("preserved 1 videos", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_accepts_valid_header_with_login_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            header = {"video_count": 1, "has_video_count": True, "visibility": "private"}
            videos = [{"video_id": "first"}]
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="ServiceLogin header page"),
                patch("yt_library.workers.extract_playlist_metadata", return_value=header),
                patch("yt_library.workers.scan_playlist_ytdlp", return_value=(videos, {})) as scan_ytdlp,
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
                patch("yt_library.workers.save_playlist_scan", return_value=(1, 0)),
                patch("yt_library.workers.enqueue_placeholder_recovery_targets", return_value={"inserted": 0}),
            ):
                worker._run(
                    "test-playlist-valid-header-with-login-marker",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=7,
                    record_summary=False,
                )

            scan_ytdlp.assert_called_once()
            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log WHERE run_id = 'test-playlist-valid-header-with-login-marker'"
                ).fetchone()
                self.assertEqual(log["level"], "info")
                self.assertIn("1 videos", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_reports_signed_out_header_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.request_text",
                    return_value=(
                        'ytcfg.set({"LOGGED_IN":false}); '
                        "<a href='https://accounts.google.com/ServiceLogin'>Sign in</a>"
                    ),
                ),
                patch("yt_library.workers.extract_playlist_metadata", return_value={"video_count": 0, "has_video_count": False}),
                patch("yt_library.workers.scan_playlist_ytdlp") as scan_ytdlp,
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
            ):
                worker._run(
                    "test-playlist-signed-out-header",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=7,
                    record_summary=False,
                )

            scan_ytdlp.assert_not_called()
            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log WHERE run_id = 'test-playlist-signed-out-header'"
                ).fetchone()
                self.assertEqual(log["level"], "error")
                self.assertIn("login session is not accepted", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_allows_foreign_playlist_short_of_reported_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLforeign', 'Foreign')")
                    core.enqueue_playlist_scan_item(conn, "PLforeign", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            header = {"video_count": 168, "has_video_count": True, "owner_channel_id": "UCother"}
            ytdlp_videos = [{"video_id": f"video{i}"} for i in range(100)]
            web_videos = [{"video_id": f"video{i}"} for i in range(167)]
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch("yt_library.workers.extract_playlist_metadata", return_value=header),
                patch("yt_library.workers.scan_playlist_ytdlp", return_value=(ytdlp_videos, {})),
                patch("yt_library.workers.youtube_session_status", return_value=(True, "")),
                patch("yt_library.workers.scan_playlist_videos", return_value=web_videos) as scan_web,
                patch("yt_library.workers.save_playlist_scan", return_value=(167, 1)) as save_scan,
                patch("yt_library.workers.enqueue_placeholder_recovery_targets", return_value={"inserted": 0}),
            ):
                worker._run(
                    "test-foreign-short",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=7,
                    record_summary=False,
                )

            scan_web.assert_called_once()
            save_scan.assert_called_once()
            saved_videos = save_scan.call_args.args[2]
            self.assertEqual(len(saved_videos), 167)
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log WHERE run_id = 'test-foreign-short'"
                ).fetchone()
                self.assertEqual(log["level"], "info")
                self.assertIn("167 exposed of 168 reported", log["message"])
            finally:
                conn.close()

    def test_placeholder_recovery_targets_use_the_common_worker_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.enqueue_worker_queue_target(conn, "PLearlierWork")
                    conn.execute("UPDATE worker_queue SET priority = 25 WHERE playlist_id = 'PLearlierWork'")
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 2,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        first = core.enqueue_placeholder_recovery_targets(
                            conn,
                            "PLexample",
                        )
                        second = core.enqueue_placeholder_recovery_targets(
                            conn,
                            "PLexample",
                        )

                self.assertEqual(first, {"inserted": 1, "existing": 0})
                self.assertEqual(second, {"inserted": 0, "existing": 1})
                row = conn.execute(
                    "SELECT worker_type, task_type, video_id, playlist_id, current_title, source_key, priority "
                    "FROM worker_queue WHERE worker_type = 'placeholder'"
                ).fetchone()
                self.assertEqual(
                    dict(row),
                    {
                        "worker_type": "placeholder",
                        "task_type": "recover",
                        "video_id": "abc12345678",
                        "playlist_id": "PLexample",
                        "current_title": "Unavailable example",
                        "source_key": "",
                        "priority": 26,
                    },
                )
            finally:
                conn.close()

    def test_worker_queue_events_capture_add_update_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abc12345678",
                        current_title="Example video",
                        metadata_source="provided",
                        priority=10,
                    )
                queue_row = conn.execute(
                    "SELECT queue_id FROM worker_queue WHERE video_id = 'abc12345678'"
                ).fetchone()
                queue_id = int(queue_row["queue_id"])
                first_cursor = core.worker_queue_event_cursor(conn)
                events = core.worker_queue_events_after(conn, 0)
                self.assertEqual([(row["queue_id"], row["operation"]) for row in events], [(queue_id, "upsert")])
                self.assertEqual(
                    [row["video_id"] for row in core.worker_queue_rows_by_id(conn, [queue_id])],
                    ["abc12345678"],
                )

                with conn:
                    conn.execute("UPDATE worker_queue SET priority = 2 WHERE queue_id = ?", (queue_id,))
                    core.remove_worker_queue_entry(conn, queue_id)
                later_events = core.worker_queue_events_after(conn, first_cursor)
                self.assertEqual(
                    [(row["queue_id"], row["operation"]) for row in later_events],
                    [(queue_id, "upsert"), (queue_id, "remove")],
                )
                self.assertEqual(core.worker_queue_rows_by_id(conn, [queue_id]), [])
            finally:
                conn.close()

    def test_worker_queue_prefers_recent_actions_within_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="olderaction1",
                        current_title="Older action",
                        metadata_source="provided",
                        priority=5,
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="neweraction1",
                        current_title="Newer action",
                        metadata_source="provided",
                        priority=5,
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="higherpriority1",
                        current_title="Higher priority",
                        metadata_source="provided",
                        priority=4,
                    )
                    conn.execute(
                        """
                        UPDATE worker_queue
                        SET updated_at = CASE video_id
                          WHEN 'olderaction1' THEN '2026-07-28T10:00:00Z'
                          WHEN 'neweraction1' THEN '2026-07-28T11:00:00Z'
                          WHEN 'higherpriority1' THEN '2026-07-28T09:00:00Z'
                        END
                        """
                    )

                rows = core.worker_queue_rows(conn)
                self.assertEqual(
                    [row["video_id"] for row in rows],
                    ["higherpriority1", "neweraction1", "olderaction1"],
                )
                next_row = WorkerQueueDispatcher()._next_row(db_path)
                self.assertEqual(next_row["video_id"], "higherpriority1")
                higher_priority_id = int(next_row["queue_id"])
                next_same_priority = WorkerQueueDispatcher()._next_row(
                    db_path,
                    excluded_queue_ids={higher_priority_id},
                )
                self.assertEqual(next_same_priority["video_id"], "neweraction1")

                with conn:
                    conn.execute(
                        """
                        UPDATE worker_queue
                        SET updated_at = '2026-07-28T12:00:00Z'
                        WHERE video_id = 'olderaction1'
                        """
                    )
                same_priority = core.metadata_queue_rows(conn)
                self.assertEqual(
                    [row["video_id"] for row in same_priority],
                    ["higherpriority1", "olderaction1", "neweraction1"],
                )
                next_refreshed = WorkerQueueDispatcher()._next_row(
                    db_path,
                    excluded_queue_ids={higher_priority_id},
                )
                self.assertEqual(next_refreshed["video_id"], "olderaction1")
            finally:
                conn.close()

    def test_worker_log_cursors_snapshot_and_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "abc12345678",
                        title="Example video",
                        source="test",
                    )
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example playlist')"
                    )
                    conn.execute(
                        "INSERT INTO metadata_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:00Z', 'video', 'abc12345678', 'first')"
                    )
                    conn.execute(
                        "INSERT INTO playlist_scan_worker_log(run_id, created_at, level, playlist_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:01Z', 'info', 'PLexample', 'playlist')"
                    )
                    conn.execute(
                        "INSERT INTO placeholder_recovery_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-2', '2026-07-13T12:00:02Z', 'found', 'placeholder1', 'recovered')"
                    )

                cursors = core.worker_log_cursors(conn)
                snapshot = core.worker_log_snapshot(conn)
                self.assertEqual([row["message"] for row in snapshot["metadataLogs"]], ["first"])
                self.assertEqual(snapshot["metadataLogs"][0]["subject_title"], "Example video")
                self.assertEqual(snapshot["metadataLogs"][0]["display_id"], "abc12345678")
                self.assertEqual([row["message"] for row in snapshot["playlistScanLogs"]], ["playlist"])
                self.assertEqual(
                    snapshot["playlistScanLogs"][0]["subject_title"],
                    "Example playlist",
                )
                self.assertEqual(snapshot["playlistScanLogs"][0]["display_id"], "PLexample")
                self.assertEqual(snapshot["liveHistoryLogs"], [])
                self.assertEqual(
                    [row["message"] for row in snapshot["placeholderRecoveryLogs"]],
                    ["recovered"],
                )

                with conn:
                    core.upsert_video(
                        conn,
                        "def12345678",
                        title="Second video",
                        source="test",
                    )
                    core.upsert_video(
                        conn,
                        "ghi12345678",
                        title="History video",
                        source="test",
                    )
                    conn.execute(
                        "INSERT INTO metadata_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:02Z', 'video', 'def12345678', 'second')"
                    )
                    conn.execute(
                        "INSERT INTO live_history_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:03Z', 'info', 'ghi12345678', 'history')"
                    )

                deltas = core.worker_logs_after(conn, cursors)
                self.assertEqual([row["message"] for row in deltas["metadataLogs"]], ["second"])
                self.assertEqual(deltas["metadataLogs"][0]["subject_title"], "Second video")
                self.assertEqual(deltas["metadataLogs"][0]["display_id"], "def12345678")
                self.assertEqual(deltas["playlistScanLogs"], [])
                self.assertEqual([row["message"] for row in deltas["liveHistoryLogs"]], ["history"])
                self.assertEqual(deltas["liveHistoryLogs"][0]["subject_title"], "History video")
                self.assertEqual(deltas["liveHistoryLogs"][0]["display_id"], "ghi12345678")
                self.assertEqual(deltas["placeholderRecoveryLogs"], [])
            finally:
                conn.close()

    def test_stopped_placeholder_recovery_keeps_its_queue_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()

            def stop_during_recovery(*args, **kwargs):
                worker._stop.set()
                return None, "", "", "stopped", "Stop requested"

            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.recover_archivarix_video", side_effect=stop_during_recovery),
            ):
                worker._run(
                    "test-placeholder-stopped",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                run = conn.execute(
                    "SELECT status, video_id, message FROM placeholder_recovery_worker_runs WHERE run_id = ?",
                    ("test-placeholder-stopped",),
                ).fetchone()
                self.assertEqual(tuple(run), ("stopped", "abc12345678", "Stop requested"))
                logs = conn.execute(
                    "SELECT run_id, level, message FROM placeholder_recovery_worker_log WHERE run_id = ? ORDER BY id",
                    ("test-placeholder-stopped",),
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in logs],
                    [
                        ("test-placeholder-stopped", "warn", "Stop requested"),
                    ],
                )
            finally:
                conn.close()

    def test_rate_limited_placeholder_recovery_keeps_queue_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(None, "", "", "rate_limited", "Archivarix daily search limit reached"),
                ),
            ):
                worker._run(
                    "test-placeholder-rate-limited",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                self.assertEqual(worker.blocked_reason(), "Archivarix daily search limit reached")
                block = core.external_service_block(conn, "archivarix")
                self.assertTrue(block["blocked"])
                self.assertEqual(block["reason_code"], "rate_limited")
                self.assertEqual(block["run_id"], "test-placeholder-rate-limited")
                self.assertTrue(block["retry_eligible"])
                run = conn.execute(
                    """
                    SELECT status, processed, failed, recovery_status, video_id,
                           request_started_at, request_count, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-rate-limited",),
                ).fetchone()
                self.assertEqual(
                    tuple(run),
                    (
                        "blocked",
                        1,
                        1,
                        "rate_limited",
                        "abc12345678",
                        run["request_started_at"],
                        1,
                        "Archivarix daily search limit reached",
                    ),
                )
                self.assertTrue(run["request_started_at"])
                logs = conn.execute(
                    "SELECT run_id, level, message FROM placeholder_recovery_worker_log WHERE run_id = ? ORDER BY id",
                    ("test-placeholder-rate-limited",),
                ).fetchall()
                self.assertEqual(logs[-1]["level"], "warn")
                self.assertEqual(logs[-1]["message"], "Archivarix daily search limit reached")
                status = core.admin_status(db_path, include_logs=True, worker_queue_limit=0)
                self.assertEqual(
                    status["latestPlaceholderRecoveryRun"]["run_id"],
                    "test-placeholder-rate-limited",
                )
                self.assertEqual(
                    status["placeholderRecoveryLogs"][0]["run_id"],
                    "test-placeholder-rate-limited",
                )
                self.assertTrue(status["archivarixBlock"]["blocked"])
                self.assertEqual(status["archivarixRequestCounts"]["last_24_hours"], 1)
                self.assertEqual(status["archivarixRequestCounts"]["total"], 1)
                self.assertEqual(
                    status["archivarixRequestCounts"]["latest_at"],
                    run["request_started_at"],
                )
            finally:
                conn.close()

    def test_placeholder_timeout_retries_then_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    side_effect=[
                        (None, "", "", "timeout", "The read operation timed out"),
                        (None, "", "", "not_found", ""),
                    ],
                ) as recover,
            ):
                worker._run(
                    "test-placeholder-timeout-recovered",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    retry_attempts=3,
                    retry_backoff_seconds=0,
                )

            self.assertEqual(recover.call_count, 2)
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 0)
                run = conn.execute(
                    """
                    SELECT status, processed, failed, recovery_status, request_count, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-timeout-recovered",),
                ).fetchone()
                self.assertEqual(
                    tuple(run),
                    ("complete", 1, 0, "not_found", 2, "not found"),
                )
                logs = conn.execute(
                    """
                    SELECT level, message
                    FROM placeholder_recovery_worker_log
                    WHERE run_id = ?
                    ORDER BY id
                    """,
                    ("test-placeholder-timeout-recovered",),
                ).fetchall()
                self.assertEqual(logs[0]["level"], "warn")
                self.assertIn("attempt 1/3", logs[0]["message"])
                self.assertEqual(logs[-1]["message"], "not found")
                self.assertEqual(core.admin_status(db_path)["archivarixRequestCounts"]["total"], 2)
            finally:
                conn.close()

    def test_placeholder_timeout_exhaustion_keeps_queue_entry_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(None, "", "", "timeout", "The read operation timed out"),
                ) as recover,
            ):
                worker._run(
                    "test-placeholder-timeout-exhausted",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    retry_attempts=3,
                    retry_backoff_seconds=0,
                )

            self.assertEqual(recover.call_count, 3)
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                run = conn.execute(
                    """
                    SELECT status, processed, failed, recovery_status, request_count, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-timeout-exhausted",),
                ).fetchone()
                self.assertEqual(
                    tuple(run)[:5],
                    ("blocked", 1, 1, "timeout", 3),
                )
                self.assertIn("timed out after 3 attempts", run["message"])
                block = core.external_service_block(conn, "archivarix")
                self.assertTrue(block["blocked"])
                self.assertEqual(block["reason_code"], "timeout")
                self.assertEqual(block["queue_id"], 1)
                self.assertEqual(worker.blocked_reason(), run["message"])
                self.assertEqual(core.admin_status(db_path)["archivarixRequestCounts"]["total"], 3)
            finally:
                conn.close()

    def test_placeholder_request_error_keeps_queue_entry_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(None, "", "", "error", "connection reset"),
                ) as recover,
            ):
                worker._run(
                    "test-placeholder-request-error",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    retry_attempts=3,
                    retry_backoff_seconds=0,
                )

            recover.assert_called_once()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                run = conn.execute(
                    """
                    SELECT status, recovery_status, request_count, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-request-error",),
                ).fetchone()
                self.assertEqual(tuple(run)[:3], ("blocked", "error", 1))
                self.assertIn("queue item retained", run["message"])
                self.assertEqual(
                    core.external_service_block(conn, "archivarix")["reason_code"],
                    "request_error",
                )
            finally:
                conn.close()

    def test_placeholder_authentication_block_is_persisted_and_keeps_queue_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch(
                    "yt_library.workers.archivarix_session_status",
                    return_value=(False, "Archivarix cookie expired"),
                ),
                patch("yt_library.workers.recover_archivarix_video") as recover,
            ):
                worker._run(
                    "test-placeholder-auth-blocked",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                )

            recover.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                run = conn.execute(
                    """
                    SELECT status, processed, failed, recovery_status, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-auth-blocked",),
                ).fetchone()
                self.assertEqual(
                    tuple(run),
                    ("blocked", 0, 1, "authentication_error", "Archivarix cookie expired"),
                )
                block = core.external_service_block(conn, "archivarix")
                self.assertEqual(block["reason_code"], "authentication_error")
                self.assertEqual(block["queue_id"], 1)
                with conn:
                    self.assertTrue(core.clear_external_service_block(conn, "archivarix"))
                self.assertFalse(core.external_service_block(conn, "archivarix")["blocked"])
            finally:
                conn.close()

    def test_dispatcher_respects_persisted_archivarix_block_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO worker_queue(
                          subject_key, worker_type, video_id, current_title,
                          priority, created_at, updated_at
                        )
                        VALUES ('placeholder:abc12345678', 'placeholder', 'abc12345678',
                                'Unavailable example', 0, ?, ?)
                        """,
                        (core.utc_now(), core.utc_now()),
                    )
                    core.set_external_service_block(
                        conn,
                        "archivarix",
                        "rate_limited",
                        "Archivarix daily search limit reached",
                        run_id="prior-run",
                        queue_id=1,
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="youtubeStillRuns",
                        current_title="YouTube still runs",
                        metadata_source="history",
                        priority=1,
                    )
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            with (
                patch("yt_library.workers.PlaceholderRecoveryWorker.start") as start_placeholder,
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    return_value={
                        "video_id": "youtubeStillRuns",
                        "title": "YouTube still runs",
                        "duration_text": "1:00",
                        "yt_status": "OK",
                    },
                ),
                patch("yt_library.workers.fetch_new_channel_metadata_if_needed", return_value=({}, "", "")),
            ):
                dispatcher._run(
                    db_path,
                    Path(temp_dir) / "youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    "UTC",
                    Path(temp_dir) / "archivarix-cookies.txt",
                    Path(temp_dir) / "archivarix-thumbs",
                    0.0,
                    1,
                    0.0,
                    1,
                )

            start_placeholder.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 0)
                self.assertTrue(core.external_service_block(conn, "archivarix")["blocked"])
            finally:
                conn.close()

    def test_dispatcher_logs_queue_start_blocked_by_failed_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="proxyRestart1",
                        current_title="Proxy restart",
                        metadata_source="history",
                        priority=0,
                    )
                    core.set_external_service_block(
                        conn,
                        "proxy",
                        "proxy_unavailable",
                        "SOCKS5 proxy 127.0.0.1:1081 is unavailable",
                    )
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            with patch.object(MetadataWorker, "start") as start_metadata:
                dispatcher._run(
                    db_path,
                    Path(temp_dir) / "youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    "UTC",
                    Path(temp_dir) / "archivarix-cookies.txt",
                    Path(temp_dir) / "archivarix-thumbs",
                    15.0,
                    30.0,
                    3,
                    0.0,
                    "socks5h://127.0.0.1:1081",
                )

            start_metadata.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 1)
                queue_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    WHERE level = 'queue error'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(queue_log["level"], "queue error")
                self.assertIn("queue start blocked", queue_log["message"].lower())
                self.assertIn("proxy is unavailable", queue_log["message"])
                self.assertNotIn("still unavailable", queue_log["message"])
            finally:
                conn.close()

    def test_reconcile_worker_runs_interrupts_placeholder_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO placeholder_recovery_worker_runs(
                          run_id, status, started_at, message
                        )
                        VALUES ('orphaned-placeholder', 'running', '2026-07-14T12:00:00Z', 'Started')
                        """
                    )
            finally:
                conn.close()

            core.reconcile_worker_runs(db_path)

            conn = core.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT status, finished_at, message FROM placeholder_recovery_worker_runs WHERE run_id = ?",
                    ("orphaned-placeholder",),
                ).fetchone()
                self.assertEqual(row["status"], "interrupted")
                self.assertTrue(row["finished_at"])
                self.assertIn("interrupted by server restart", row["message"])
            finally:
                conn.close()

    def test_dispatch_metadata_error_acknowledges_queue_entry_without_summary_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abc12345678",
                        current_title="Example video",
                        metadata_source="provided",
                        priority=0,
                        manual=True,
                    )
            finally:
                conn.close()

            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    side_effect=urllib.error.URLError("offline for test"),
                ),
            ):
                worker._run(
                    "test-run",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_count(conn), 0)
                run = conn.execute(
                    "SELECT status, total, processed, failed FROM metadata_worker_runs WHERE run_id = 'test-run'"
                ).fetchone()
                self.assertEqual(dict(run), {"status": "complete", "total": 1, "processed": 1, "failed": 1})
                logs = conn.execute(
                    "SELECT level, message FROM metadata_worker_log WHERE run_id = 'test-run' ORDER BY id"
                ).fetchall()
                self.assertEqual(len(logs), 1)
                self.assertEqual(logs[0]["level"], "provided error")
                self.assertNotIn("Worker complete", logs[0]["message"])
                self.assertNotIn("Queued", logs[0]["message"])
            finally:
                conn.close()

    def test_metadata_worker_logs_watch_percentage_for_history_and_manual_videos(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "abc12345678",
                        title="History video",
                        watch_progress_percent=64,
                        source="metadata",
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abc12345678",
                        current_title="History video",
                        metadata_source="history",
                        priority=0,
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="def12345678",
                        current_title="Manual video",
                        metadata_source="provided",
                        priority=1,
                        manual=True,
                    )
            finally:
                conn.close()

            def watch_metadata(_opener, video_id, _thumb_dir, require_authenticated=False):
                del require_authenticated
                return {
                    "video_id": video_id,
                    "title": "History video" if video_id == "abc12345678" else "Manual video",
                    "duration_text": "1:00",
                    "watch_progress_percent": "0" if video_id == "abc12345678" else "87",
                    "watch_resume_seconds": "0",
                    "yt_status": "OK",
                }

            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.fetch_watch_metadata", side_effect=watch_metadata),
                patch("yt_library.workers.fetch_new_channel_metadata_if_needed", return_value=({}, "", "")),
            ):
                worker._run(
                    "test-watch-progress-log",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=2,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                logs = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    WHERE run_id = 'test-watch-progress-log'
                    ORDER BY id
                    """
                ).fetchall()
                self.assertEqual(
                    [dict(row) for row in logs],
                    [
                        {
                            "level": "history",
                            "message": (
                                "ok: History video\n"
                                "watch percentage: 0% reported by YT; 64% retained"
                            ),
                        },
                        {
                            "level": "provided",
                            "message": "ok: Manual video\nwatch percentage: 87%",
                        },
                    ],
                )
            finally:
                conn.close()

    def test_metadata_channel_uses_channel_id_in_queue_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            channel_id = "UCchannel12345678901234"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_channel(conn, channel_id, title="Queued Channel")
                    core.enqueue_metadata_item(
                        conn,
                        video_id=channel_id,
                        channel_id=channel_id,
                        channel_title=channel_id,
                        metadata_source="channel",
                        priority=0,
                        manual=True,
                    )
                queue_row = core.worker_queue_rows(conn, limit=1)[0]
                self.assertEqual(queue_row["channel_id"], channel_id)
                self.assertEqual(queue_row["known_channel_title"], "Queued Channel")
            finally:
                conn.close()

            channel_metadata = {
                "channel_id": channel_id,
                "channel": "Fetched Channel",
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "channel_description": "",
                "channel_aliases": "",
                "channel_thumbnail_url": "",
                "channel_thumbnail_path": "",
                "archivarix_channel_id": "",
                "channel_status": "",
                "channel_status_reason": "",
            }
            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.fetch_channel_metadata", return_value=channel_metadata),
            ):
                worker._run(
                    "test-channel-id-log",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    """
                    SELECT level, video_id, message
                    FROM metadata_worker_log
                    WHERE run_id = 'test-channel-id-log'
                    """
                ).fetchone()
                self.assertEqual(log["level"], "channel")
                self.assertEqual(log["video_id"], channel_id)
                self.assertEqual(log["message"], "ok: Fetched Channel")
                display_log = core.worker_log_snapshot(conn)["metadataLogs"][0]
                self.assertEqual(display_log["display_id"], channel_id)
                self.assertEqual(display_log["subject_title"], "Fetched Channel")
                with conn:
                    conn.execute(
                        """
                        INSERT INTO metadata_worker_log(
                          run_id, created_at, level, video_id, message
                        )
                        VALUES (
                          'legacy-channel-log', '2026-07-13T12:00:00Z',
                          'channel', 'Fetched Channel', 'legacy channel message'
                        )
                        """
                    )
                legacy_log = next(
                    row
                    for row in core.worker_log_snapshot(conn)["metadataLogs"]
                    if row["message"] == "legacy channel message"
                )
                self.assertEqual(legacy_log["display_id"], channel_id)
                self.assertEqual(legacy_log["subject_title"], "Fetched Channel")
            finally:
                conn.close()

    def test_metadata_worker_fetches_new_channel_metadata_discovered_from_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abc12345678",
                        current_title="Example video",
                        metadata_source="history",
                        priority=0,
                    )
            finally:
                conn.close()

            watch_metadata = {
                "video_id": "abc12345678",
                "title": "Example video",
                "description": "",
                "channel_id": "UCnewchannel12345678901",
                "channel": "New Channel",
                "channel_url": "https://www.youtube.com/channel/UCnewchannel12345678901",
                "duration_text": "",
                "view_count": "",
                "upload_date": "",
                "thumbnail_url": "",
                "thumbnail_path": "",
                "channel_thumbnail_url": "",
                "channel_thumbnail_path": "",
                "reaction": "",
                "watch_progress_percent": "0",
                "watch_resume_seconds": "0",
                "yt_status": "OK",
            }
            channel_metadata = {
                "channel_id": "UCnewchannel12345678901",
                "channel": "New Channel",
                "channel_url": "https://www.youtube.com/channel/UCnewchannel12345678901",
                "channel_description": "About the new channel",
                "channel_aliases": "",
                "channel_thumbnail_url": "https://example.test/channel.jpg",
                "channel_thumbnail_path": "video_thumbs/UCnewchannel12345678901.jpg",
                "archivarix_channel_id": "",
                "channel_status": "",
                "channel_status_reason": "",
            }

            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.fetch_watch_metadata", return_value=watch_metadata),
                patch("yt_library.core.fetch_channel_metadata", return_value=channel_metadata) as fetch_channel,
            ):
                worker._run(
                    "test-new-channel",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            fetch_channel.assert_called_once()
            conn = core.connect(db_path)
            try:
                channel = conn.execute(
                    """
                    SELECT title, description, fetch_status, fetched_at
                    FROM channels
                    WHERE channel_id = 'UCnewchannel12345678901'
                    """
                ).fetchone()
                self.assertEqual(channel["title"], "New Channel")
                self.assertEqual(channel["description"], "About the new channel")
                self.assertEqual(channel["fetch_status"], "ok")
                self.assertIsNotNone(channel["fetched_at"])
                logs = conn.execute(
                    "SELECT level, message FROM metadata_worker_log WHERE run_id = 'test-new-channel' ORDER BY id"
                ).fetchall()
                self.assertEqual([row["level"] for row in logs], ["history", "channel"])
                self.assertIn("discovered via Example video", logs[1]["message"])
            finally:
                conn.close()

    def test_metadata_worker_does_not_refetch_known_channel_discovered_from_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_channel(conn, "UCknownchannel123456789", title="Known Channel")
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abc12345678",
                        current_title="Example video",
                        metadata_source="history",
                        priority=0,
                    )
            finally:
                conn.close()

            watch_metadata = {
                "video_id": "abc12345678",
                "title": "Example video",
                "description": "",
                "channel_id": "UCknownchannel123456789",
                "channel": "Known Channel",
                "channel_url": "https://www.youtube.com/channel/UCknownchannel123456789",
                "duration_text": "",
                "view_count": "",
                "upload_date": "",
                "thumbnail_url": "",
                "thumbnail_path": "",
                "channel_thumbnail_url": "",
                "channel_thumbnail_path": "",
                "reaction": "",
                "watch_progress_percent": "0",
                "watch_resume_seconds": "0",
                "yt_status": "OK",
            }

            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.fetch_watch_metadata", return_value=watch_metadata),
                patch("yt_library.core.fetch_channel_metadata") as fetch_channel,
            ):
                worker._run(
                    "test-known-channel",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            fetch_channel.assert_not_called()
            conn = core.connect(db_path)
            try:
                channel = conn.execute(
                    """
                    SELECT title, fetch_status, fetched_at
                    FROM channels
                    WHERE channel_id = 'UCknownchannel123456789'
                    """
                ).fetchone()
                self.assertEqual(channel["title"], "Known Channel")
                self.assertEqual(channel["fetch_status"], "")
                self.assertIsNone(channel["fetched_at"])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
