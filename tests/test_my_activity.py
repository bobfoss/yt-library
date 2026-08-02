from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
import zipfile
from collections import Counter
from pathlib import Path

from yt_library import core
from yt_library.my_activity import (
    MyActivityError,
    _activity_page_from_payload,
    _continuation_request,
    _follow_my_activity_continuations,
    parse_my_activity_bootstrap,
    parse_my_activity_continuation_response,
    parse_my_activity_subscription_events,
    parse_my_activity_watch_events,
)


def activity_record(
    timestamp_us: int,
    token: str,
    title: str,
    action: str,
    url: str,
) -> list[object]:
    return [
        None,
        None,
        None,
        None,
        timestamp_us,
        token,
        None,
        None,
        None,
        [title, None, action, url],
    ]


def activity_page(*records: list[object]) -> str:
    payload = [list(records), "opaque-continuation", [1785376234, 140162000]]
    return (
        "<html><script>AF_initDataCallback({key:'ds:5',data:"
        + json.dumps(payload)
        + ",sideChannel:{}});</script></html>"
    )


def bootstrap_page(*records: list[object]) -> str:
    return (
        "<html><script>AF_dataServiceRequests={'ds:5':{id:'rpc-id',"
        'request:[[null,["youtube"]],null,100,null,[]]}};'
        'WIZ_global_data={"FdrFJe":"session-id","cfb2h":"build-label",'
        '"SNlM0e":"xsrf-token"};</script>'
        + activity_page(*records)
        + "</html>"
    )


def activity_payload(
    records: list[list[object]],
    continuation: str = "",
) -> list[object]:
    return [records, continuation, [1785376234, 140162000]]


