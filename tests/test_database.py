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
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(schema_version, database.SCHEMA_VERSION)
        self.assertIn("uploader_category", video_columns)
        self.assertIn("payload_json", queue_columns)
        self.assertIn("plugin_subject_id", queue_columns)
        self.assertIsNotNone(plugin_run_table)
        self.assertEqual(foreign_keys, 1)

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


if __name__ == "__main__":
    unittest.main()
