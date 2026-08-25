from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import Mock

from yt_library import core, server
from yt_library.annotations import (
    annotation_for_entity,
    annotation_search_matches,
    save_entity_annotation,
    tag_suggestions,
)
from yt_library.queries import history_search_data, omni_search_data, video_collection_data


class AnnotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "library.sqlite3"
        core.migrate_database(self.db_path)
        self.conn = core.connect(self.db_path)
        with self.conn:
            self.conn.execute(
                "INSERT INTO channels(channel_id, title) VALUES ('UCnotes', 'Notes channel')"
            )
            self.conn.execute(
                "INSERT INTO playlists(playlist_id, title) VALUES ('PLnotes', 'Notes playlist')"
            )
            self.conn.execute(
                "INSERT INTO videos(video_id, title, channel_id) "
                "VALUES ('video-notes', 'Notes video', 'UCnotes')"
            )
            self.conn.execute(
                "INSERT INTO clips(clip_id, title, source_video_id) "
                "VALUES ('clip-notes', 'Notes clip', 'video-notes')"
            )
            self.conn.execute(
                "INSERT INTO playlist_items(playlist_id, position, video_id) "
                "VALUES ('PLnotes', 1, 'video-notes')"
            )

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_notes_and_tags_are_normalized_and_searchable(self) -> None:
        saved = save_entity_annotation(
            self.conn,
            "video",
            "video-notes",
            note="  Research this later  ",
            tags=[" Deep   Dive ", "deep dive", "Reference"],
        )

        self.assertEqual(saved, {"note": "Research this later", "tags": ["Deep Dive", "Reference"]})
        self.assertEqual(annotation_for_entity(self.conn, "video", "video-notes"), saved)
        self.assertEqual(tag_suggestions(self.conn, "deep"), ["Deep Dive"])
        self.assertEqual(
            annotation_search_matches(
                self.conn,
                "resear",
                search_notes=True,
                search_tags=False,
                entity_kinds={"video"},
            ),
            {"video": {"video-notes"}},
        )

    def test_search_and_playlist_facets_use_stable_note_counts(self) -> None:
        save_entity_annotation(
            self.conn,
            "video",
            "video-notes",
            note="Watch again",
            tags=["Favorite"],
        )

        omni = omni_search_data(
            self.conn,
            "favorite",
            search_fields={"tags"},
            result_kinds={"video"},
            video_note_filters={"with_note"},
        )
        collection = video_collection_data(
            self.conn,
            playlist_id="PLnotes",
            query="watch",
            search_fields={"notes"},
            note_filters={"with_note"},
        )

        self.assertEqual(omni["total"], 1)
        self.assertEqual(omni["metaCounts"]["videos"]["with_note"], 1)
        self.assertEqual(omni["results"][0]["item"]["tags"], ["Favorite"])
        self.assertEqual(collection["total"], 1)
        self.assertEqual(collection["noteCounts"], {"with_note": 1, "without_note": 0})
        self.assertEqual(collection["results"][0]["note"], "Watch again")

    def test_history_cards_receive_video_notes_and_tags(self) -> None:
        save_entity_annotation(
            self.conn,
            "video",
            "video-notes",
            note="History note",
            tags=["Favorite", "Research"],
        )
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO history_events(
                  event_id, video_id, watched_at, watch_date, time_precision,
                  source_type, match_type
                ) VALUES (
                  'history:video-notes', 'video-notes',
                  '2026-08-24T12:00:00Z', '2026-08-24',
                  'exact', 'takeout', 'takeout_only'
                )
                """
            )

        row = history_search_data(self.conn, "", limit=1)["watch"][0]

        self.assertEqual(row["note"], "History note")
        self.assertEqual(row["tags"], ["Favorite", "Research"])

    def test_annotation_put_route_updates_a_canonical_entity(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = self.db_path
        body = json.dumps({"note": "Route note", "tags": ["Route tag"]}).encode()
        handler.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
        }
        handler.rfile = io.BytesIO(body)
        handler.send_json = Mock()

        handler._handle_annotation_put(
            urllib.parse.urlparse("/api/annotations/video/video-notes")
        )

        payload = handler.send_json.call_args.args[0]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["note"], "Route note")
        self.assertEqual(payload["tags"], ["Route tag"])


if __name__ == "__main__":
    unittest.main()
