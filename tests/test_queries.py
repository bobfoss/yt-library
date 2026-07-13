from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from yt_library import core
from yt_library.queries import fetch_app_data, history_search_data


class NormalizedReadModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        core.migrate_database(self.db_path)
        self.conn = core.connect(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def add_video(self, video_id: str, title: str, channel_id: str | None = None) -> None:
        if channel_id:
            core.upsert_channel(self.conn, channel_id, title=f"Channel {channel_id}")
        core.upsert_video(
            self.conn,
            video_id,
            title=title,
            description=f"Description for {title}",
            channel_id=channel_id or "",
            source="metadata",
        )

    def test_history_search_uses_canonical_video_metadata_and_sorts_newest_first(self) -> None:
        self.add_video("old123", "Old Router Video")
        self.add_video("new123", "AT&T Fiber Without the Gateway")
        self.conn.executemany(
            """
            INSERT INTO history_events(
              event_id, video_id, watched_at, watch_date, time_precision, source_type, match_type
            ) VALUES (?, ?, ?, ?, 'exact', 'takeout', 'takeout_only')
            """,
            [
                ("old", "old123", "2026-07-01T16:00:00Z", "2026-07-01"),
                ("new", "new123", "2026-07-02T16:00:00Z", "2026-07-02"),
            ],
        )
        self.conn.commit()

        data = history_search_data(self.conn, "")
        self.assertEqual([row["video_id"] for row in data["watch"]], ["new123", "old123"])
        filtered = history_search_data(self.conn, "fiber")
        self.assertEqual([row["video_id"] for row in filtered["watch"]], ["new123"])

    def test_history_search_preserves_date_only_without_fabricating_time(self) -> None:
        self.add_video("date123", "Date Only")
        self.conn.execute(
            """
            INSERT INTO history_events(
              event_id, video_id, watch_date, time_precision, source_type, match_type, youtube_ordinal
            ) VALUES ('youtube:1', 'date123', '2026-07-04', 'date_only', 'youtube', 'youtube_only', 1)
            """
        )
        row = history_search_data(self.conn, "", limit=1)["watch"][0]
        self.assertIsNone(row["watched_at"])
        self.assertEqual(row["watch_date"], "2026-07-04")
        self.assertEqual(row["time_quality"], "date_only")
        self.assertEqual(row["source_label"], "YouTube")
        self.assertEqual(row["match_label"], "YouTube only")
        self.assertEqual(row["history_badges"], ["date only"])

    def test_history_badges_hide_source_and_match_labels(self) -> None:
        self.add_video("takeout123", "Takeout Only")
        self.conn.execute(
            """
            INSERT INTO history_events(
              event_id, video_id, watched_at, watch_date, time_precision, source_type, match_type
            ) VALUES ('takeout:1', 'takeout123', '2026-07-04T05:27:45Z', '2026-07-04', 'exact', 'takeout', 'takeout_only')
            """
        )

        row = history_search_data(self.conn, "", limit=1)["watch"][0]

        self.assertEqual(row["source_label"], "Takeout")
        self.assertEqual(row["match_label"], "Takeout only")
        self.assertEqual(row["history_badges"], ["exact time"])

        self.add_video("matched123", "Matched")
        self.conn.execute(
            """
            INSERT INTO history_events(
              event_id, video_id, watched_at, watch_date, time_precision, source_type, match_type
            ) VALUES ('matched:1', 'matched123', '2026-07-05T05:27:45Z', '2026-07-05', 'exact', 'takeout_youtube', 'video_id_date')
            """
        )

        matched = history_search_data(self.conn, "Matched", limit=1)["watch"][0]

        self.assertEqual(matched["source_label"], "Takeout + YouTube")
        self.assertEqual(matched["match_label"], "matched by video/date")
        self.assertEqual(matched["history_badges"], ["exact time"])

    def test_history_search_filters_by_canonical_channel(self) -> None:
        self.add_video("history123", "History Channel Video", "UC_history")
        self.conn.execute(
            """
            INSERT INTO history_events(event_id, video_id, watch_date, time_precision)
            VALUES ('history-channel', 'history123', '2026-07-01', 'date_only')
            """
        )
        rows = history_search_data(self.conn, "", channel_id="UC_history")["watch"]
        self.assertEqual([row["video_id"] for row in rows], ["history123"])

    def test_playlist_items_share_one_video_and_include_all_playlist_links(self) -> None:
        self.add_video("same123", "Same Video")
        self.conn.executemany(
            "INSERT INTO playlists(playlist_id, title) VALUES (?, ?)",
            [("pl1", "First Playlist"), ("pl2", "Second Playlist")],
        )
        self.conn.executemany(
            """
            INSERT INTO playlist_items(
              playlist_id, position, video_id, membership_state, source_quality
            ) VALUES (?, 1, 'same123', ?, ?)
            """,
            [
                ("pl1", "current", "youtube"),
                ("pl2", "retained_unavailable", "takeout"),
            ],
        )
        self.conn.commit()

        rows = [row for row in fetch_app_data(self.conn)["playlistVideos"] if row["video_id"] == "same123"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[0]["playlist_links"],
            [
                {"playlist_id": "pl1", "title": "First Playlist", "removed": False},
                {"playlist_id": "pl2", "title": "Second Playlist", "removed": True},
            ],
        )

    def test_fetch_app_data_includes_standalone_history_video_metadata(self) -> None:
        self.add_video("historyonly1", "History Only Video", "UC_history")
        self.conn.execute(
            """
            INSERT INTO history_events(
              event_id, video_id, watched_at, watch_date, time_precision, source_type, match_type
            ) VALUES ('historyonly-event', 'historyonly1', '2026-07-02T16:00:00Z', '2026-07-02', 'exact', 'takeout', 'takeout_only')
            """
        )
        self.conn.commit()

        data = fetch_app_data(self.conn)
        standalone = data["standaloneVideos"]

        self.assertEqual([row["video_id"] for row in standalone], ["historyonly1"])
        self.assertEqual(standalone[0]["metadata_title"], "History Only Video")
        self.assertEqual(standalone[0]["watch_count"], 1)
        self.assertEqual(standalone[0]["playlist_links"], [])
        self.assertEqual(data["playlistVideos"], [])

    def test_fetch_app_data_marks_dominant_owner_and_generates_urls(self) -> None:
        core.upsert_channel(self.conn, "UC_owner", title="Library Owner")
        self.conn.executemany(
            "INSERT INTO playlists(playlist_id, title, owner_channel_id) VALUES (?, ?, 'UC_owner')",
            [(f"pl{i}", f"Playlist {i}") for i in range(6)],
        )
        self.conn.commit()
        playlists = fetch_app_data(self.conn)["playlists"]
        self.assertTrue(all(row["is_library_owner"] for row in playlists))
        self.assertTrue(all(row["url"].startswith("https://www.youtube.com/playlist?list=") for row in playlists))

    def test_foreign_key_check_is_clean(self) -> None:
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
