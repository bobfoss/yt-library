import sqlite3
import tempfile
import unittest
from pathlib import Path

from yt_library import core, database, history, time_utils


class DatabaseModuleTests(unittest.TestCase):
    def test_core_preserves_moved_helper_imports(self) -> None:
        self.assertIs(core.connect, database.connect)
        self.assertIs(core.migrate_database, database.migrate_database)
        self.assertIs(core.history_source_type_for_identity, history.history_source_type_for_identity)
        self.assertIs(core.history_match_type_for_identity, history.history_match_type_for_identity)
        self.assertIs(core.utc_now, time_utils.utc_now)
        self.assertIs(core.utc_days_ago, time_utils.utc_days_ago)
        self.assertEqual(core.SCHEMA_VERSION, database.SCHEMA_VERSION)
        self.assertEqual(core.SCHEMA, database.SCHEMA)

    def test_database_module_bootstraps_current_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                schema_version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                video_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(videos)")
                }
                queue_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(worker_queue)")
                }
                plugin_run_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='plugin_worker_runs'"
                ).fetchone()
                collaborator_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='playlist_collaborators'"
                ).fetchone()
                featured_channels_table = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='channel_featured_channels'"
                ).fetchone()
                clips_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='clips'"
                ).fetchone()
                tags_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='tags'"
                ).fetchone()
                note_fts_table = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='entity_note_fts'"
                ).fetchone()
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(schema_version, database.SCHEMA_VERSION)
        self.assertIn("uploader_category", video_columns)
        self.assertTrue(
            {"movie_rating", "movie_release_date", "movie_offer"}.issubset(
                video_columns
            )
        )
        self.assertTrue(
            {
                "max_video_height",
                "spatial_format",
                "stereo_layout",
                "dynamic_range",
                "license",
                "location_name",
            }.issubset(video_columns)
        )
        self.assertIn("payload_json", queue_columns)
        self.assertIn("plugin_subject_id", queue_columns)
        self.assertIn("clip_id", queue_columns)
        self.assertIsNotNone(plugin_run_table)
        self.assertIsNotNone(collaborator_table)
        self.assertIsNotNone(featured_channels_table)
        self.assertIsNotNone(clips_table)
        self.assertIsNotNone(tags_table)
        self.assertIsNotNone(note_fts_table)
        self.assertIn("note", video_columns)
        self.assertEqual(foreign_keys, 1)

    def test_database_module_migrates_annotations_from_version_29(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    for trigger_name in (
                        "videos_note_fts_insert", "videos_note_fts_update", "videos_note_fts_delete",
                        "clips_note_fts_insert", "clips_note_fts_update", "clips_note_fts_delete",
                        "playlists_note_fts_insert", "playlists_note_fts_update", "playlists_note_fts_delete",
                        "channels_note_fts_insert", "channels_note_fts_update", "channels_note_fts_delete",
                    ):
                        conn.execute(f"DROP TRIGGER {trigger_name}")
                    conn.execute("DROP TABLE entity_note_fts")
                    for table_name in ("video_tags", "clip_tags", "playlist_tags", "channel_tags", "tags"):
                        conn.execute(f"DROP TABLE {table_name}")
                    for table_name in ("videos", "clips", "playlists", "channels"):
                        conn.execute(f"ALTER TABLE {table_name} DROP COLUMN note")
                    conn.execute("DELETE FROM schema_migrations WHERE version >= 30")
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                note_columns = {
                    table_name: {
                        row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")
                    }
                    for table_name in ("videos", "clips", "playlists", "channels")
                }
                tags_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='tags'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertTrue(all("note" in columns for columns in note_columns.values()))
        self.assertIsNotNone(tags_table)

    def test_database_module_migrates_uploader_category_from_version_14(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute("ALTER TABLE videos DROP COLUMN uploader_category")
                    conn.execute("DELETE FROM schema_migrations")
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (14, '2026-08-03T00:00:00Z')"
                    )
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                schema_version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                video_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(videos)")
                }
            finally:
                conn.close()

        self.assertEqual(schema_version, database.SCHEMA_VERSION)
        self.assertIn("uploader_category", video_columns)

    def test_database_module_migrates_plugin_worker_support_from_version_15(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute("DROP TABLE plugin_worker_log")
                    conn.execute("DROP TABLE plugin_worker_runs")
                    conn.execute("ALTER TABLE worker_queue DROP COLUMN payload_json")
                    conn.execute("ALTER TABLE worker_queue DROP COLUMN plugin_subject_id")
                    conn.execute("DELETE FROM schema_migrations")
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (15, '2026-08-03T00:00:00Z')"
                    )
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                queue_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(worker_queue)")
                }
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertIn("payload_json", queue_columns)
        self.assertIn("plugin_subject_id", queue_columns)
        self.assertIn("plugin_worker_runs", tables)
        self.assertIn("plugin_worker_log", tables)

    def test_database_module_migrates_playlist_collaborators_from_version_17(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute("DROP TABLE playlist_collaborators")
                    conn.execute("DELETE FROM schema_migrations")
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (17, '2026-08-05T00:00:00Z')"
                    )
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='playlist_collaborators'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertIsNotNone(table)

    def test_database_module_migrates_clips_from_version_18(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute("DROP TABLE clips")
                    conn.execute("ALTER TABLE worker_queue DROP COLUMN clip_id")
                    conn.execute("DELETE FROM schema_migrations WHERE version >= 19")
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                clip_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='clips'"
                ).fetchone()
                queue_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(worker_queue)")
                }
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertIsNotNone(clip_table)
        self.assertIn("clip_id", queue_columns)

    def test_database_module_migrates_reactions_from_version_20(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.executescript(
                        """
                        ALTER TABLE videos RENAME COLUMN reaction TO current_reaction;
                        ALTER TABLE videos ADD COLUMN reaction TEXT NOT NULL DEFAULT '';
                        ALTER TABLE videos DROP COLUMN current_reaction;
                        ALTER TABLE playlist_scan_worker_runs
                          ADD COLUMN stale_days INTEGER NOT NULL DEFAULT 0;
                        """
                    )
                    conn.executemany(
                        "INSERT INTO videos(video_id, title, reaction) VALUES (?, ?, ?)",
                        (
                            ("legacy-like", "Legacy like", "L"),
                            ("legacy-dislike", "Legacy dislike", "D"),
                            ("legacy-unknown", "Legacy unknown", ""),
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_scan_worker_runs(
                          run_id, status, started_at, stale_days, message
                        ) VALUES ('legacy-playlist-run', 'complete',
                                  '2026-08-01T00:00:00Z', 7, 'Preserve me')
                        """
                    )
                    conn.execute("DELETE FROM schema_migrations WHERE version >= 21")
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                reactions = {
                    row["video_id"]: row["reaction"]
                    for row in conn.execute(
                        "SELECT video_id, reaction FROM videos ORDER BY video_id"
                    )
                }
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                videos_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'videos'"
                ).fetchone()[0]
                playlist_run_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(playlist_scan_worker_runs)")
                }
                playlist_run = conn.execute(
                    """
                    SELECT status, message
                    FROM playlist_scan_worker_runs
                    WHERE run_id = 'legacy-playlist-run'
                    """
                ).fetchone()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE videos SET reaction = 'INVALID' WHERE video_id = 'legacy-like'"
                    )
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertEqual(reactions["legacy-like"], "LIKE")
        self.assertEqual(reactions["legacy-dislike"], "DISLIKE")
        self.assertEqual(reactions["legacy-unknown"], "")
        self.assertIn("INDIFFERENT", videos_sql)
        self.assertNotIn("stale_days", playlist_run_columns)
        self.assertEqual(tuple(playlist_run), ("complete", "Preserve me"))

    def test_database_module_migrates_clip_feed_order_from_version_21(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute("DROP INDEX idx_clips_feed_ordinal")
                    conn.execute("ALTER TABLE clips DROP COLUMN youtube_feed_ordinal")
                    conn.execute("DELETE FROM schema_migrations WHERE version >= 22")
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                clip_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(clips)")
                }
                index = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'idx_clips_feed_ordinal'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertIn("youtube_feed_ordinal", clip_columns)
        self.assertIsNotNone(index)

    def test_database_module_migrates_video_type_from_version_22(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute("ALTER TABLE videos DROP COLUMN video_type")
                    conn.execute("DELETE FROM schema_migrations WHERE version >= 23")
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                video_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(videos)")
                }
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertIn("video_type", video_columns)

    def test_database_module_migrates_movie_metadata_from_version_23(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO videos(video_id, video_type) VALUES ('short123456', 'short')"
                    )
                    conn.executescript(
                        """
                        ALTER TABLE videos RENAME COLUMN video_type TO current_video_type;
                        ALTER TABLE videos ADD COLUMN video_type TEXT NOT NULL DEFAULT ''
                          CHECK (video_type IN ('', 'video', 'short', 'live'));
                        UPDATE videos SET video_type = current_video_type;
                        ALTER TABLE videos DROP COLUMN current_video_type;
                        ALTER TABLE videos DROP COLUMN movie_rating;
                        ALTER TABLE videos DROP COLUMN movie_release_date;
                        ALTER TABLE videos DROP COLUMN movie_offer;
                        DELETE FROM schema_migrations WHERE version >= 24;
                        """
                    )
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                video_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(videos)")
                }
                retained_type = conn.execute(
                    "SELECT video_type FROM videos WHERE video_id = 'short123456'"
                ).fetchone()[0]
                with conn:
                    conn.execute(
                        "UPDATE videos SET video_type = 'movie', movie_rating = 'R', "
                        "movie_release_date = '2015', movie_offer = 'Free' "
                        "WHERE video_id = 'short123456'"
                    )
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertEqual(retained_type, "short")
        self.assertTrue(
            {"movie_rating", "movie_release_date", "movie_offer"}.issubset(
                video_columns
            )
        )

    def test_database_module_migrates_video_features_from_version_24(self) -> None:
        feature_columns = {
            "max_video_height",
            "spatial_format",
            "stereo_layout",
            "dynamic_range",
            "license",
            "location_name",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO videos(video_id, title) VALUES ('featurekeep1', 'Keep me')"
                    )
                    for column in feature_columns:
                        conn.execute(f"ALTER TABLE videos DROP COLUMN {column}")
                    conn.execute("DELETE FROM schema_migrations WHERE version >= 25")
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                video_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(videos)")
                }
                retained = conn.execute(
                    "SELECT title FROM videos WHERE video_id = 'featurekeep1'"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertEqual(retained, "Keep me")
        self.assertTrue(feature_columns.issubset(video_columns))

    def test_database_module_migrates_broadcast_metadata_from_version_25(self) -> None:
        broadcast_columns = {
            "broadcast_status",
            "broadcast_started_at",
            "broadcast_ended_at",
            "broadcast_status_checked_at",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.executescript(
                        """
                        ALTER TABLE videos RENAME COLUMN video_type TO current_video_type;
                        ALTER TABLE videos ADD COLUMN video_type TEXT NOT NULL DEFAULT ''
                          CHECK (video_type IN ('', 'video', 'short', 'live', 'movie'));
                        UPDATE videos
                        SET video_type = CASE current_video_type
                          WHEN 'livestream' THEN 'live'
                          ELSE current_video_type
                        END;
                        ALTER TABLE videos DROP COLUMN current_video_type;
                        ALTER TABLE videos DROP COLUMN broadcast_status;
                        ALTER TABLE videos DROP COLUMN broadcast_started_at;
                        ALTER TABLE videos DROP COLUMN broadcast_ended_at;
                        ALTER TABLE videos DROP COLUMN broadcast_status_checked_at;
                        INSERT INTO videos(video_id, title, video_type)
                        VALUES ('legacy-live', 'Legacy live stream', 'live');
                        DELETE FROM schema_migrations WHERE version >= 26;
                        """
                    )
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                video_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(videos)")
                }
                retained = conn.execute(
                    "SELECT title, video_type, broadcast_status "
                    "FROM videos WHERE video_id = 'legacy-live'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertTrue(broadcast_columns.issubset(video_columns))
        self.assertEqual(
            tuple(retained),
            ("Legacy live stream", "livestream", None),
        )

    def test_database_module_migrates_content_warning_metadata_from_version_26(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO videos(video_id, title) "
                        "VALUES ('warningkeep1', 'Keep warning candidate')"
                    )
                    conn.execute("ALTER TABLE videos DROP COLUMN content_check_required")
                    conn.execute("ALTER TABLE videos DROP COLUMN content_check_reason")
                    conn.execute("DELETE FROM schema_migrations WHERE version >= 27")
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                video_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(videos)")
                }
                retained = conn.execute(
                    "SELECT title, content_check_required, content_check_reason "
                    "FROM videos WHERE video_id = 'warningkeep1'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertTrue(
            {"content_check_required", "content_check_reason"}.issubset(video_columns)
        )
        self.assertEqual(tuple(retained), ("Keep warning candidate", None, None))

    def test_database_module_migrates_featured_channels_from_version_27(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO channels(channel_id, title) "
                        "VALUES ('UCfeaturedowner', 'Featured owner')"
                    )
                    conn.execute("DROP TABLE channel_featured_channels")
                    conn.execute("DELETE FROM schema_migrations WHERE version >= 28")
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                table = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='channel_featured_channels'"
                ).fetchone()
                retained = conn.execute(
                    "SELECT title FROM channels WHERE channel_id = 'UCfeaturedowner'"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertIsNotNone(table)
        self.assertEqual(retained, "Featured owner")

    def test_database_module_migrates_cookie_auth_status_from_version_28(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO channels(channel_id, title) "
                        "VALUES ('UCcookiekeep', 'Cookie migration survivor')"
                    )
                    conn.execute("DROP TABLE cookie_auth_status")
                    conn.execute("DELETE FROM schema_migrations WHERE version >= 29")
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                table = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='cookie_auth_status'"
                ).fetchone()
                retained = conn.execute(
                    "SELECT title FROM channels WHERE channel_id = 'UCcookiekeep'"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(version, database.SCHEMA_VERSION)
        self.assertIsNotNone(table)
        self.assertEqual(retained, "Cookie migration survivor")


if __name__ == "__main__":
    unittest.main()
