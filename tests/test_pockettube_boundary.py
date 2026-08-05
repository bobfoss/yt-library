from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yt_library import database


class PocketTubeBoundaryTests(unittest.TestCase):
    def test_version_17_removes_only_the_legacy_youtube_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLone', 'One')"
                    )
                    conn.execute(
                        "INSERT INTO groups(group_key, name, position) "
                        "VALUES ('youtube-ungrouped', 'Uncategorized', 0)"
                    )
                    conn.execute(
                        "INSERT INTO groups(group_key, name, position) "
                        "VALUES ('user-group', 'User group', 1)"
                    )
                    conn.execute(
                        "INSERT INTO group_playlists(group_key, playlist_id, position) "
                        "VALUES ('youtube-ungrouped', 'PLone', 0)"
                    )
                    conn.execute(
                        "INSERT INTO group_playlists(group_key, playlist_id, position) "
                        "VALUES ('user-group', 'PLone', 0)"
                    )
                    conn.execute("DROP TABLE playlist_collaborators")
                    conn.execute("DELETE FROM schema_migrations WHERE version >= 17")
            finally:
                conn.close()

            database.migrate_database(db_path)
            conn = database.connect(db_path)
            try:
                groups = {
                    row["group_key"]
                    for row in conn.execute("SELECT group_key FROM groups")
                }
                memberships = {
                    (row["group_key"], row["playlist_id"])
                    for row in conn.execute(
                        "SELECT group_key, playlist_id FROM group_playlists"
                    )
                }
                playlists = conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0]
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertNotIn("youtube-ungrouped", groups)
        self.assertEqual(groups, {"user-group"})
        self.assertEqual(memberships, {("user-group", "PLone")})
        self.assertEqual(playlists, 1)
        self.assertEqual(version, database.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
