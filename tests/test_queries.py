from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yt_library import core
from yt_library.queries import history_search_data


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
              reconciled_id, video_id, title, channel, best_watch_time, watch_date, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("old", "old123", "Old Router Video", "Net Channel", "2026-07-01T09:00:00-07:00", "2026-07-01", 1),
                ("new", "new123", "New Fiber Video", "Got Wire", "2026-07-05T09:00:00-07:00", "2026-07-05", 2),
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


if __name__ == "__main__":
    unittest.main()
