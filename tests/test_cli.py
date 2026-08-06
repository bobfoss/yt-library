from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_library import cli, core
from yt_library.config import load_config

from tests.support import migrated_connection


class QueuedCliTests(unittest.TestCase):
    def test_recovery_candidate_selector_preserves_legacy_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.executemany(
                        "INSERT INTO playlists(playlist_id, title) VALUES (?, ?)",
                        (("PLlikely", "Likely"), ("PLother", "Other")),
                    )
                    core.upsert_video(
                        conn,
                        "likelyvideo1",
                        title="Likely video",
                        thumbnail_path="cached.jpg",
                        is_playable=False,
                        source="test",
                    )
                    core.upsert_video(
                        conn,
                        "othervideo01",
                        title="Other video",
                        is_playable=False,
                        source="test",
                    )
                    conn.executemany(
                        """
                        INSERT INTO playlist_items(
                          playlist_id, position, video_id, membership_state
                        ) VALUES (?, 1, ?, 'retained_unavailable')
                        """,
                        (("PLlikely", "likelyvideo1"), ("PLother", "othervideo01")),
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count, scan_status
                        ) VALUES ('PLlikely', ?, 1, 1, 'ok')
                        """,
                        (core.utc_now(),),
                    )
                    core.save_video_recovery(
                        conn,
                        "likelyvideo1",
                        None,
                        "not_found",
                        "",
                        "",
                        "",
                    )

                likely = core.placeholder_recovery_candidate_rows(
                    conn,
                    include_completed=True,
                    likely_unavailable_only=True,
                    order_by="video",
                )
                missing = core.placeholder_recovery_candidate_rows(
                    conn,
                    include_completed=True,
                    only_missing_thumbnails=True,
                    order_by="video",
                )
                exact = core.placeholder_recovery_candidate_rows(
                    conn,
                    include_completed=True,
                    video_id="othervideo01",
                    order_by="video",
                )
            finally:
                conn.close()

        self.assertEqual([row["video_id"] for row in likely], ["likelyvideo1"])
        self.assertEqual([row["video_id"] for row in missing], ["othervideo01"])
        self.assertEqual([row["video_id"] for row in exact], ["othervideo01"])

    def test_scan_hidden_enqueues_selected_playlists_with_cookie_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cookie_file = Path(temp_dir) / "youtube-cookies.txt"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.executemany(
                        "INSERT INTO playlists(playlist_id, title) VALUES (?, ?)",
                        (("PLbeta", "Beta"), ("PLalpha", "Alpha")),
                    )
            finally:
                conn.close()
            config = load_config(Path(temp_dir) / "config.json")
            args = argparse.Namespace(
                db=str(db_path),
                cookies=str(cookie_file),
                limit=1,
                config_data=config,
            )

            with patch(
                "yt_library.cli._run_queued_cli_batch",
                return_value={"completed": 1, "failed": 0},
            ) as run_batch:
                result = cli.scan_hidden_queued(args)

            conn = core.connect(db_path)
            try:
                rows = core.worker_queue_rows(conn)
            finally:
                conn.close()

        self.assertEqual(result, {"completed": 1, "failed": 0})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["playlist_id"], "PLalpha")
        self.assertEqual(rows[0]["worker_type"], "playlist")
        self.assertTrue(rows[0]["manual"])
        self.assertEqual(
            json.loads(rows[0]["payload_json"]),
            {"cookie_file": str(cookie_file)},
        )
        self.assertEqual(run_batch.call_args.args[1][0]["queue_id"], rows[0]["queue_id"])

    def test_recovery_cli_uses_shared_selector_and_persists_worker_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cookie_file = Path(temp_dir) / "archivarix-cookies.txt"
            thumb_dir = Path(temp_dir) / "archivarix-thumbs"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLone', 'One')"
                    )
                    core.upsert_video(
                        conn,
                        "missingthumb1",
                        title="Missing thumbnail",
                        is_playable=False,
                        source="test",
                    )
                    core.upsert_video(
                        conn,
                        "othermissing1",
                        title="Other missing",
                        is_playable=False,
                        source="test",
                    )
                    conn.executemany(
                        """
                        INSERT INTO playlist_items(
                          playlist_id, position, video_id, membership_state
                        ) VALUES ('PLone', ?, ?, 'retained_unavailable')
                        """,
                        ((1, "missingthumb1"), (2, "othermissing1")),
                    )
                    core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="missingthumb1",
                        payload={"automatic": True},
                    )
            finally:
                conn.close()
            config = load_config(Path(temp_dir) / "config.json")
            args = argparse.Namespace(
                db=str(db_path),
                thumbs=str(thumb_dir),
                archivarix_cookies=str(cookie_file),
                video_id="missingthumb1",
                limit=0,
                only_missing=True,
                likely_unavailable_only=False,
                no_api=True,
                delay=1.25,
                refresh_metadata=True,
                config_data=config,
            )

            with patch(
                "yt_library.cli._run_queued_cli_batch",
                return_value={"completed": 1, "failed": 0},
            ) as run_batch:
                result = cli.recover_unavailable_videos_queued(args)

            conn = core.connect(db_path)
            try:
                rows = core.worker_queue_rows(conn)
                selected = next(row for row in rows if row["video_id"] == "missingthumb1")
                payload = json.loads(selected["payload_json"])
                with conn:
                    core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="missingthumb1",
                        payload={"automatic": "replacement"},
                    )
                preserved = conn.execute(
                    "SELECT task_type, manual, payload_json FROM worker_queue WHERE queue_id = ?",
                    (selected["queue_id"],),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(result, {"completed": 1, "failed": 0})
        self.assertEqual(selected["task_type"], "thumbnail")
        self.assertTrue(selected["manual"])
        self.assertEqual(
            payload,
            {
                "cookie_file": str(cookie_file),
                "delay_seconds": 1.25,
                "no_api": True,
                "refresh_metadata": True,
                "thumbnail_dir": str(thumb_dir),
            },
        )
        self.assertEqual(preserved["task_type"], "thumbnail")
        self.assertTrue(preserved["manual"])
        self.assertEqual(json.loads(preserved["payload_json"]), payload)
        self.assertEqual(len(run_batch.call_args.args[1]), 1)
        self.assertEqual(run_batch.call_args.args[1][0]["subject_id"], "missingthumb1")


if __name__ == "__main__":
    unittest.main()
