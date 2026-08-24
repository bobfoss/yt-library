from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

from yt_library import core, server
from yt_library.config import load_config
from yt_library.cookie_files import (
    COOKIE_CONFIG,
    CookieFileError,
    cookie_file_status,
    replace_cookie_file,
)


def netscape_cookie(domain: str, expires: int = 4102444800) -> bytes:
    return (
        "# Netscape HTTP Cookie File\n"
        f".{domain}\tTRUE\t/\tTRUE\t{expires}\tSID\tsecret-value\n"
    ).encode("utf-8")


class CookieFileTests(unittest.TestCase):
    def test_replace_validates_domain_and_never_returns_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "google-cookies.txt"
            status = replace_cookie_file(path, netscape_cookie("google.com"), ("google.com",))

            self.assertTrue(status["valid"])
            self.assertEqual(status["matchingCookieCount"], 1)
            self.assertNotIn("secret-value", repr(status))
            self.assertTrue(cookie_file_status(path, ("google.com",))["exists"])

    def test_invalid_replacement_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "youtube-cookies.txt"
            original = netscape_cookie("youtube.com")
            path.write_bytes(original)

            with self.assertRaises(CookieFileError):
                replace_cookie_file(path, netscape_cookie("example.com"), ("youtube.com",))

            self.assertEqual(path.read_bytes(), original)

    def test_wrong_service_replacement_identifies_export_and_preserves_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "my-activity-cookies.txt"
            original = netscape_cookie("myactivity.google.com")
            path.write_bytes(original)

            with self.assertRaisesRegex(
                CookieFileError,
                "appears to belong to YouTube, not Google My Activity",
            ):
                replace_cookie_file(
                    path,
                    netscape_cookie("youtube.com"),
                    COOKIE_CONFIG["google"].expected_domains,
                    expected_kind="google",
                )

            self.assertEqual(path.read_bytes(), original)

    def test_unrecognized_service_replacement_reports_expected_cookie_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "archivarix-cookies.txt"

            with self.assertRaisesRegex(
                CookieFileError,
                "Expected at least one cookie scoped to archivarix.net",
            ):
                replace_cookie_file(
                    path,
                    netscape_cookie("example.com"),
                    COOKIE_CONFIG["archivarix"].expected_domains,
                    expected_kind="archivarix",
                )

            self.assertFalse(path.exists())

    def test_admin_route_accepts_valid_text_without_echoing_cookie_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "config.json")
            db_path = Path(temp_dir) / "library.sqlite3"
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    core.record_cookie_auth_status(
                        conn,
                        "google",
                        "valid",
                        "Authenticated request accepted.",
                    )
            finally:
                conn.close()
            content = netscape_cookie("myactivity.google.com")
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/cookies/google"
            handler.db_path = db_path
            handler.config_data = config
            handler.headers = {
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(content)),
                "X-YT-Library-Admin": "1",
                "Host": "127.0.0.1:8765",
                "Origin": "http://127.0.0.1:8765",
            }
            handler.rfile = BytesIO(content)
            handler.send_json = Mock()

            handler.do_POST()

            payload = handler.send_json.call_args.args[0]
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["status"]["valid"])
            self.assertNotIn("secret-value", repr(payload))
            self.assertTrue((Path(temp_dir) / "my_activity_cookies.txt").is_file())
            conn = core.connect(db_path)
            try:
                self.assertEqual(
                    core.cookie_auth_statuses(conn)["google"]["status"],
                    "unknown",
                )
            finally:
                conn.close()

    def test_admin_route_rejects_cookie_export_for_another_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "config.json")
            db_path = Path(temp_dir) / "library.sqlite3"
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    core.record_cookie_auth_status(
                        conn,
                        "google",
                        "valid",
                        "Authenticated request accepted.",
                    )
            finally:
                conn.close()
            target = Path(temp_dir) / "my_activity_cookies.txt"
            original = netscape_cookie("myactivity.google.com")
            target.write_bytes(original)
            content = netscape_cookie("youtube.com")
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/cookies/google"
            handler.db_path = db_path
            handler.config_data = config
            handler.headers = {
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(content)),
                "X-YT-Library-Admin": "1",
                "Host": "127.0.0.1:8765",
                "Origin": "http://127.0.0.1:8765",
            }
            handler.rfile = BytesIO(content)
            handler.send_json = Mock()

            handler.do_POST()

            payload = handler.send_json.call_args.args[0]
            self.assertIn(
                "appears to belong to YouTube, not Google My Activity",
                payload["error"],
            )
            self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)
            self.assertEqual(target.read_bytes(), original)
            conn = core.connect(db_path)
            try:
                self.assertEqual(
                    core.cookie_auth_statuses(conn)["google"]["status"],
                    "valid",
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
