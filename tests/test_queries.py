from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from yt_library import core
from yt_library.queries import (
    channel_list_data,
    fetch_app_data,
    history_activity_data,
    history_search_data,
    library_bootstrap_data,
    omni_search_data,
    playlist_list_data,
    video_collection_data,
    video_detail_data,
)


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

    def test_omni_search_deduplicates_sources_counts_and_pages_globally(self) -> None:
        self.add_video("shared123", "Needle Shared Video", "UC_needle")
        self.add_video("history123", "Needle History Video")
        self.conn.execute(
            "UPDATE channels SET title = 'Needle Channel', subscribed = 1 WHERE channel_id = 'UC_needle'"
        )
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title, owner_channel_id) VALUES ('PLneedle', 'Needle Playlist', 'UC_needle')"
        )
        self.conn.execute(
            """
            INSERT INTO playlist_items(playlist_id, position, video_id, membership_state)
            VALUES ('PLneedle', 1, 'shared123', 'current')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO history_events(event_id, video_id, watch_date, time_precision)
            VALUES (?, ?, ?, 'date_only')
            """,
            [
                ("shared-history", "shared123", "2026-07-01"),
                ("history-only", "history123", "2026-07-02"),
            ],
        )
        self.conn.commit()

        data = omni_search_data(self.conn, "needle", sort="type", limit=20)

        self.assertEqual(data["counts"], {"videos": 2, "channels": 1, "playlists": 1})
        self.assertEqual(data["total"], 4)
        self.assertEqual([result["kind"] for result in data["results"]], ["video", "video", "channel", "playlist"])
        video_ids = [result["item"]["video_id"] for result in data["results"] if result["kind"] == "video"]
        self.assertEqual(sorted(video_ids), ["history123", "shared123"])
        shared = next(result["item"] for result in data["results"] if result["item"].get("video_id") == "shared123")
        self.assertEqual(shared["watch_count"], 1)
        self.assertEqual(shared["playlist_links"][0]["playlist_id"], "PLneedle")

        page = omni_search_data(self.conn, "needle", sort="type", limit=2, offset=2)
        self.assertEqual(page["total"], 4)
        self.assertEqual(page["offset"], 2)
        self.assertEqual([result["kind"] for result in page["results"]], ["channel", "playlist"])

    def test_omni_search_applies_source_field_subscription_and_availability_filters(self) -> None:
        self.add_video("description1", "Ordinary title", "UC_subscribed")
        self.add_video("unavailable1", "Needle unavailable")
        self.conn.execute(
            "UPDATE videos SET description = 'Needle in description' WHERE video_id = 'description1'"
        )
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'private' WHERE video_id = 'unavailable1'"
        )
        self.conn.execute(
            "UPDATE channels SET title = 'Needle subscribed', subscribed = 1 WHERE channel_id = 'UC_subscribed'"
        )
        self.conn.execute("INSERT INTO channels(channel_id, title, subscribed) VALUES ('UC_other', 'Needle other', 0)")
        self.conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLfilters', 'Filter playlist')")
        self.conn.executemany(
            """
            INSERT INTO playlist_items(playlist_id, position, video_id, membership_state)
            VALUES ('PLfilters', ?, ?, 'current')
            """,
            [(1, "description1"), (2, "unavailable1")],
        )
        self.conn.execute(
            """
            INSERT INTO history_events(event_id, video_id, watch_date, time_precision)
            VALUES ('description-history', 'description1', '2026-07-03', 'date_only')
            """
        )
        self.conn.commit()

        descriptions = omni_search_data(
            self.conn,
            "needle",
            filters={"descriptions", "history_videos"},
        )
        self.assertEqual(
            [result["item"]["video_id"] for result in descriptions["results"]],
            ["description1"],
        )

        subscribed = omni_search_data(
            self.conn,
            "needle",
            filters={"videos", "channels_subscribed"},
        )
        self.assertEqual(
            [result["item"]["channel_id"] for result in subscribed["results"]],
            ["UC_subscribed"],
        )

        available_only = omni_search_data(
            self.conn,
            "needle",
            filters={"videos", "playlist_videos", "unavailable_videos"},
            include_unavailable=False,
        )
        self.assertEqual(available_only["results"], [])
        with_unavailable = omni_search_data(
            self.conn,
            "needle",
            filters={"videos", "playlist_videos", "unavailable_videos"},
            include_unavailable=True,
        )
        self.assertEqual(with_unavailable["results"][0]["item"]["video_id"], "unavailable1")

    def test_library_bootstrap_contains_counts_without_card_collections(self) -> None:
        self.add_video("liked1", "Liked", "UC_subscribed")
        self.conn.execute("UPDATE videos SET reaction = 'L' WHERE video_id = 'liked1'")
        self.conn.execute("UPDATE channels SET subscribed = 1 WHERE channel_id = 'UC_subscribed'")
        self.conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLone', 'One')")
        self.conn.execute(
            "INSERT INTO playlist_items(playlist_id, position, video_id) VALUES ('PLone', 1, 'liked1')"
        )
        self.conn.execute(
            "INSERT INTO history_events(event_id, video_id, watch_date, time_precision) VALUES ('watch1', 'liked1', '2026-07-01', 'date_only')"
        )
        self.conn.commit()

        data = library_bootstrap_data(self.conn)

        self.assertEqual(set(data), {"groups", "memberships", "counts"})
        self.assertEqual(data["counts"]["playlists"], 1)
        self.assertEqual(data["counts"]["playlist_videos"], 1)
        self.assertEqual(data["counts"]["liked_videos"], 1)
        self.assertEqual(data["counts"]["history"], 1)
        self.assertEqual(data["counts"]["subscribed_channels"], 1)

    def test_playlist_list_filters_sorts_and_pages_on_server(self) -> None:
        self.conn.executemany(
            "INSERT INTO playlists(playlist_id, title, visibility, video_count) VALUES (?, ?, ?, ?)",
            [("PLz", "Zulu", "private", 2), ("PLa", "Alpha", "public", 5)],
        )
        self.conn.executemany(
            "INSERT INTO playlist_scans(playlist_id, scanned_at, video_count, unavailable_count) VALUES (?, '2026-07-01', ?, ?)",
            [("PLz", 2, 1), ("PLa", 5, 0)],
        )
        self.conn.commit()

        data = playlist_list_data(self.conn, sort="most_videos", limit=1)

        self.assertEqual(data["total"], 2)
        self.assertEqual(data["counts"]["private"], 1)
        self.assertEqual(data["counts"]["public"], 1)
        self.assertEqual([row["playlist_id"] for row in data["results"]], ["PLa"])
        unavailable = playlist_list_data(self.conn, unavailable_only=True)
        self.assertEqual([row["playlist_id"] for row in unavailable["results"]], ["PLz"])

    def test_video_and_channel_collections_hydrate_only_requested_page(self) -> None:
        self.add_video("available1", "Alpha", "UC_subscribed")
        self.add_video("unavailable1", "Beta", "UC_other")
        self.conn.execute("UPDATE channels SET subscribed = 1 WHERE channel_id = 'UC_subscribed'")
        self.conn.execute("UPDATE videos SET is_playable = 1, reaction = 'L' WHERE video_id = 'available1'")
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'private', reaction = 'L' WHERE video_id = 'unavailable1'"
        )
        self.conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLone', 'One')")
        self.conn.executemany(
            "INSERT INTO playlist_items(playlist_id, position, video_id) VALUES ('PLone', ?, ?)",
            [(1, "available1"), (2, "unavailable1")],
        )
        self.conn.commit()

        liked = video_collection_data(self.conn, scope="liked", include_unavailable=False, limit=1)
        self.assertEqual(liked["counts"], {"videos": 1, "unavailable": 1})
        self.assertEqual([row["video_id"] for row in liked["results"]], ["available1"])
        self.assertIn("metadata_description", liked["results"][0])
        channels = channel_list_data(self.conn, categories={"subscribed"})
        self.assertEqual([row["channel_id"] for row in channels["results"]], ["UC_subscribed"])
        detail = video_detail_data(self.conn, "available1")
        self.assertEqual(detail["video_id"], "available1")

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

    def test_history_activity_counts_days_and_includes_page_offsets(self) -> None:
        self.add_video("activity123", "Activity Video", "UC_activity")
        self.add_video("otheractivity", "Other Activity", "UC_other")
        self.conn.executemany(
            """
            INSERT INTO history_events(event_id, video_id, watched_at, watch_date, time_precision)
            VALUES (?, 'activity123', ?, ?, 'exact')
            """,
            [
                ("activity-new-1", "2026-07-05T17:00:00Z", "2026-07-05"),
                ("activity-new-2", "2026-07-05T18:00:00Z", "2026-07-05"),
                ("activity-mid", "2026-07-04T17:00:00Z", "2026-07-04"),
                ("activity-old", "2026-06-30T17:00:00Z", "2026-06-30"),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO history_events(event_id, video_id, watched_at, watch_date, time_precision)
            VALUES ('activity-other', 'otheractivity', '2026-07-05T19:00:00Z', '2026-07-05', 'exact')
            """
        )

        data = history_activity_data(self.conn, start_date="2026-07-01", end_date="2026-07-05")

        self.assertEqual(
            data["activity"],
            [
                {"watch_date": "2026-07-05", "watch_count": 3, "offset": 0},
                {"watch_date": "2026-07-04", "watch_count": 1, "offset": 3},
            ],
        )
        channel_data = history_activity_data(
            self.conn,
            start_date="2026-07-01",
            end_date="2026-07-05",
            channel_id="UC_activity",
        )
        self.assertEqual(channel_data["channel_id"], "UC_activity")
        self.assertEqual(
            channel_data["activity"],
            [
                {"watch_date": "2026-07-05", "watch_count": 2, "offset": 0},
                {"watch_date": "2026-07-04", "watch_count": 1, "offset": 2},
            ],
        )

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
