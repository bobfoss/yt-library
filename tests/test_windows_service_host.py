from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import service_runtime, windows_service_host


class WindowsServiceHostTests(unittest.TestCase):
    def test_child_command_uses_project_venv_and_absolute_manager_path(self) -> None:
        root = Path(r"C:\repo\YT Library")
        config = windows_service_host.HostConfiguration(
            repo_root=root,
            service_name="YTLibraryManager-TEST",
        )

        self.assertEqual(
            config.child_command,
            (
                str(root / ".venv" / "Scripts" / "python.exe"),
                str(root / "yt_library_manager.py"),
            ),
        )

    def test_service_base_url_uses_loopback_for_wildcard_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "yt_library.config.json").write_text(
                json.dumps({"host": "0.0.0.0", "port": 9876}),
                encoding="utf-8",
            )

            self.assertEqual(
                windows_service_host.service_base_url(root),
                "http://127.0.0.1:9876",
            )

    def test_service_base_url_brackets_ipv6_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "yt_library.config.json").write_text(
                json.dumps({"host": "2001:db8::1", "port": 8765}),
                encoding="utf-8",
            )

            self.assertEqual(
                windows_service_host.service_base_url(root),
                "http://[2001:db8::1]:8765",
            )

    def test_queue_monitor_preserves_resume_intent_while_proxy_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            host = windows_service_host.WindowsServiceHost(
                windows_service_host.HostConfiguration(
                    repo_root=root,
                    service_name="YTLibraryManager-TEST",
                )
            )
            service_runtime.write_queue_intent(
                host.config.log_directory,
                True,
                source="unit-test",
            )
            with patch.object(
                windows_service_host,
                "_request_json",
                return_value={
                    "workerQueueRunning": False,
                    "workerQueueStopping": False,
                    "proxyBlock": {"blocked": True},
                },
            ):
                host._remember_runtime_state()

            self.assertTrue(
                service_runtime.queue_intent(host.config.log_directory)[
                    "queueShouldRun"
                ]
            )


if __name__ == "__main__":
    unittest.main()
