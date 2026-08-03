from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yt_library import core
from yt_library.queries import (
    channel_detail_data,
    channel_list_data,
    history_activity_data,
    history_search_data,
    library_bootstrap_data,
    omni_search_data,
    playlist_detail_data,
    playlist_list_data,
    video_collection_data,
    video_detail_data,
    video_summaries_data,
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

    def test_video_summaries_batch_hydrates_known_ids_in_request_order(self) -> None:
        self.add_video("second123", "Second", "UC_second")
        self.add_video("first123", "First", "UC_first")
        self.conn.commit()

        data = video_summaries_data(
            self.conn,
            ["first123", "missing123", "second123", "first123"],
        )

        self.assertEqual(
            [video["video_id"] for video in data["videos"]],
            ["first123", "second123"],
        )
        self.assertEqual(data["videos"][0]["metadata_title"], "First")
        self.assertEqual(data["videos"][0]["metadata_channel"], "Channel UC_first")
        self.assertIn("playlist_links", data["videos"][0])

    def test_channel_aliases_drive_all_external_channel_links(self) -> None:
        channel_id = "UC_alias_owner"
        self.add_video("alias-video", "Alias Video", channel_id)
        self.conn.execute(
            "UPDATE channels SET title = 'Alias Channel', aliases = '@first_alias, @second_alias' WHERE channel_id = ?",
            (channel_id,),
        )
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title, owner_channel_id) VALUES ('PLalias', 'Alias Playlist', ?)",
            (channel_id,),
        )
        self.conn.execute(
            "INSERT INTO history_events(event_id, video_id, watch_date, time_precision) VALUES ('alias-history', 'alias-video', '2026-08-01', 'date_only')"
        )
        self.conn.commit()
        expected = "https://www.youtube.com/@first_alias"
        reference = "@first_alias"

        channel_detail = channel_detail_data(self.conn, reference)
        self.assertEqual(channel_detail["channel_id"], channel_id)
        self.assertEqual(channel_detail["preferred_reference"], reference)
        self.assertEqual(channel_detail["url"], expected)
        channel_list = channel_list_data(self.conn)["results"][0]
        self.assertEqual(channel_list["preferred_reference"], reference)
        self.assertEqual(channel_list["url"], expected)
        playlist_detail = playlist_detail_data(self.conn, "PLalias")
        self.assertEqual(playlist_detail["owner_channel_reference"], reference)
        self.assertEqual(playlist_detail["owner_channel_url"], expected)
        self.assertEqual(
            playlist_list_data(self.conn)["results"][0]["owner_channel_url"],
            expected,
        )
        self.assertEqual(
            playlist_list_data(self.conn)["results"][0]["owner_channel_reference"],
            reference,
        )
        video_detail = video_detail_data(self.conn, "alias-video")
        self.assertEqual(video_detail["metadata_channel_reference"], reference)
        self.assertEqual(video_detail["metadata_channel_url"], expected)
        history_item = history_search_data(self.conn, "alias-video")["watch"][0]
        self.assertEqual(history_item["metadata_channel_reference"], reference)
        self.assertEqual(
            history_item["metadata_channel_url"],
            expected,
        )
        results = omni_search_data(self.conn, "Alias", sort="type")["results"]
        by_kind = {result["kind"]: result["item"] for result in results}
        self.assertEqual(by_kind["channel"]["preferred_reference"], reference)
        self.assertEqual(by_kind["channel"]["url"], expected)
        self.assertEqual(by_kind["playlist"]["owner_channel_reference"], reference)
        self.assertEqual(by_kind["playlist"]["owner_channel_url"], expected)
        self.assertEqual(by_kind["video"]["metadata_channel_reference"], reference)
        self.assertEqual(by_kind["video"]["metadata_channel_url"], expected)

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
        self.assertEqual([result["kind"] for result in data["results"]], ["video", "video", "playlist", "channel"])
        video_ids = [result["item"]["video_id"] for result in data["results"] if result["kind"] == "video"]
        self.assertEqual(sorted(video_ids), ["history123", "shared123"])
        shared = next(result["item"] for result in data["results"] if result["item"].get("video_id") == "shared123")
        self.assertEqual(shared["watch_count"], 1)
        self.assertEqual(shared["playlist_links"][0]["playlist_id"], "PLneedle")

        page = omni_search_data(self.conn, "needle", sort="type", limit=2, offset=2)
        self.assertEqual(page["total"], 4)
        self.assertEqual(page["offset"], 2)
        self.assertEqual([result["kind"] for result in page["results"]], ["playlist", "channel"])

    def test_omni_search_can_limit_result_kinds(self) -> None:
        self.add_video("scoped123", "Scoped video", "UC_scoped")
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title) VALUES ('PLscoped', 'Scoped playlist')"
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "scoped",
            result_kinds={"playlist"},
            limit=20,
        )

        self.assertEqual(data["resultKinds"], ["playlist"])
        self.assertEqual(data["counts"], {"videos": 0, "playlists": 1, "channels": 0})
        self.assertEqual([result["kind"] for result in data["results"]], ["playlist"])

    def test_omni_search_playlist_group_includes_child_groups(self) -> None:
        self.conn.executemany(
            "INSERT INTO playlists(playlist_id, title) VALUES (?, ?)",
            [
                ("PLparent", "Parent playlist"),
                ("PLchild", "Child playlist"),
                ("PLother", "Other playlist"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO groups(group_key, name, parent_key, position) VALUES (?, ?, ?, ?)",
            [
                ("parent", "Parent", None, 1),
                ("child", "Child", "parent", 1),
                ("other", "Other", None, 2),
            ],
        )
        self.conn.executemany(
            "INSERT INTO group_playlists(group_key, playlist_id, position) VALUES (?, ?, ?)",
            [
                ("parent", "PLparent", 1),
                ("child", "PLchild", 1),
                ("other", "PLother", 1),
            ],
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "",
            result_kinds={"playlist"},
            playlist_group_key="parent",
            sort="title",
            limit=20,
        )

        self.assertEqual(data["playlistGroupKey"], "parent")
        self.assertEqual(data["metaCounts"]["playlists"]["total"], 2)
        self.assertEqual(
            [result["item"]["playlist_id"] for result in data["results"]],
            ["PLchild", "PLparent"],
        )

    def test_omni_search_preset_sources_scope_candidates_and_counts(self) -> None:
        self.add_video("likedsource", "Liked source", "UC_subscribed_source")
        self.add_video("membersource", "Playlist member source", "UC_regular_source")
        self.add_video("othersource", "Other source", "UC_terminated_source")
        self.conn.execute("UPDATE videos SET reaction = 'L' WHERE video_id = 'likedsource'")
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title) VALUES ('PLsource', 'Source playlist')"
        )
        self.conn.execute(
            """
            INSERT INTO playlist_items(playlist_id, position, video_id, membership_state)
            VALUES ('PLsource', 1, 'membersource', 'current')
            """
        )
        self.conn.execute(
            "UPDATE channels SET subscribed = 1 WHERE channel_id = 'UC_subscribed_source'"
        )
        self.conn.execute(
            "UPDATE channels SET status = 'terminated' WHERE channel_id = 'UC_terminated_source'"
        )
        self.conn.commit()

        liked = omni_search_data(
            self.conn,
            "",
            result_kinds={"video"},
            video_source="liked",
            limit=20,
        )
        members = omni_search_data(
            self.conn,
            "",
            result_kinds={"video"},
            video_source="playlist_member",
            limit=20,
        )
        subscribed = omni_search_data(
            self.conn,
            "",
            result_kinds={"channel"},
            channel_source="subscribed",
            limit=20,
        )
        terminated = omni_search_data(
            self.conn,
            "",
            result_kinds={"channel"},
            channel_source="terminated",
            channel_status_filters={"terminated"},
            limit=20,
        )

        self.assertEqual(liked["metaCounts"]["videos"]["total"], 1)
        self.assertEqual(liked["results"][0]["item"]["video_id"], "likedsource")
        self.assertEqual(members["metaCounts"]["videos"]["total"], 1)
        self.assertEqual(members["results"][0]["item"]["video_id"], "membersource")
        self.assertEqual(subscribed["metaCounts"]["channels"]["total"], 1)
        self.assertEqual(subscribed["results"][0]["item"]["channel_id"], "UC_subscribed_source")
        self.assertEqual(terminated["metaCounts"]["channels"]["total"], 1)
        self.assertEqual(terminated["results"][0]["item"]["channel_id"], "UC_terminated_source")

    def test_omni_search_empty_query_returns_all_results_newest_first(self) -> None:
        self.add_video("older123", "Older video", "UC_older")
        self.add_video("newer123", "Newer video", "UC_newer")
        self.conn.execute(
            "UPDATE videos SET is_playable = 1 WHERE video_id IN ('older123', 'newer123')"
        )
        self.conn.executemany(
            """
            INSERT INTO history_events(event_id, video_id, watch_date, time_precision)
            VALUES (?, ?, ?, 'date_only')
            """,
            [
                ("older-watch", "older123", "2026-07-01"),
                ("newer-watch", "newer123", "2026-07-02"),
            ],
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "",
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
            limit=20,
        )

        self.assertEqual(data["sort"], "newest")
        self.assertEqual(data["total"], 2)
        self.assertEqual(
            [result["item"]["video_id"] for result in data["results"]],
            ["newer123", "older123"],
        )

        all_data = omni_search_data(self.conn, "", limit=20)
        self.assertEqual(all_data["counts"], {"videos": 2, "playlists": 0, "channels": 2})
        self.assertEqual(all_data["total"], 4)

    def test_omni_search_newest_sorts_playlists_by_newest_member_upload_date(self) -> None:
        self.add_video("contentnew1", "Newest playlist member")
        self.add_video("contentscan1", "Older playlist member")
        self.add_video("contentnone1", "Undated playlist member")
        self.conn.executemany(
            "UPDATE videos SET upload_date = ? WHERE video_id = ?",
            [
                ("2026-07-01T00:00:00Z", "contentnew1"),
                ("2024-01-01T00:00:00Z", "contentscan1"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO playlists(playlist_id, title, updated_at)
            VALUES (?, ?, ?)
            """,
            [
                ("PLcontentnew", "Content newest", "2024-01-01T00:00:00Z"),
                ("PLscannew", "Scan newest", "2026-07-30T00:00:00Z"),
                ("PLundated", "Undated", "2026-07-31T00:00:00Z"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO playlist_items(playlist_id, position, video_id, membership_state)
            VALUES (?, 1, ?, 'current')
            """,
            [
                ("PLcontentnew", "contentnew1"),
                ("PLscannew", "contentscan1"),
                ("PLundated", "contentnone1"),
            ],
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "",
            video_meta_filters=set(),
            channel_subscription_filters=set(),
            sort="newest",
            limit=20,
        )

        self.assertEqual(
            [result["item"]["playlist_id"] for result in data["results"]],
            ["PLcontentnew", "PLscannew", "PLundated"],
        )
        self.assertEqual(
            [result["item"]["newest_video_upload_date"] for result in data["results"]],
            ["2026-07-01T00:00:00Z", "2024-01-01T00:00:00Z", ""],
        )

    def test_omni_search_keeps_missing_video_titles_blank(self) -> None:
        self.add_video("missing123", "missing123")
        self.conn.execute(
            """
            INSERT INTO history_events(event_id, video_id, watch_date, time_precision)
            VALUES ('missing-watch', 'missing123', '2026-07-02', 'date_only')
            """
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "",
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
            limit=20,
        )

        item = next(
            result["item"]
            for result in data["results"]
            if result["item"].get("video_id") == "missing123"
        )
        self.assertEqual(item["title"], "")
        self.assertEqual(item["metadata_title"], "")

    def test_omni_search_newest_ranks_unwatched_videos_last(self) -> None:
        self.add_video("watched123", "Watched video")
        self.add_video("unwatched123", "Unwatched video")
        self.conn.execute(
            "UPDATE videos SET is_playable = 1 WHERE video_id IN ('watched123', 'unwatched123')"
        )
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title) VALUES ('PLdates', 'Date sorting')"
        )
        self.conn.executemany(
            """
            INSERT INTO playlist_items(
              playlist_id, position, video_id, membership_state, added_at
            )
            VALUES ('PLdates', ?, ?, 'current', ?)
            """,
            [
                (1, "watched123", "2026-01-01T00:00:00Z"),
                (2, "unwatched123", "2026-07-29T00:00:00Z"),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO history_events(
              event_id, video_id, watched_at, watch_date, time_precision
            )
            VALUES (
              'watched-event', 'watched123', '2026-06-01T00:00:00Z',
              '2026-06-01', 'exact'
            )
            """
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "",
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
            sort="newest",
            limit=20,
        )

        self.assertEqual(
            [result["item"]["video_id"] for result in data["results"]],
            ["watched123", "unwatched123"],
        )

    def test_omni_search_newest_places_date_only_videos_before_same_day_channels(self) -> None:
        self.add_video("dateonly123", "Date-only video")
        self.conn.execute(
            "UPDATE videos SET is_playable = 1 WHERE video_id = 'dateonly123'"
        )
        self.conn.execute(
            """
            INSERT INTO history_events(
              event_id, video_id, watch_date, time_precision
            )
            VALUES ('date-only-watch', 'dateonly123', '2026-07-29', 'date_only')
            """
        )
        core.upsert_channel(
            self.conn,
            "UCsame-day",
            title="Same-day channel",
            first_seen_at="2026-07-30T01:00:00Z",
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "",
            playlist_meta_filters=set(),
            sort="newest",
            limit=20,
            display_timezone="America/Los_Angeles",
        )

        self.assertEqual(
            [
                (
                    result["kind"],
                    result["item"].get("video_id") or result["item"].get("channel_id"),
                )
                for result in data["results"]
            ],
            [
                ("video", "dateonly123"),
                ("channel", "UCsame-day"),
            ],
        )

    def test_omni_search_newest_uses_youtube_ordinal_within_a_day(self) -> None:
        self.add_video("ordinalnew1", "Zulu newest")
        self.add_video("ordinalold1", "Alphabetically first")
        self.conn.execute(
            """
            UPDATE videos
            SET is_playable = 1
            WHERE video_id IN ('ordinalnew1', 'ordinalold1')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO history_events(
              event_id, video_id, watch_date, time_precision, youtube_ordinal
            )
            VALUES (?, ?, '2026-07-30', 'date_only', ?)
            """,
            [
                ("newest-ordinal-watch", "ordinalnew1", 1),
                ("older-ordinal-watch", "ordinalold1", 2),
            ],
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "",
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
            sort="newest",
            limit=20,
            display_timezone="America/Los_Angeles",
        )

        self.assertEqual(
            [result["item"]["video_id"] for result in data["results"]],
            ["ordinalnew1", "ordinalold1"],
        )

    def test_omni_search_newest_uses_ordinal_from_latest_watch_date(self) -> None:
        self.add_video("repeatvideo", "Repeat video")
        self.add_video("othervideo1", "Other video")
        self.conn.execute(
            """
            UPDATE videos
            SET is_playable = 1
            WHERE video_id IN ('repeatvideo', 'othervideo1')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO history_events(
              event_id, video_id, watch_date, time_precision, youtube_ordinal
            )
            VALUES (?, ?, ?, 'date_only', ?)
            """,
            [
                ("repeat-old-watch", "repeatvideo", "2026-07-29", 1),
                ("repeat-new-watch", "repeatvideo", "2026-07-30", 2),
                ("other-new-watch", "othervideo1", "2026-07-30", 1),
            ],
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "",
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
            sort="newest",
            limit=20,
            display_timezone="America/Los_Angeles",
        )

        self.assertEqual(
            [result["item"]["video_id"] for result in data["results"]],
            ["othervideo1", "repeatvideo"],
        )

    def test_omni_search_uses_channel_first_seen_and_ranks_fallback_dates_last(self) -> None:
        self.add_video("datedvideo", "Dated video")
        self.conn.execute(
            """
            UPDATE videos
            SET is_playable = 1, updated_at = '2026-06-15T00:00:00Z'
            WHERE video_id = 'datedvideo'
            """
        )
        self.conn.execute(
            """
            INSERT INTO history_events(
              event_id, video_id, watched_at, watch_date, time_precision
            )
            VALUES (
              'dated-watch', 'datedvideo', '2026-06-15T00:00:00Z',
              '2026-06-15', 'exact'
            )
            """
        )
        core.upsert_channel(
            self.conn,
            "UCproper",
            title="Proper channel",
            first_seen_at="2026-06-01T00:00:00Z",
            updated_at="2026-07-29T00:00:00Z",
        )
        core.upsert_channel(
            self.conn,
            "UCfallback",
            title="Fallback channel",
            updated_at="2026-07-30T00:00:00Z",
        )
        self.conn.execute(
            """
            UPDATE channels
            SET first_seen_at = NULL,
                updated_at = '2026-07-30T00:00:00Z'
            WHERE channel_id = 'UCfallback'
            """
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "",
            playlist_meta_filters=set(),
            sort="newest",
            limit=20,
        )

        self.assertEqual(
            [
                (result["kind"], result["item"].get("channel_id") or result["item"].get("video_id"))
                for result in data["results"]
            ],
            [
                ("video", "datedvideo"),
                ("channel", "UCproper"),
                ("channel", "UCfallback"),
            ],
        )

    def test_omni_search_applies_field_and_visibility_filters(self) -> None:
        self.add_video("description1", "Ordinary title", "UC_subscribed")
        self.add_video("unavailable1", "Needle unavailable")
        self.add_video("private1", "Needle private")
        self.add_video("members1", "Needle members only")
        self.conn.execute(
            "UPDATE videos SET description = 'Needle in description', is_playable = 1 WHERE video_id = 'description1'"
        )
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'private' WHERE video_id = 'unavailable1'"
        )
        self.conn.execute(
            "UPDATE videos SET is_playable = 1, availability = 'private' WHERE video_id = 'private1'"
        )
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'subscriber_only' WHERE video_id = 'members1'"
        )
        self.conn.execute(
            "UPDATE videos SET description = '' WHERE video_id IN ('unavailable1', 'private1', 'members1')"
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
            [(1, "description1"), (2, "unavailable1"), (3, "members1")],
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
            search_fields={"descriptions"},
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
        )
        self.assertEqual(
            [result["item"]["video_id"] for result in descriptions["results"]],
            ["description1"],
        )

        subscribed = omni_search_data(
            self.conn,
            "needle",
            search_fields={"titles"},
            video_meta_filters=set(),
            channel_subscription_filters={"subscribed"},
            playlist_meta_filters=set(),
        )
        self.assertEqual(
            [result["item"]["channel_id"] for result in subscribed["results"]],
            ["UC_subscribed"],
        )
        self.conn.execute(
            "UPDATE channels SET title = 'Subscribed channel' WHERE channel_id = 'UC_subscribed'"
        )
        self.conn.commit()

        available_only = omni_search_data(
            self.conn,
            "needle",
            search_fields={"titles"},
            video_meta_filters={"public"},
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
        )
        self.assertEqual(available_only["results"], [])
        with_unavailable = omni_search_data(
            self.conn,
            "needle",
            search_fields={"titles"},
            video_meta_filters={"unavailable"},
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
        )
        self.assertEqual(with_unavailable["results"][0]["item"]["video_id"], "unavailable1")
        self.assertEqual(available_only["metaCounts"], with_unavailable["metaCounts"])
        private = omni_search_data(
            self.conn,
            "needle",
            search_fields={"titles"},
            video_meta_filters={"private"},
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
        )
        self.assertEqual(
            [result["item"]["video_id"] for result in private["results"]],
            ["private1"],
        )
        members_only = omni_search_data(
            self.conn,
            "needle",
            search_fields={"titles"},
            video_meta_filters={"members_only"},
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
        )
        self.assertEqual(
            [result["item"]["video_id"] for result in members_only["results"]],
            ["members1"],
        )

    def test_omni_search_description_only_match_ignores_title_relevance(self) -> None:
        self.add_video("both1", "Needle title")
        self.conn.execute(
            "UPDATE videos SET description = 'Needle description' WHERE video_id = 'both1'"
        )
        self.conn.commit()

        data = omni_search_data(
            self.conn,
            "needle",
            search_fields={"descriptions"},
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
        )

        self.assertEqual(data["results"][0]["item"]["video_id"], "both1")
        self.assertTrue(data["results"][0]["matchedDescription"])

    def test_omni_search_meta_filters_count_before_filtering_all_result_types(self) -> None:
        for video_id, title in (
            ("available1", "Needle available"),
            ("unavailable1", "Needle unavailable"),
            ("members1", "Needle members"),
            ("unlisted1", "Needle unlisted video"),
            ("unknown1", "Needle unknown video"),
        ):
            self.add_video(video_id, title)
        self.conn.execute(
            "UPDATE videos SET is_playable = 1, availability = 'public' WHERE video_id = 'available1'"
        )
        self.conn.execute(
            "UPDATE videos SET is_playable = 1, availability = 'unlisted' WHERE video_id = 'unlisted1'"
        )
        self.conn.execute("UPDATE videos SET reaction = 'L' WHERE video_id = 'available1'")
        self.conn.execute("UPDATE videos SET reaction = 'D' WHERE video_id = 'unavailable1'")
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'private' WHERE video_id = 'unavailable1'"
        )
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'subscriber_only' WHERE video_id = 'members1'"
        )
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title) VALUES ('PLsource', 'Source collection')"
        )
        self.conn.executemany(
            """
            INSERT INTO playlist_items(
              playlist_id, position, video_id, membership_state, source_quality, match_type
            ) VALUES ('PLsource', ?, ?, ?, ?, ?)
            """,
            [
                (1, "available1", "current", "youtube", ""),
                (2, "unavailable1", "current", "youtube", ""),
                (3, "members1", "current", "youtube", ""),
                (
                    4,
                    "unlisted1",
                    "retained_unavailable",
                    "takeout",
                    "ambiguous_hidden_candidate",
                ),
                (5, "unknown1", "current", "youtube", ""),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO channels(channel_id, title, subscribed, status)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("UC_meta_subscribed", "Needle subscribed", 1, ""),
                ("UC_meta_other", "Needle non-subscribed", 0, ""),
                ("UC_meta_terminated", "Needle terminated", 1, "terminated"),
                ("UC_library_owner", "Library owner", 0, ""),
                ("UC_playlist_other", "Other owner", 0, ""),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO playlists(
              playlist_id, title, visibility, owner_channel_id, fetch_status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("PLprivate", "Needle private", "private", None, ""),
                ("PLpublic", "Needle public", "public", None, ""),
                ("PLunlisted", "Needle unlisted", "unlisted", None, ""),
                ("PLremoved", "Needle removed playlist", "private", None, "removed"),
                ("PLother", "Needle other playlist", "unlisted", "UC_playlist_other", ""),
                *[
                    (f"PLunknown{index}", f"Needle unknown {index}", "", "UC_library_owner", "")
                    for index in range(5)
                ],
            ],
        )
        self.conn.commit()

        unfiltered = omni_search_data(self.conn, "needle", sort="type", limit=100)

        self.assertEqual(
            unfiltered["metaCounts"],
            {
                "videos": {
                    "total": 5,
                    "public": 1,
                    "unlisted": 1,
                    "private": 0,
                    "unavailable": 1,
                    "members_only": 1,
                    "unknown": 1,
                },
                "channels": {
                    "total": 3,
                    "subscribed": 2,
                    "non_subscribed": 1,
                    "active": 2,
                    "terminated": 1,
                },
                "playlists": {
                    "total": 10,
                    "private": 2,
                    "public": 1,
                    "unlisted": 2,
                    "unknown": 5,
                    "mine": 5,
                    "others": 1,
                    "ownership_unknown": 4,
                    "active": 9,
                    "removed": 1,
                },
            },
        )
        self.assertEqual(
            unfiltered["reactionCounts"],
            {
                "total": 5,
                "none": 3,
                "liked": 1,
                "disliked": 1,
            },
        )
        liked = omni_search_data(
            self.conn,
            "needle",
            video_reaction_filters={"liked"},
            sort="type",
            limit=100,
        )
        self.assertEqual(liked["reactionCounts"], unfiltered["reactionCounts"])
        self.assertEqual(
            [
                result["item"]["video_id"]
                for result in liked["results"]
                if result["kind"] == "video"
            ],
            ["available1"],
        )
        disliked = omni_search_data(
            self.conn,
            "needle",
            video_reaction_filters={"disliked"},
            sort="type",
            limit=100,
        )
        self.assertEqual(
            [
                result["item"]["video_id"]
                for result in disliked["results"]
                if result["kind"] == "video"
            ],
            ["unavailable1"],
        )
        filtered = omni_search_data(
            self.conn,
            "needle",
            video_meta_filters={"members_only"},
            channel_status_filters={"terminated"},
            playlist_status_filters={"removed"},
            sort="type",
            limit=100,
        )

        self.assertEqual(filtered["metaCounts"], unfiltered["metaCounts"])
        self.assertEqual(filtered["counts"], {"videos": 1, "channels": 1, "playlists": 1})
        self.assertEqual(filtered["total"], 3)
        self.assertEqual(
            [
                (
                    result["kind"],
                    result.get("metaCategory"),
                    result.get("playlistStatus"),
                    result.get("channelSubscription"),
                    result.get("channelStatus"),
                )
                for result in filtered["results"]
            ],
            [
                ("video", "members_only", None, None, None),
                ("playlist", "private", "removed", None, None),
                ("channel", None, None, "subscribed", "terminated"),
            ],
        )
        other_owned = omni_search_data(
            self.conn,
            "needle",
            video_meta_filters=set(),
            playlist_ownership_filters={"others"},
            channel_subscription_filters=set(),
            sort="type",
            limit=100,
        )
        self.assertEqual(
            [result["item"]["playlist_id"] for result in other_owned["results"]],
            ["PLother"],
        )
        self.assertEqual(other_owned["results"][0]["metaCategory"], "unlisted")
        self.assertEqual(other_owned["results"][0]["playlistOwnership"], "others")
        active_subscribed = omni_search_data(
            self.conn,
            "needle",
            video_meta_filters=set(),
            playlist_meta_filters=set(),
            channel_subscription_filters={"subscribed"},
            channel_status_filters={"active"},
            sort="type",
            limit=100,
        )
        self.assertEqual(
            [result["item"]["channel_id"] for result in active_subscribed["results"]],
            ["UC_meta_subscribed"],
        )
        terminated_subscribed = omni_search_data(
            self.conn,
            "needle",
            video_meta_filters=set(),
            playlist_meta_filters=set(),
            channel_subscription_filters={"subscribed"},
            channel_status_filters={"terminated"},
            sort="type",
            limit=100,
        )
        self.assertEqual(
            [result["item"]["channel_id"] for result in terminated_subscribed["results"]],
            ["UC_meta_terminated"],
        )
        self.assertEqual(
            terminated_subscribed["metaCounts"],
            unfiltered["metaCounts"],
        )
        unlisted = omni_search_data(
            self.conn,
            "needle",
            video_meta_filters={"unlisted"},
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
            sort="type",
            limit=100,
        )
        self.assertEqual(unlisted["metaCounts"], unfiltered["metaCounts"])
        self.assertEqual(
            [result["item"]["video_id"] for result in unlisted["results"]],
            ["unlisted1"],
        )

    def test_omni_search_completion_and_playlist_membership_facets(self) -> None:
        for video_id, title in (
            ("complete1", "Facet complete"),
            ("partial1", "Facet partial"),
            ("unknown1", "Facet unknown"),
            ("never1", "Facet never watched"),
        ):
            self.add_video(video_id, title)
        self.conn.executemany(
            "UPDATE videos SET is_playable = 1 WHERE video_id = ?",
            [("complete1",), ("partial1",), ("unknown1",), ("never1",)],
        )
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title) VALUES ('PLfacet', 'Facet playlist')"
        )
        self.conn.executemany(
            """
            INSERT INTO playlist_items(
              playlist_id, position, video_id, membership_state, unavailable_kind
            ) VALUES ('PLfacet', ?, ?, ?, ?)
            """,
            [
                (1, "complete1", "current", ""),
                (2, "partial1", "current", ""),
                (3, None, "unresolved_unavailable", "unavailable"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO history_events(
              event_id, video_id, watch_date, time_precision, watch_progress_percent
            ) VALUES (?, ?, '2026-07-01', 'date_only', ?)
            """,
            [
                ("complete-watch", "complete1", 100),
                ("partial-watch", "partial1", 45),
                ("unknown-watch", "unknown1", 0),
            ],
        )
        self.conn.commit()

        unfiltered = omni_search_data(self.conn, "facet", sort="type", limit=100)

        self.assertEqual(
            unfiltered["completionCounts"],
            {
                "total": 5,
                "complete": 1,
                "partial": 1,
                "partial_below_minimum": 0,
                "unknown": 2,
                "never_watched": 1,
            },
        )
        self.assertEqual(
            unfiltered["playlistMembershipCounts"],
            {
                "total": 5,
                "member": 3,
                "non_member": 2,
            },
        )

        partial_members = omni_search_data(
            self.conn,
            "facet",
            video_completion_filters={"partial"},
            video_playlist_membership_filters={"member"},
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
            sort="type",
            limit=100,
        )
        self.assertEqual(
            [result["item"]["video_id"] for result in partial_members["results"]],
            ["partial1"],
        )
        self.assertEqual(
            partial_members["completionCounts"],
            unfiltered["completionCounts"],
        )
        self.assertEqual(
            partial_members["playlistMembershipCounts"],
            unfiltered["playlistMembershipCounts"],
        )

        non_members = omni_search_data(
            self.conn,
            "facet",
            video_playlist_membership_filters={"non_member"},
            channel_subscription_filters=set(),
            playlist_meta_filters=set(),
            sort="type",
            limit=100,
        )
        self.assertEqual(
            [result["item"]["video_id"] for result in non_members["results"]],
            ["never1", "unknown1"],
        )

    def test_omni_search_filters_partial_completion_by_minimum_percentage(self) -> None:
        for video_id, title in (
            ("partial-low", "Partial low"),
            ("partial-high", "Partial high"),
        ):
            self.add_video(video_id, title)
        self.conn.executemany(
            "UPDATE videos SET is_playable = 1 WHERE video_id = ?",
            [("partial-low",), ("partial-high",)],
        )
        self.conn.executemany(
            """
            INSERT INTO history_events(
              event_id, video_id, watch_date, time_precision, watch_progress_percent
            ) VALUES (?, ?, '2026-07-01', 'date_only', ?)
            """,
            [
                ("partial-low-watch", "partial-low", 24),
                ("partial-high-watch", "partial-high", 68),
            ],
        )
        self.conn.commit()

        filtered = omni_search_data(
            self.conn,
            "partial",
            result_kinds={"video"},
            video_completion_filters={"partial"},
            video_partial_min_percent=50,
            limit=100,
        )

        self.assertEqual(
            [result["item"]["video_id"] for result in filtered["results"]],
            ["partial-high"],
        )
        self.assertEqual(filtered["completionCounts"]["total"], 2)
        self.assertEqual(filtered["completionCounts"]["partial"], 1)
        self.assertEqual(filtered["completionCounts"]["partial_below_minimum"], 1)

        below_minimum = omni_search_data(
            self.conn,
            "partial",
            result_kinds={"video"},
            video_completion_filters={"partial_below_minimum"},
            video_partial_min_percent=50,
            limit=100,
        )
        self.assertEqual(
            [result["item"]["video_id"] for result in below_minimum["results"]],
            ["partial-low"],
        )

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
        self.assertEqual(data["counts"]["videos"], 1)
        self.assertEqual(data["counts"]["playlists"], 1)
        self.assertEqual(data["counts"]["playlist_videos"], 1)
        self.assertEqual(data["counts"]["liked_videos"], 1)
        self.assertEqual(data["counts"]["history"], 1)
        self.assertEqual(data["counts"]["subscribed_channels"], 1)

    def test_playlist_list_filters_sorts_and_pages_on_server(self) -> None:
        self.conn.executemany(
            """
            INSERT INTO playlists(
              playlist_id, title, visibility, video_count, fetch_status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("PLz", "Zulu", "private", 2, ""),
                ("PLa", "Alpha", "public", 5, ""),
                ("PLremoved", "Removed", "private", 1, "removed"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO playlist_scans(playlist_id, scanned_at, video_count, unavailable_count) VALUES (?, '2026-07-01', ?, ?)",
            [("PLz", 2, 1), ("PLa", 5, 0)],
        )
        self.conn.commit()

        data = playlist_list_data(self.conn, sort="most_videos", limit=1)

        self.assertEqual(data["total"], 3)
        self.assertEqual(data["counts"]["private"], 1)
        self.assertEqual(data["counts"]["public"], 1)
        self.assertEqual(data["counts"]["removed"], 1)
        self.assertEqual([row["playlist_id"] for row in data["results"]], ["PLa"])
        without_removed = playlist_list_data(self.conn, include_removed=False)
        self.assertEqual(
            [row["playlist_id"] for row in without_removed["results"]],
            ["PLa", "PLz"],
        )
        removed_only = playlist_list_data(
            self.conn,
            visibilities=set(),
            include_removed=True,
        )
        self.assertEqual(
            [row["playlist_id"] for row in removed_only["results"]],
            ["PLremoved"],
        )
        unavailable = playlist_list_data(self.conn, unavailable_only=True)
        self.assertEqual([row["playlist_id"] for row in unavailable["results"]], ["PLz"])

    def test_playlist_and_channel_lists_apply_pagination_in_sql(self) -> None:
        self.conn.executemany(
            "INSERT INTO playlists(playlist_id, title) VALUES (?, ?)",
            [("PLc", "Charlie"), ("PLa", "Alpha"), ("PLb", "Bravo")],
        )
        for channel_id, title in (
            ("UCc", "Charlie Channel"),
            ("UCa", "Alpha Channel"),
            ("UCb", "Bravo Channel"),
        ):
            core.upsert_channel(self.conn, channel_id, title=title)
        for video_id, title in (
            ("video-c", "Charlie Video"),
            ("video-a", "Alpha Video"),
            ("video-b", "Bravo Video"),
        ):
            core.upsert_video(self.conn, video_id, title=title, source="test")
        self.conn.execute("UPDATE videos SET reaction = 'L'")
        self.conn.commit()

        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        try:
            playlist_page = playlist_list_data(self.conn, limit=1, offset=1)
            channel_page = channel_list_data(self.conn, limit=1, offset=1)
            video_page = video_collection_data(
                self.conn,
                scope="liked",
                sort="title",
                limit=1,
                offset=1,
            )
        finally:
            self.conn.set_trace_callback(None)

        self.assertEqual(
            [row["playlist_id"] for row in playlist_page["results"]],
            ["PLb"],
        )
        self.assertEqual(
            [row["channel_id"] for row in channel_page["results"]],
            ["UCb"],
        )
        self.assertEqual(
            [row["video_id"] for row in video_page["results"]],
            ["video-b"],
        )
        paged_queries = [
            " ".join(statement.upper().split())
            for statement in statements
            if "ORDER BY" in statement.upper() and "LIMIT" in statement.upper()
        ]
        self.assertEqual(len(paged_queries), 3)
        self.assertTrue(all("LIMIT 1 OFFSET 1" in query for query in paged_queries))

    def test_playlist_collection_plan_uses_playlist_key_before_paging(self) -> None:
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title) VALUES ('PLindexed', 'Indexed')"
        )
        for position, video_id in enumerate(("indexed-a", "indexed-b", "indexed-c")):
            self.add_video(video_id, video_id)
            self.conn.execute(
                """
                INSERT INTO playlist_items(playlist_id, position, video_id)
                VALUES ('PLindexed', ?, ?)
                """,
                (position, video_id),
            )
        self.conn.commit()

        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        try:
            page = video_collection_data(
                self.conn,
                playlist_id="PLindexed",
                sort="playlist_order",
                limit=1,
                offset=1,
            )
        finally:
            self.conn.set_trace_callback(None)

        self.assertEqual([row["video_id"] for row in page["results"]], ["indexed-b"])
        paged_query = next(
            statement
            for statement in statements
            if "WITH raw_candidates AS MATERIALIZED" in statement
            and "LIMIT 1 OFFSET 1" in " ".join(statement.upper().split())
        )
        plan = [
            str(row["detail"]).upper()
            for row in self.conn.execute(f"EXPLAIN QUERY PLAN {paged_query}")
        ]
        self.assertTrue(
            any("SEARCH PI USING" in detail and "PLAYLIST_ID=?" in detail for detail in plan),
            plan,
        )
        self.assertFalse(any(detail.startswith("SCAN PI") for detail in plan), plan)

    def test_video_and_channel_collections_hydrate_only_requested_page(self) -> None:
        self.add_video("available1", "Alpha", "UC_subscribed")
        self.add_video("unavailable1", "Beta", "UC_other")
        self.add_video("members1", "Members", "UC_other")
        self.conn.execute("UPDATE channels SET subscribed = 1 WHERE channel_id = 'UC_subscribed'")
        self.conn.execute("UPDATE videos SET is_playable = 1, reaction = 'L' WHERE video_id = 'available1'")
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'private', reaction = 'L' WHERE video_id = 'unavailable1'"
        )
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'subscriber_only', reaction = 'L' WHERE video_id = 'members1'"
        )
        self.conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLone', 'One')")
        self.conn.executemany(
            "INSERT INTO playlist_items(playlist_id, position, video_id) VALUES ('PLone', ?, ?)",
            [(1, "available1"), (2, "unavailable1")],
        )
        self.conn.commit()

        liked = video_collection_data(self.conn, scope="liked", include_unavailable=False, limit=1)
        self.assertEqual(
            liked["counts"],
            {
                "public": 1,
                "unlisted": 0,
                "private": 0,
                "unavailable": 1,
                "members_only": 1,
                "unknown": 0,
                "removed": 0,
            },
        )
        self.assertEqual([row["video_id"] for row in liked["results"]], ["available1"])
        self.assertIn("metadata_description", liked["results"][0])
        channels = channel_list_data(self.conn, categories={"subscribed"})
        self.assertEqual([row["channel_id"] for row in channels["results"]], ["UC_subscribed"])
        detail = video_detail_data(self.conn, "available1")
        self.assertEqual(detail["video_id"], "available1")

    def test_playlist_video_collection_separates_removed_from_unavailable(self) -> None:
        self.add_video("available1", "Available")
        self.add_video("unavailable1", "Unavailable")
        self.add_video("members1", "Members only")
        self.add_video("removed1", "Removed")
        self.conn.execute("UPDATE videos SET is_playable = 1 WHERE video_id = 'available1'")
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'private' WHERE video_id = 'unavailable1'"
        )
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'subscriber_only' WHERE video_id = 'members1'"
        )
        self.conn.execute("UPDATE videos SET is_playable = 1 WHERE video_id = 'removed1'")
        self.conn.executemany(
            "INSERT INTO playlists(playlist_id, title) VALUES (?, ?)",
            [("PLone", "One"), ("PLtwo", "Two")],
        )
        self.conn.executemany(
            """
            INSERT INTO playlist_items(
              playlist_id, position, video_id, membership_state, source_quality, match_type
            ) VALUES ('PLone', ?, ?, ?, ?, ?)
            """,
            [
                (1, "available1", "current", "youtube", ""),
                (2, "unavailable1", "current", "youtube", ""),
                (3, "members1", "current", "youtube", ""),
                (
                    2000,
                    "removed1",
                    "retained_unavailable",
                    "takeout",
                    "ambiguous_hidden_candidate",
                ),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO playlist_items(
              playlist_id, position, video_id, membership_state, source_quality, match_type
            ) VALUES (
              'PLtwo', 2000, 'available1', 'retained_unavailable',
              'takeout', 'ambiguous_hidden_candidate'
            )
            """
        )
        self.conn.commit()

        all_rows = video_collection_data(self.conn)
        self.assertEqual(
            all_rows["counts"],
            {
                "public": 1,
                "unlisted": 0,
                "private": 0,
                "unavailable": 1,
                "members_only": 1,
                "unknown": 0,
                "removed": 2,
            },
        )
        self.assertEqual(
            {row["video_id"] for row in all_rows["results"]},
            {"available1", "unavailable1", "members1", "removed1"},
        )

        removed = video_collection_data(
            self.conn,
            include_public=False,
            include_unlisted=False,
            include_unavailable=False,
            include_members_only=False,
            include_unknown=False,
            include_removed=True,
        )
        self.assertEqual(
            {row["video_id"] for row in removed["results"]},
            {"available1", "removed1"},
        )
        self.assertTrue(
            all(row["collection_category"] == "removed" for row in removed["results"])
        )

        unavailable = video_collection_data(
            self.conn,
            include_public=False,
            include_unlisted=False,
            include_unavailable=True,
            include_members_only=False,
            include_unknown=False,
            include_removed=False,
        )
        self.assertEqual(
            [row["video_id"] for row in unavailable["results"]],
            ["unavailable1"],
        )
        members_only = video_collection_data(
            self.conn,
            include_public=False,
            include_unlisted=False,
            include_unavailable=False,
            include_members_only=True,
            include_unknown=False,
            include_removed=False,
        )
        self.assertEqual(
            [row["video_id"] for row in members_only["results"]],
            ["members1"],
        )

    def test_video_collection_separates_playable_private_from_unavailable(self) -> None:
        self.add_video("private1", "Accessible private")
        self.add_video("unavailable1", "Inaccessible private")
        self.conn.execute(
            "UPDATE videos SET is_playable = 1, availability = 'private', reaction = 'L' WHERE video_id = 'private1'"
        )
        self.conn.execute(
            "UPDATE videos SET is_playable = 0, availability = 'private', reaction = 'L' WHERE video_id = 'unavailable1'"
        )
        self.conn.commit()

        all_rows = video_collection_data(self.conn, scope="liked")
        self.assertEqual(all_rows["counts"]["private"], 1)
        self.assertEqual(all_rows["counts"]["unavailable"], 1)

        private = video_collection_data(
            self.conn,
            scope="liked",
            include_public=False,
            include_unlisted=False,
            include_private=True,
            include_unavailable=False,
            include_members_only=False,
            include_unknown=False,
            include_removed=False,
        )
        self.assertEqual(
            [row["video_id"] for row in private["results"]],
            ["private1"],
        )

    def test_playlist_video_collection_filters_by_completion_with_stable_counts(self) -> None:
        for video_id, title in [
            ("complete1", "Complete"),
            ("partial1", "Partial"),
            ("history-complete1", "History complete"),
            ("unknown1", "Unknown"),
            ("never1", "Never watched"),
        ]:
            self.add_video(video_id, title)
        self.conn.executemany(
            "UPDATE videos SET is_playable = 1 WHERE video_id = ?",
            [("complete1",), ("partial1",), ("unknown1",), ("never1",)],
        )
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title) VALUES ('PLcompletion', 'Completion')"
        )
        self.conn.executemany(
            "INSERT INTO playlist_items(playlist_id, position, video_id) VALUES ('PLcompletion', ?, ?)",
            [
                (1, "complete1"),
                (2, "partial1"),
                (3, "history-complete1"),
                (4, "unknown1"),
                (5, "never1"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO history_events(
              event_id, video_id, watch_date, time_precision, watch_progress_percent
            )
            VALUES (?, ?, '2026-07-30', 'date_only', ?)
            """,
            [
                ("complete-watch", "complete1", 100),
                ("partial-watch", "partial1", 40),
                ("history-complete-watch", "history-complete1", 100),
                ("unknown-watch", "unknown1", 0),
            ],
        )
        self.conn.commit()

        all_rows = video_collection_data(self.conn, playlist_id="PLcompletion")
        expected_counts = {
            "complete": 2,
            "partial": 1,
            "partial_below_minimum": 0,
            "unknown": 1,
            "never_watched": 1,
        }
        self.assertEqual(all_rows["completionCounts"], expected_counts)

        filtered = video_collection_data(
            self.conn,
            playlist_id="PLcompletion",
            completion_filters={"partial", "never_watched"},
        )
        self.assertEqual(
            {row["video_id"] for row in filtered["results"]},
            {"partial1", "never1"},
        )
        self.assertEqual(filtered["total"], 2)
        self.assertEqual(filtered["completionCounts"], expected_counts)

    def test_playlist_video_collection_filters_duplicate_occurrences(self) -> None:
        self.add_video("duplicate1", "Repeated")
        self.add_video("single1", "Single")
        self.conn.executemany(
            "UPDATE videos SET is_playable = 1, availability = 'public' WHERE video_id = ?",
            [("duplicate1",), ("single1",)],
        )
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title) VALUES ('PLduplicates', 'Duplicates')"
        )
        self.conn.executemany(
            "INSERT INTO playlist_items(playlist_id, position, video_id) VALUES ('PLduplicates', ?, ?)",
            [(1, "duplicate1"), (2, "single1"), (3, "duplicate1")],
        )
        self.conn.commit()

        unfiltered = video_collection_data(
            self.conn,
            playlist_id="PLduplicates",
            sort="playlist_order",
        )
        self.assertEqual(unfiltered["total"], 3)
        self.assertEqual(unfiltered["duplicateCount"], 2)
        self.assertEqual(
            [(row["video_id"], row["position"]) for row in unfiltered["results"]],
            [("duplicate1", 1), ("single1", 2), ("duplicate1", 3)],
        )

        duplicates = video_collection_data(
            self.conn,
            playlist_id="PLduplicates",
            duplicates_only=True,
            sort="playlist_order",
        )
        self.assertEqual(duplicates["total"], 2)
        self.assertEqual(duplicates["duplicateCount"], 2)
        self.assertEqual(
            [(row["video_id"], row["position"]) for row in duplicates["results"]],
            [("duplicate1", 1), ("duplicate1", 3)],
        )

        no_matching_duplicates = video_collection_data(
            self.conn,
            playlist_id="PLduplicates",
            query="Single",
            duplicates_only=True,
        )
        self.assertEqual(no_matching_duplicates["total"], 0)
        self.assertEqual(no_matching_duplicates["duplicateCount"], 2)

    def test_playlist_collection_filters_partial_completion_by_minimum_percentage(self) -> None:
        for video_id, title in (
            ("partial-low", "Partial low"),
            ("partial-high", "Partial high"),
        ):
            self.add_video(video_id, title)
        self.conn.execute(
            "INSERT INTO playlists(playlist_id, title) VALUES ('PLminimum', 'Minimum')"
        )
        self.conn.executemany(
            "INSERT INTO playlist_items(playlist_id, position, video_id) VALUES ('PLminimum', ?, ?)",
            [(1, "partial-low"), (2, "partial-high")],
        )
        self.conn.executemany(
            """
            INSERT INTO history_events(
              event_id, video_id, watch_date, time_precision, watch_progress_percent
            ) VALUES (?, ?, '2026-07-30', 'date_only', ?)
            """,
            [
                ("partial-low-watch", "partial-low", 20),
                ("partial-high-watch", "partial-high", 75),
            ],
        )
        self.conn.commit()

        filtered = video_collection_data(
            self.conn,
            playlist_id="PLminimum",
            completion_filters={"partial"},
            partial_min_percent=50,
        )

        self.assertEqual(
            [row["video_id"] for row in filtered["results"]],
            ["partial-high"],
        )
        self.assertEqual(filtered["completionCounts"]["partial"], 1)
        self.assertEqual(filtered["completionCounts"]["partial_below_minimum"], 1)

        below_minimum = video_collection_data(
            self.conn,
            playlist_id="PLminimum",
            completion_filters={"partial_below_minimum"},
            partial_min_percent=50,
        )
        self.assertEqual(
            [row["video_id"] for row in below_minimum["results"]],
            ["partial-low"],
        )

    def test_history_search_uses_canonical_video_metadata_and_sorts_newest_first(self) -> None:
        self.add_video("old123", "Old Router Video")
        self.add_video("new123", "AT&T Fiber Without the Gateway")
        self.conn.executemany(
            "UPDATE videos SET is_playable = 1, availability = ? WHERE video_id = ?",
            [("unlisted", "old123"), ("public", "new123")],
        )
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
        self.assertEqual(
            [
                (row["video_id"], row["is_playable"], row["availability"])
                for row in data["watch"]
            ],
            [("new123", 1, "public"), ("old123", 1, "unlisted")],
        )
        filtered = history_search_data(self.conn, "fiber")
        self.assertEqual([row["video_id"] for row in filtered["watch"]], ["new123"])

    def test_history_search_hydrates_all_playlist_links_and_video_identity(self) -> None:
        self.add_video("historylinks", "Linked History Video", "UC_historylinks")
        self.conn.executemany(
            "INSERT INTO playlists(playlist_id, title) VALUES (?, ?)",
            [("PLcurrent", "Alpha Playlist"), ("PLremoved", "Zeta Playlist")],
        )
        self.conn.executemany(
            """
            INSERT INTO playlist_items(playlist_id, position, video_id, membership_state)
            VALUES (?, 1, 'historylinks', ?)
            """,
            [("PLcurrent", "current"), ("PLremoved", "retained_unavailable")],
        )
        self.conn.execute(
            """
            INSERT INTO history_events(
              event_id, video_id, watched_at, watch_date, time_precision
            ) VALUES (
              'history-links', 'historylinks', '2026-07-03T16:00:00Z', '2026-07-03', 'exact'
            )
            """
        )
        self.conn.commit()

        row = history_search_data(self.conn, "Linked History", limit=1)["watch"][0]

        self.assertEqual(row["url"], "https://www.youtube.com/watch?v=historylinks")
        self.assertEqual(
            row["metadata_channel_url"],
            "https://www.youtube.com/channel/UC_historylinks",
        )
        self.assertEqual(row["watch_dates"], ["2026-07-03"])
        self.assertEqual(
            row["playlist_links"],
            [
                {"playlist_id": "PLcurrent", "title": "Alpha Playlist", "removed": False},
                {"playlist_id": "PLremoved", "title": "Zeta Playlist", "removed": True},
            ],
        )

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

        video = video_detail_data(self.conn, "same123")
        self.assertIsNotNone(video)
        self.assertEqual(
            video["playlist_links"],
            [
                {"playlist_id": "pl1", "title": "First Playlist", "removed": False},
                {"playlist_id": "pl2", "title": "Second Playlist", "removed": True},
            ],
        )

    def test_video_detail_includes_standalone_history_video_metadata(self) -> None:
        self.add_video("historyonly1", "History Only Video", "UC_history")
        self.conn.execute(
            """
            INSERT INTO history_events(
              event_id, video_id, watched_at, watch_date, time_precision, source_type, match_type
            ) VALUES ('historyonly-event', 'historyonly1', '2026-07-02T16:00:00Z', '2026-07-02', 'exact', 'takeout', 'takeout_only')
            """
        )
        self.conn.commit()

        video = video_detail_data(self.conn, "historyonly1")

        self.assertIsNotNone(video)
        self.assertEqual(video["metadata_title"], "History Only Video")
        self.assertEqual(video["watch_count"], 1)
        self.assertEqual(video["playlist_links"], [])

    def test_playlist_list_marks_dominant_owner_and_generates_urls(self) -> None:
        core.upsert_channel(self.conn, "UC_owner", title="Library Owner")
        self.conn.executemany(
            "INSERT INTO playlists(playlist_id, title, owner_channel_id) VALUES (?, ?, 'UC_owner')",
            [(f"pl{i}", f"Playlist {i}") for i in range(6)],
        )
        self.conn.commit()
        playlists = playlist_list_data(self.conn, limit=20)["results"]
        self.assertTrue(all(row["is_library_owner"] for row in playlists))
        self.assertTrue(all(row["url"].startswith("https://www.youtube.com/playlist?list=") for row in playlists))

    def test_playlist_list_uses_library_playlist_evidence_for_visible_owners(self) -> None:
        core.upsert_channel(self.conn, "UC_owner", title="Library Owner")
        core.upsert_channel(self.conn, "UC_other", title="Other Owner")
        self.conn.executemany(
            """
            INSERT INTO playlists(
              playlist_id, title, visibility, owner_channel_id, is_library_playlist
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("PLmine", "My public playlist", "public", "UC_owner", 1),
                ("PLminelegacy", "My private playlist", "private", "UC_owner", 0),
                ("PLother", "Their unlisted playlist", "unlisted", "UC_other", 0),
            ],
        )
        self.conn.commit()

        playlists = {
            row["playlist_id"]: row
            for row in playlist_list_data(self.conn, limit=20)["results"]
        }

        self.assertEqual(playlists["PLmine"]["is_library_owner"], 1)
        self.assertEqual(playlists["PLminelegacy"]["is_library_owner"], 1)
        self.assertEqual(playlists["PLother"]["is_library_owner"], 0)

    def test_foreign_key_check_is_clean(self) -> None:
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