class MyActivityTests(unittest.TestCase):
    def test_parser_extracts_distinct_exact_watch_events(self) -> None:
        page = activity_page(
            activity_record(
                1_785_376_234_140_162,
                "first-token",
                "First video",
                "Watched",
                "https://www.youtube.com/watch?v=abc123_-XYZ",
            ),
            activity_record(
                1_785_376_100_000_001,
                "second-token",
                "Second video",
                "Watched",
                "https://www.youtube.com/watch?v=second",
            ),
            activity_record(
                1_785_376_000_000_000,
                "search-token",
                "Search query",
                "Searched for",
                "https://www.youtube.com/results?search_query=test",
            ),
        )

        events = parse_my_activity_watch_events(page)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].video_id, "abc123_-XYZ")
        self.assertEqual(events[0].watched_at, "2026-07-30T01:50:34.140162Z")
        self.assertEqual(events[0].title, "First video")
        self.assertTrue(events[0].event_id.startswith("my_activity:"))
        self.assertNotIn("first-token", events[0].event_id)

    def test_parser_deduplicates_repeated_tokens_for_one_exact_watch(self) -> None:
        first = activity_record(
            1_785_376_234_140_162,
            "first-representation",
            "First video",
            "Watched",
            "https://www.youtube.com/watch?v=first",
        )
        duplicate = activity_record(
            1_785_376_234_140_162,
            "second-representation",
            "First video",
            "Watched",
            "https://www.youtube.com/watch?v=first",
        )

        together = parse_my_activity_watch_events(activity_page(first, duplicate))
        separately = parse_my_activity_watch_events(activity_page(duplicate))

        self.assertEqual(len(together), 1)
        self.assertEqual(together[0].event_id, separately[0].event_id)

    def test_parser_keeps_repeat_watches_with_distinct_exact_times(self) -> None:
        events = parse_my_activity_watch_events(
            activity_page(
                activity_record(
                    1_785_376_234_140_162,
                    "first-token",
                    "First video",
                    "Watched",
                    "https://www.youtube.com/watch?v=first",
                ),
                activity_record(
                    1_785_376_234_140_163,
                    "second-token",
                    "First video",
                    "Watched",
                    "https://www.youtube.com/watch?v=first",
                ),
            )
        )

        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].event_id, events[1].event_id)

    def test_database_collection_is_idempotent_and_reports_overlap(self) -> None:
        initial = parse_my_activity_watch_events(
            activity_page(
                activity_record(
                    1_785_376_234_140_162,
                    "first-token",
                    "First video",
                    "Watched",
                    "https://www.youtube.com/watch?v=first",
                )
            )
        )
        later = parse_my_activity_watch_events(
            activity_page(
                activity_record(
                    1_785_376_300_000_000,
                    "new-token",
                    "New video",
                    "Watched",
                    "https://www.youtube.com/watch?v=new",
                ),
                activity_record(
                    1_785_376_234_140_162,
                    "first-token",
                    "First video",
                    "Watched",
                    "https://www.youtube.com/watch?v=first",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    first_stats = core.save_my_activity_events(conn, initial, [], "UTC")
                with conn:
                    later_stats = core.save_my_activity_events(conn, later, [], "UTC")
                with conn:
                    repeat_stats = core.save_my_activity_events(conn, later, [], "UTC")
                stored = conn.execute("SELECT COUNT(*) FROM my_activity_watch_events").fetchone()[0]
            finally:
                conn.close()

            self.assertTrue(first_stats["first_collection"])
            self.assertEqual(later_stats["watch_inserted"], 1)
            self.assertEqual(later_stats["overlap_events"], 1)
            self.assertEqual(repeat_stats["watch_inserted"], 0)
            self.assertEqual(stored, 2)

    def test_database_reuses_legacy_event_id_for_same_exact_occurrence(self) -> None:
        event = parse_my_activity_watch_events(
            activity_page(
                activity_record(
                    1_785_376_234_140_162,
                    "current-token",
                    "First video",
                    "Watched",
                    "https://www.youtube.com/watch?v=first",
                )
            )
        )[0]
        legacy = type(event)(
            event_id="my_activity:legacy-token-hash",
            video_id=event.video_id,
            watched_at=event.watched_at,
            title=event.title,
            url=event.url,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    first = core.save_my_activity_events(conn, [legacy], [], "UTC")
                with conn:
                    second = core.save_my_activity_events(conn, [event], [], "UTC")
                source_rows = conn.execute(
                    "SELECT * FROM my_activity_watch_events"
                ).fetchall()
                history_rows = conn.execute("SELECT * FROM history_events").fetchall()
            finally:
                conn.close()

        self.assertEqual(first["watch_inserted"], 1)
        self.assertEqual(second["watch_inserted"], 0)
        self.assertEqual(second["watch_existing"], 1)
        self.assertEqual(second["overlap_events"], 1)
        self.assertEqual(len(source_rows), 1)
        self.assertEqual(source_rows[0]["event_id"], legacy.event_id)
        self.assertEqual(len(history_rows), 1)
        self.assertEqual(history_rows[0]["my_activity_event_id"], legacy.event_id)

    def test_migration_collapses_duplicate_exact_occurrences_and_keeps_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(
                    """
                    CREATE TABLE schema_migrations (
                      version INTEGER PRIMARY KEY,
                      applied_at TEXT NOT NULL
                    );
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (13, '2026-08-01T00:00:00Z');
                    CREATE TABLE my_activity_watch_events (
                      event_id TEXT PRIMARY KEY,
                      video_id TEXT NOT NULL,
                      watched_at TEXT NOT NULL,
                      observed_title TEXT NOT NULL DEFAULT '',
                      observed_url TEXT NOT NULL DEFAULT '',
                      collected_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    );
                    CREATE TABLE my_activity_subscription_events (
                      event_id TEXT PRIMARY KEY,
                      channel_id TEXT NOT NULL,
                      subscribed_at TEXT NOT NULL,
                      observed_title TEXT NOT NULL DEFAULT '',
                      observed_url TEXT NOT NULL DEFAULT '',
                      collected_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    );
                    CREATE TABLE history_events (
                      event_id TEXT PRIMARY KEY,
                      video_id TEXT NOT NULL,
                      watched_at TEXT,
                      watch_date TEXT,
                      time_precision TEXT NOT NULL,
                      source_type TEXT NOT NULL DEFAULT '',
                      match_type TEXT NOT NULL DEFAULT '',
                      youtube_ordinal INTEGER,
                      my_activity_event_id TEXT,
                      takeout_history_key TEXT,
                      takeout_row_key TEXT,
                      watch_progress_percent INTEGER NOT NULL DEFAULT 0,
                      watch_resume_seconds INTEGER NOT NULL DEFAULT 0,
                      observed_at TEXT,
                      imported_at TEXT NOT NULL DEFAULT '',
                      updated_at TEXT NOT NULL DEFAULT ''
                    );
                    """
                )
                watched_at = "2026-08-01T16:56:04.538418Z"
                for index in range(4):
                    event_id = f"my_activity:representation-{index}"
                    raw.execute(
                        """
                        INSERT INTO my_activity_watch_events(
                          event_id, video_id, watched_at, observed_title,
                          observed_url, collected_at, updated_at
                        ) VALUES (?, 'duplicate-video', ?, 'Duplicate video',
                                  'https://www.youtube.com/watch?v=duplicate-video',
                                  ?, ?)
                        """,
                        (
                            event_id,
                            watched_at,
                            f"2026-08-01T20:0{index}:00Z",
                            f"2026-08-01T21:0{index}:00Z",
                        ),
                    )
                    raw.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watched_at, watch_date,
                          time_precision, source_type, match_type,
                          youtube_ordinal, my_activity_event_id,
                          watch_progress_percent, imported_at, updated_at
                        ) VALUES (?, 'duplicate-video', ?, '2026-08-01',
                                  'exact', ?, ?, ?, ?, ?,
                                  '2026-08-01T20:00:00Z',
                                  '2026-08-01T20:00:00Z')
                        """,
                        (
                            event_id,
                            watched_at,
                            "my_activity_youtube" if index == 2 else "my_activity",
                            "video_id_date" if index == 2 else "my_activity_only",
                            9 if index == 2 else None,
                            event_id,
                            100 if index == 2 else 0,
                        ),
                    )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                source_rows = conn.execute(
                    "SELECT * FROM my_activity_watch_events"
                ).fetchall()
                history_rows = conn.execute("SELECT * FROM history_events").fetchall()
                schema_version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    with conn:
                        conn.execute(
                            """
                            INSERT INTO my_activity_watch_events(
                              event_id, video_id, watched_at, collected_at, updated_at
                            ) VALUES ('my_activity:new-representation',
                                      'duplicate-video', ?,
                                      '2026-08-01T22:00:00Z',
                                      '2026-08-01T22:00:00Z')
                            """,
                            (watched_at,),
                        )
            finally:
                conn.close()

        self.assertEqual(schema_version, core.SCHEMA_VERSION)
        self.assertEqual(len(source_rows), 1)
        self.assertEqual(source_rows[0]["event_id"], "my_activity:representation-2")
        self.assertEqual(source_rows[0]["collected_at"], "2026-08-01T20:00:00Z")
        self.assertEqual(len(history_rows), 1)
        self.assertEqual(history_rows[0]["youtube_ordinal"], 9)
        self.assertEqual(history_rows[0]["watch_progress_percent"], 100)

    def test_parser_replaces_invalid_json_surrogates_before_writing(self) -> None:
        events = parse_my_activity_watch_events(
            activity_page(
                activity_record(
                    1_785_376_234_140_162,
                    "surrogate-token",
                    "Broken \udc90 title",
                    "Watched",
                    "https://www.youtube.com/watch?v=surrogate",
                )
            )
        )

        self.assertEqual(events[0].title, "Broken � title")

    def test_parser_extracts_subscription_dates_and_channel_ids(self) -> None:
        events = parse_my_activity_subscription_events(
            activity_page(
                activity_record(
                    1_785_376_234_140_162,
                    "subscription-token",
                    "Example channel",
                    "Subscribed to",
                    "https://www.youtube.com/channel/UCexample123",
                )
            )
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].channel_id, "UCexample123")
        self.assertEqual(events[0].subscribed_at, "2026-07-30T01:50:34.140162Z")
        self.assertEqual(events[0].title, "Example channel")

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    core.save_my_activity_events(conn, [], events, "UTC")
                channel = conn.execute(
                    "SELECT * FROM channels WHERE channel_id = 'UCexample123'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(channel["subscribed_at"], "2026-07-30T01:50:34.140162Z")
        self.assertEqual(channel["subscribed_at_source"], "my_activity")
        self.assertIsNone(channel["first_seen_at"])

    def test_bootstrap_exposes_dynamic_continuation_request(self) -> None:
        page, session = parse_my_activity_bootstrap(
            bootstrap_page(
                activity_record(
                    1_785_376_234_140_162,
                    "first-token",
                    "First video",
                    "Watched",
                    "https://www.youtube.com/watch?v=first",
                )
            )
        )

        request = _continuation_request(session, "next-page-token")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        body = urllib.parse.parse_qs(request.data.decode("utf-8"))
        envelope = json.loads(body["f.req"][0])
        arguments = json.loads(envelope[0][0][1])

        self.assertEqual(len(page.events), 1)
        self.assertEqual(session.rpc_id, "rpc-id")
        self.assertEqual(query["rpcids"], ["rpc-id"])
        self.assertEqual(body["at"], ["xsrf-token"])
        self.assertEqual(arguments[1], "next-page-token")

    def test_continuation_parser_reads_framed_rpc_payload(self) -> None:
        payload = activity_payload(
            [
                activity_record(
                    1_785_376_100_000_001,
                    "second-token",
                    "Second video",
                    "Watched",
                    "https://www.youtube.com/watch?v=second",
                )
            ],
            "another-page",
        )
        response = (
            ")]}'\n\n123\n"
            + json.dumps([["wrb.fr", "rpc-id", json.dumps(payload), None]])
            + "\n25\n"
            + json.dumps([["di", 1]])
        )

        page = parse_my_activity_continuation_response(response, "rpc-id")

        self.assertEqual(page.activity_records, 1)
        self.assertEqual(page.events[0].video_id, "second")
        self.assertEqual(page.continuation_token, "another-page")

    def test_continuation_parser_accepts_terminal_empty_page(self) -> None:
        payload = [None, None, [1785376234, 140162000]]
        response = (
            ")]}'\n\n"
            + json.dumps([["wrb.fr", "rpc-id", json.dumps(payload), None]])
        )

        page = parse_my_activity_continuation_response(response, "rpc-id")

        self.assertEqual(page.activity_records, 0)
        self.assertEqual(page.events, [])
        self.assertEqual(page.continuation_token, "")

    def test_continuation_walk_is_bounded_and_rejects_loops(self) -> None:
        first = _activity_page_from_payload(
            activity_payload(
                [
                    activity_record(
                        1_785_376_234_140_162,
                        "first-token",
                        "First video",
                        "Watched",
                        "https://www.youtube.com/watch?v=first",
                    )
                ],
                "page-two",
            )
        )
        second = _activity_page_from_payload(
            activity_payload(
                [
                    activity_record(
                        1_785_376_100_000_001,
                        "second-token",
                        "Second video",
                        "Watched",
                        "https://www.youtube.com/watch?v=second",
                    )
                ],
                "page-three",
            )
        )
        calls: list[str] = []

        pages = _follow_my_activity_continuations(
            first,
            lambda token: calls.append(token) or second,
            max_pages=2,
        )

        self.assertEqual(len(pages), 2)
        self.assertEqual(calls, ["page-two"])
        looping = _activity_page_from_payload(
            activity_payload(
                [
                    activity_record(
                        1_785_376_000_000_001,
                        "third-token",
                        "Third video",
                        "Watched",
                        "https://www.youtube.com/watch?v=third",
                    )
                ],
                "page-two",
            )
        )
        with self.assertRaises(MyActivityError):
            _follow_my_activity_continuations(
                first,
                lambda _token: looping,
                max_pages=3,
            )

    def test_database_import_reconciles_and_preserves_exact_event(self) -> None:
        event = parse_my_activity_watch_events(
            activity_page(
                activity_record(
                    1_785_376_234_140_162,
                    "database-token",
                    "Database video",
                    "Watched",
                    "https://www.youtube.com/watch?v=database",
                )
            )
        )[0]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    core.upsert_video(conn, event.video_id, title="", source="test")
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watch_date, time_precision,
                          source_type, match_type, youtube_ordinal,
                          watch_progress_percent, watch_resume_seconds
                        )
                        VALUES ('youtube:existing', ?, '2026-07-29', 'date_only',
                                'youtube', 'youtube_only', 7, 64, 217)
                        """,
                        (event.video_id,),
                    )
            finally:
                conn.close()

            conn = core.connect(db_path)
            try:
                with conn:
                    first = core.save_my_activity_events(
                        conn, [event], [], "America/Los_Angeles"
                    )
                with conn:
                    second = core.save_my_activity_events(
                        conn, [event], [], "America/Los_Angeles"
                    )
                row = conn.execute("SELECT * FROM history_events").fetchone()
                snapshot = core.youtube_history_occurrence_snapshot(conn)
                with conn:
                    core.save_youtube_history_events(
                        conn,
                        [{"video_id": event.video_id, "watch_date": "2026-07-29"}],
                        1,
                        snapshot,
                        Counter(),
                    )
                refreshed = conn.execute("SELECT * FROM history_events").fetchone()
                with conn:
                    core.synchronize_youtube_history_order(
                        conn,
                        core.youtube_history_occurrence_snapshot(conn),
                        set(),
                        processed=1,
                        shift=0,
                        final=True,
                        complete_scan=True,
                    )
                preserved = conn.execute("SELECT * FROM history_events").fetchone()
            finally:
                conn.close()

        self.assertEqual(first["watch_inserted"], 1)
        self.assertEqual(first["matched_history_rows"], 1)
        self.assertEqual(second["watch_existing"], 1)
        self.assertEqual(row["event_id"], event.event_id)
        self.assertEqual(row["watched_at"], event.watched_at)
        self.assertEqual(row["time_precision"], "exact")
        self.assertEqual(row["source_type"], "my_activity_youtube")
        self.assertEqual(row["match_type"], "video_id_date")
        self.assertEqual(row["youtube_ordinal"], 7)
        self.assertEqual(row["watch_progress_percent"], 64)
        self.assertEqual(refreshed["watched_at"], event.watched_at)
        self.assertEqual(refreshed["time_precision"], "exact")
        self.assertEqual(refreshed["source_type"], "my_activity_youtube")
        self.assertEqual(preserved["event_id"], event.event_id)
        self.assertIsNone(preserved["youtube_ordinal"])
        self.assertEqual(preserved["source_type"], "my_activity")
        self.assertEqual(preserved["match_type"], "my_activity_only")

    def test_later_takeout_import_merges_by_video_and_second(self) -> None:
        event = parse_my_activity_watch_events(
            activity_page(
                activity_record(
                    1_785_376_234_140_162,
                    "takeout-merge-token",
                    "Merged video",
                    "Watched",
                    "https://www.youtube.com/watch?v=merged",
                )
            )
        )[0]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    core.save_my_activity_events(
                        conn, [event], [], "America/Los_Angeles"
                    )
            finally:
                conn.close()
            zip_path = root / "takeout-20260730T020000Z-001.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr(
                    "Takeout/YouTube and YouTube Music/history/watch-history.json",
                    json.dumps(
                        [
                            {
                                "title": "Watched Merged video",
                                "titleUrl": "https://www.youtube.com/watch?v=merged",
                                "time": "2026-07-30T01:50:34.000Z",
                            }
                        ]
                    ),
                )
            args = argparse.Namespace(
                db=str(db_path),
                takeout=str(root),
                history_key="",
                config_data={"display_timezone": "America/Los_Angeles"},
            )

            first = core.import_history(args)
            second = core.import_history(args)

            conn = core.connect(db_path)
            try:
                rows = conn.execute("SELECT * FROM history_events").fetchall()
            finally:
                conn.close()

        self.assertEqual(first["merged_my_activity_rows"], 1)
        self.assertEqual(second["inserted_watch_rows"], 0)
        self.assertEqual(second["duplicate_watch_rows"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_id"], event.event_id)
        self.assertIsNotNone(rows[0]["takeout_history_key"])
        self.assertEqual(rows[0]["source_type"], "takeout_my_activity")
        self.assertEqual(rows[0]["match_type"], "video_id_time")


if __name__ == "__main__":
    unittest.main()
