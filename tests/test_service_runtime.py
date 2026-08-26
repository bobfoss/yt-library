from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import service_runtime


class ServiceRuntimeTests(unittest.TestCase):
    def test_prepare_run_archives_previous_streams_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_directory = Path(temp_dir)
            first = service_runtime.prepare_run(log_directory, mode="direct")
            (log_directory / "service.stdout.log").write_text("first stdout", encoding="utf-8")
            (log_directory / "service.stderr.log").write_text("first stderr", encoding="utf-8")
            service_runtime.update_manifest(
                log_directory,
                servicePid=123,
                stoppedAt=service_runtime.utc_now(),
                stopReason="test-stop",
            )

            second = service_runtime.prepare_run(
                log_directory,
                mode="windows-service",
                host_pid=456,
                archive_reason="test-next-run",
            )

            archives = list((log_directory / "archive").iterdir())
            self.assertEqual(len(archives), 1)
            archived = archives[0]
            self.assertEqual(
                (archived / "service.stdout.log").read_text(encoding="utf-8"),
                "first stdout",
            )
            self.assertEqual(
                (archived / "service.stderr.log").read_text(encoding="utf-8"),
                "first stderr",
            )
            archived_manifest = json.loads(
                (archived / "service-run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(archived_manifest["runId"], first["runId"])
            self.assertEqual(archived_manifest["archiveReason"], "test-next-run")
            self.assertEqual(archived_manifest["stopReason"], "test-stop")
            self.assertEqual(second["mode"], "windows-service")
            self.assertEqual(second["hostPid"], 456)
            self.assertNotEqual(second["runId"], first["runId"])
            self.assertFalse((log_directory / "service.stdout.log").exists())

    def test_pruning_keeps_newest_run_even_when_it_exceeds_byte_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_directory = Path(temp_dir) / "archive"
            for name, size in (("20260101Z-old", 2), ("20260102Z-new", 8)):
                path = archive_directory / name
                path.mkdir(parents=True)
                (path / "service.stdout.log").write_bytes(b"x" * size)

            removed = service_runtime.prune_archives(
                Path(temp_dir),
                keep_runs=20,
                keep_bytes=1,
            )

            self.assertEqual(removed, ("20260101Z-old",))
            self.assertTrue((archive_directory / "20260102Z-new").is_dir())

    def test_queue_intent_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_directory = Path(temp_dir)
            written = service_runtime.write_queue_intent(
                log_directory,
                True,
                source="unit-test",
            )

            self.assertTrue(written["queueShouldRun"])
            self.assertEqual(written["source"], "unit-test")
            self.assertEqual(service_runtime.queue_intent(log_directory), written)


if __name__ == "__main__":
    unittest.main()
