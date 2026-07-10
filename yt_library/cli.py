"""Command-line interface for YT Library Manager."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .core import (
    ARCHIVARIX_COOKIE_FILE,
    COOKIE_FILE,
    DEFAULT_ARCHIVARIX_THUMB_DIR,
    DEFAULT_DB,
    DEFAULT_THUMB_DIR,
    DEFAULT_VIDEO_THUMB_DIR,
    POCKETTUBE_EXPORT,
    ROOT,
    TAKEOUT_DIR,
    discover_current_playlists,
    import_history,
    import_playlists,
    import_takeout_snapshot,
    migrate_database,
    recover_archivarix_thumbnails,
    recover_snapshot_missing,
    scan_hidden,
)
from .server import serve


def migrate(args: argparse.Namespace) -> None:
    migrate_database(Path(args.db))
    print(f"Migrated {args.db}")

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import YouTube library data and browse it locally.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="Import playlists and cache thumbnails")
    import_parser.add_argument("--db", default=str(DEFAULT_DB))
    import_parser.add_argument("--thumbs", default=str(DEFAULT_THUMB_DIR))
    import_parser.add_argument("--cookies", default=str(COOKIE_FILE))
    import_parser.add_argument("--pockettube", default=str(POCKETTUBE_EXPORT))
    import_parser.set_defaults(func=import_playlists)

    discover_parser = subparsers.add_parser(
        "discover-current",
        help="Discover current signed-in YouTube playlists and add ungrouped ones",
    )
    discover_parser.add_argument("--db", default=str(DEFAULT_DB))
    discover_parser.add_argument("--thumbs", default=str(DEFAULT_THUMB_DIR))
    discover_parser.add_argument("--cookies", default=str(COOKIE_FILE))
    discover_parser.add_argument("--browse-id", default="FEplaylist_aggregation")
    discover_parser.add_argument("--group-key", default="youtube-ungrouped")
    discover_parser.add_argument("--group-name", default="Ungrouped / YouTube")
    discover_parser.add_argument("--include-system", action="store_true")
    discover_parser.set_defaults(func=discover_current_playlists)

    scan_parser = subparsers.add_parser("scan-hidden", help="Scan playlists for unavailable videos")
    scan_parser.add_argument("--db", default=str(DEFAULT_DB))
    scan_parser.add_argument("--cookies", default=str(COOKIE_FILE))
    scan_parser.add_argument("--limit", type=int, default=0, help="Scan only the first N playlists")
    scan_parser.set_defaults(func=scan_hidden)

    archivarix_parser = subparsers.add_parser(
        "archivarix-thumbnails",
        help="Search Archivarix for deleted video thumbnail candidates",
    )
    archivarix_parser.add_argument("--db", default=str(DEFAULT_DB))
    archivarix_parser.add_argument("--thumbs", default=str(DEFAULT_ARCHIVARIX_THUMB_DIR))
    archivarix_parser.add_argument("--limit", type=int, default=0, help="Search only the first N affected playlists")
    archivarix_parser.add_argument("--page-size", type=int, default=50)
    archivarix_parser.set_defaults(func=recover_archivarix_thumbnails)

    takeout_parser = subparsers.add_parser("import-takeout", help="Import a Google Takeout YouTube snapshot")
    takeout_parser.add_argument("--db", default=str(DEFAULT_DB))
    takeout_parser.add_argument("--takeout", default=str(TAKEOUT_DIR))
    takeout_parser.add_argument("--snapshot-key", default="takeout-2025-11-09")
    takeout_parser.add_argument("--label", default="Takeout 2025-11-09")
    takeout_parser.set_defaults(func=import_takeout_snapshot)

    history_parser = subparsers.add_parser("import-history", help="Import YouTube Takeout watch/search history")
    history_parser.add_argument("--db", default=str(DEFAULT_DB))
    history_parser.add_argument("--takeout", default=str(ROOT))
    history_parser.add_argument("--history-key", default="")
    history_parser.set_defaults(func=import_history)

    recover_missing_parser = subparsers.add_parser(
        "recover-missing-thumbnails",
        help="Recover Archivarix thumbnails for exact missing snapshot video IDs",
    )
    recover_missing_parser.add_argument("--db", default=str(DEFAULT_DB))
    recover_missing_parser.add_argument("--thumbs", default=str(DEFAULT_ARCHIVARIX_THUMB_DIR))
    recover_missing_parser.add_argument("--snapshot-key", default="takeout-2025-11-09")
    recover_missing_parser.add_argument("--archivarix-cookies", default=str(ARCHIVARIX_COOKIE_FILE))
    recover_missing_parser.add_argument("--video-id", default="")
    recover_missing_parser.add_argument("--limit", type=int, default=0)
    recover_missing_parser.add_argument("--only-missing", action="store_true")
    recover_missing_parser.add_argument("--likely-hidden-only", action="store_true")
    recover_missing_parser.add_argument("--no-api", action="store_true", help="Only try direct Archivarix thumbnail URLs")
    recover_missing_parser.add_argument("--delay", type=float, default=3.0, help="Seconds to wait before each Archivarix API search")
    recover_missing_parser.add_argument("--refresh-metadata", action="store_true", help="Use Archivarix API even when a thumbnail is already cached")
    recover_missing_parser.set_defaults(func=recover_snapshot_missing)

    migrate_parser = subparsers.add_parser("migrate", help="Apply database schema migrations and repairs")
    migrate_parser.add_argument("--db", default=str(DEFAULT_DB))
    migrate_parser.set_defaults(func=migrate)

    serve_parser = subparsers.add_parser("serve", help="Serve the library manager")
    serve_parser.add_argument("--db", default=str(DEFAULT_DB))
    serve_parser.add_argument("--cookies", default=str(COOKIE_FILE))
    serve_parser.add_argument("--video-thumbs", default=str(DEFAULT_VIDEO_THUMB_DIR))
    serve_parser.add_argument("--takeout", default=str(ROOT))
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.set_defaults(func=serve)

    args = parser.parse_args(argv)
    if args.command not in {"serve", "migrate"}:
        migrate_database(Path(args.db))
    args.func(args)
    return 0
