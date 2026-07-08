from __future__ import annotations

import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from yt_library import core


class CoreHelperTests(unittest.TestCase):
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
            "2026-07-04T05:27:45-07:00",
        )
        self.assertEqual(
            core.takeout_watch_datetime("2026-07-04T05:27:45.123Z"),
            "2026-07-04T05:27:45+00:00",
        )
        self.assertEqual(core.youtube_watch_datetime("2026-01-15"), "2026-01-15T00:00:00-08:00")
        self.assertEqual(core.youtube_watch_datetime("2026-07-15"), "2026-07-15T00:00:00-07:00")

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
        self.assertEqual(core.format_duration(65), "1:05")
        self.assertEqual(core.format_duration(3661), "1:01:01")
        self.assertEqual(core.bounded_int("140"), 100)
        self.assertEqual(core.bounded_int("-5"), 0)

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
            core.playlist_match_type_from_legacy(
                "current",
                "current hidden slot has no exposed video ID",
            ),
            "ambiguous_hidden_slot",
        )
        self.assertEqual(
            core.reconciled_video_availability("Ax8Yn8DPZe0", "", "LIVE"),
            "LIVE",
        )
        self.assertEqual(core.reconciled_video_availability("Ax8Yn8DPZe0", "live", ""), "LIVE")
        self.assertEqual(core.reconciled_video_availability("Ax8Yn8DPZe0", "", "", 1), "public")
        self.assertEqual(core.reconciled_video_availability("Ax8Yn8DPZe0", "subscriber_only", "", 0), "subscriber_only")
        self.assertEqual(core.reconciled_video_availability("", "private", "LIVE"), "")

    def test_history_reconciliation_helpers_split_legacy_source_quality(self) -> None:
        self.assertEqual(core.history_source_type_from_legacy("matched"), "takeout_youtube")
        self.assertEqual(core.history_match_type_from_legacy("matched"), "video_id_date")
        self.assertEqual(core.history_time_quality_from_legacy("matched"), "exact")
        self.assertEqual(core.history_source_type_from_legacy("youtube_observed_only"), "youtube")
        self.assertEqual(
            core.history_match_type_from_legacy("youtube_observed_only", "observed_only"),
            "youtube_only",
        )
        self.assertEqual(core.history_time_quality_from_legacy("youtube_observed_only"), "observed_only")
        self.assertEqual(core.history_time_quality_label("observed_only"), "observed time")
        self.assertIn("observed_at", core.history_time_quality_note("observed_only"))


class SchemaTests(unittest.TestCase):
    def test_connect_bootstraps_expected_tables(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                db_path = Path(temp_dir) / "library.sqlite3"
                conn = core.connect(db_path)
                try:
                    tables = {
                        row["name"]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    columns = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(video_metadata)")
                    }
                    reconciled_columns = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(playlist_video_reconciled)")
                    }
                    history_columns = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(history_reconciled)")
                    }
                    snapshot_playlist_columns = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(snapshot_playlists)")
                    }
                    snapshot_video_columns = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(snapshot_videos)")
                    }
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

        self.assertIn("playlists", tables)
        self.assertIn("channels", tables)
        self.assertIn("history_reconciled", tables)
        self.assertIn("metadata_queue", tables)
        self.assertIn("worker_queue", tables)
        self.assertIn("metadata_worker_runs", tables)
        self.assertIn("reaction", columns)
        self.assertIn("match_type", reconciled_columns)
        self.assertNotIn("match_notes", reconciled_columns)
        self.assertIn("source_type", history_columns)
        self.assertIn("match_type", history_columns)
        self.assertIn("time_quality", history_columns)
        self.assertNotIn("source_quality", history_columns)
        self.assertNotIn("match_confidence", history_columns)
        self.assertNotIn("match_notes", history_columns)
        self.assertNotIn("source_file", snapshot_playlist_columns)
        self.assertNotIn("source_file", snapshot_video_columns)

    def test_recent_channel_fetch_without_thumbnail_ages_out_of_metadata_queue(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                conn = core.connect(Path(temp_dir) / "library.sqlite3")
                try:
                    now = int(time.time())
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
                        queued = core.enqueue_provided_metadata_target(conn, "https://www.youtube.com/@ESSIGI")
                    self.assertEqual(queued["channel_id"], "@ESSIGI")
                    self.assertEqual(queued["metadata_source"], "channel")

                    with conn:
                        conn.execute(
                            """
                            INSERT INTO playlists(playlist_id, title)
                            VALUES ('PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4', 'Test playlist')
                            """
                        )
                        conn.execute(
                            """
                            INSERT INTO playlist_videos(playlist_id, position, video_id, title)
                            VALUES
                              ('PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4', 1, 'abc12345678', 'First'),
                              ('PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4', 2, 'def12345678', 'Second')
                            """
                        )
                        queued_playlist = core.enqueue_provided_metadata_target(
                            conn,
                            "https://www.youtube.com/playlist?list=PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4",
                        )
                    self.assertEqual(queued_playlist["metadata_source"], "playlist")
                    self.assertEqual(queued_playlist["queued_count"], "2")
                    with conn:
                        queued_scan = core.enqueue_playlist_scan_target_from_text(
                            conn,
                            "PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4",
                        )
                    self.assertEqual(queued_scan["worker_type"], "playlist")
                    playlist_queue_rows = core.playlist_scan_queue_rows(conn, limit=10)
                    self.assertEqual([row["playlist_id"] for row in playlist_queue_rows], ["PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4"])
                    with self.assertRaises(ValueError):
                        core.enqueue_playlist_scan_target_from_text(conn, "https://www.youtube.com/watch?v=abc12345678")
                    playlist_video_rows = [
                        row
                        for row in core.metadata_queue_rows(conn, limit=10)
                        if row["metadata_source"] == "playlist"
                    ]
                    self.assertEqual(
                        {row["video_id"] for row in playlist_video_rows},
                        {"abc12345678", "def12345678"},
                    )

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

    def test_recovered_live_playlist_row_is_playable(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                conn = core.connect(Path(temp_dir) / "library.sqlite3")
                try:
                    with conn:
                        conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('pl1', 'Playlist')")
                        conn.execute("INSERT INTO snapshots(snapshot_key, label) VALUES ('snap1', 'Snapshot')")
                        conn.execute(
                            """
                            INSERT INTO snapshot_videos(snapshot_key, playlist_id, position, video_id, playlist_title)
                            VALUES ('snap1', 'pl1', 1, 'KRhofr57Na8', 'Playlist')
                            """
                        )
                        conn.execute(
                            """
                            INSERT INTO snapshot_video_recovery(snapshot_key, video_id, title, status, search_status)
                            VALUES ('snap1', 'KRhofr57Na8', 'Can You Safely Drink Your Own Pee?', 'LIVE', 'found')
                            """
                        )
                        core.rebuild_playlist_reconciliation(conn, "pl1")

                    row = conn.execute(
                        """
                        SELECT is_playable, availability, source_quality, match_type
                        FROM playlist_video_reconciled
                        WHERE playlist_id = 'pl1' AND video_id = 'KRhofr57Na8'
                        """
                    ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["is_playable"], 1)
                    self.assertEqual(row["availability"], "LIVE")
                    self.assertEqual(row["source_quality"], "takeout")
                    self.assertEqual(row["match_type"], "ambiguous_hidden_candidate")
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root


if __name__ == "__main__":
    unittest.main()
