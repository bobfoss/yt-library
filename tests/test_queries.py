from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yt_library import core
from yt_library.queries import fetch_app_data, history_search_data


class HistorySearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_root = core.ROOT
        core.ROOT = Path(self.temp_dir.name)
        self.conn = core.connect(Path(self.temp_dir.name) / "library.sqlite3")
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        self.conn.close()
        core.ROOT = self.original_root
        self.temp_dir.cleanup()

    def test_history_search_sorts_newest_first_and_filters_metadata(self) -> None:
        self.conn.executemany(
            """
            INSERT INTO history_reconciled(
              reconciled_id, video_id, title, channel, best_watch_time, watch_date,
              source_type, match_type, time_quality, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "old",
                    "old123",
                    "Old Router Video",
                    "Net Channel",
                    "2026-07-01T09:00:00-07:00",
                    "2026-07-01",
                    "takeout",
                    "takeout_only",
                    "exact",
                    1,
                ),
                (
                    "new",
                    "new123",
                    "New Fiber Video",
                    "Got Wire",
                    "2026-07-05T09:00:00-07:00",
                    "2026-07-05",
                    "youtube",
                    "youtube_only",
                    "date_only",
                    2,
                ),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO video_metadata(video_id, title, description, duration_text)
            VALUES (?, ?, ?, ?)
            """,
            ("new123", "AT&T Fiber Without the Gateway", "Gateway bypass and router notes", "11:34"),
        )
        self.conn.commit()

        data = history_search_data(self.conn, "", limit=10)
        self.assertEqual([row["video_id"] for row in data["watch"]], ["new123", "old123"])
        self.assertEqual(data["watch"][0]["history_badges"], ["YouTube", "date only", "YouTube only"])

        filtered = history_search_data(self.conn, "gateway", limit=10)
        self.assertEqual(filtered["totals"]["filtered_watch_rows"], 1)
        self.assertEqual(filtered["watch"][0]["video_id"], "new123")
        self.assertEqual(filtered["watch"][0]["metadata_duration"], "11:34")

    def test_history_search_clamps_limit_and_offset(self) -> None:
        self.conn.execute(
            """
            INSERT INTO history_reconciled(
              reconciled_id, video_id, title, best_watch_time, watch_date
            )
            VALUES ('one', 'one123', 'One', '2026-07-01T09:00:00-07:00', '2026-07-01')
            """
        )
        self.conn.commit()

        data = history_search_data(self.conn, "", limit=0, offset=-10)
        self.assertEqual(data["limit"], 1)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(len(data["watch"]), 1)

    def test_likely_hidden_excludes_live_recovered_snapshot_rows(self) -> None:
        self.conn.execute(
            """
            INSERT INTO playlists(playlist_id, title)
            VALUES ('pl1', 'Snapshot Playlist')
            """
        )
        self.conn.execute(
            """
            INSERT INTO snapshots(snapshot_key, label)
            VALUES ('snap1', 'Snapshot')
            """
        )
        self.conn.execute(
            """
            INSERT INTO playlist_scans(playlist_id, video_count, hidden_count, scan_status)
            VALUES ('pl1', 10, 2, 'ok')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO snapshot_videos(
              snapshot_key, playlist_id, position, video_id, playlist_title
            )
            VALUES ('snap1', 'pl1', ?, ?, 'Snapshot Playlist')
            """,
            [
                (1, "live123"),
                (2, "deleted123"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO snapshot_video_recovery(snapshot_key, video_id, title, status, search_status)
            VALUES ('snap1', ?, ?, ?, 'found')
            """,
            [
                ("live123", "Live but removed", "LIVE"),
                ("deleted123", "Deleted and hidden", "DELETED_FULL_META"),
            ],
        )
        self.conn.commit()

        data = fetch_app_data(self.conn)

        self.assertEqual(
            {row["video_id"] for row in data["snapshotMissing"]},
            {"live123", "deleted123"},
        )
        self.assertEqual(
            [row["video_id"] for row in data["snapshotLikelyHidden"]],
            ["deleted123"],
        )

    def test_playlist_videos_include_all_playlist_links_for_same_video(self) -> None:
        self.conn.executemany(
            """
            INSERT INTO playlists(playlist_id, title)
            VALUES (?, ?)
            """,
            [
                ("pl1", "First Playlist"),
                ("pl2", "Second Playlist"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO playlist_video_reconciled(
              playlist_id, display_position, video_id, title, source_quality, match_type
            )
            VALUES (?, ?, 'same123', 'Same Video', ?, ?)
            """,
            [
                ("pl1", 1, "current", ""),
                ("pl2", 2, "takeout", "ambiguous_hidden_candidate"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO history_reconciled(reconciled_id, video_id, title, best_watch_time, watch_date)
            VALUES (?, 'same123', 'Same Video', ?, ?)
            """,
            [
                ("watch1", "2026-07-01T09:00:00-07:00", "2026-07-01"),
                ("watch2", "2026-07-02T09:00:00-07:00", "2026-07-02"),
            ],
        )
        self.conn.commit()

        data = fetch_app_data(self.conn)
        rows = [row for row in data["playlistVideos"] if row["video_id"] == "same123"]

        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["watch_count"], 2)
            self.assertEqual(row["watch_dates"], ["2026-07-01", "2026-07-02"])
            self.assertEqual(
                row["playlist_links"],
                [
                    {"playlist_id": "pl1", "title": "First Playlist", "removed": False},
                    {"playlist_id": "pl2", "title": "Second Playlist", "removed": True},
                ],
            )


if __name__ == "__main__":
    unittest.main()
