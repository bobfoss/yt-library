from pathlib import Path

from yt_library import core


def migrated_connection(db_path: Path):
    core.migrate_database(db_path)
    return core.connect(db_path)
