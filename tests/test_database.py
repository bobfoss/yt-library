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
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(schema_version, database.SCHEMA_VERSION)
        self.assertEqual(foreign_keys, 1)


if __name__ == "__main__":
    unittest.main()
