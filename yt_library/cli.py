"""Command-line interface for YT Library Manager."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .config import config_int, config_path, ensure_config_file, load_config
from .core import (
    discover_current_playlists,
    import_history,
    import_playlists,
    import_takeout_playlists,
    migrate_database,
    recover_archivarix_thumbnails,
    recover_unavailable_videos,
    scan_hidden,
)
from .server import serve


def migrate(args: argparse.Namespace) -> None:
    ensure_config_file(args.config_data)
    migrate_database(Path(args.db))
    print(f"Migrated {args.db}")


def _preparse_config(argv: list[str] | None) -> tuple[list[str] | None, dict[str, Any]]:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", default=None)
    known, _ = config_parser.parse_known_args(argv)
    return argv, load_config(known.config)


def _attach_config(args: argparse.Namespace, config: dict[str, Any]) -> argparse.Namespace:
    args.config_data = config
    return args


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv, config = _preparse_config(argv)
    parser = argparse.ArgumentParser(description="Import YouTube library data and browse it locally.")
    parser.add_argument("--config", default=str(config["_config_path"]), help="Path to the JSON configuration file")
    subparsers = parser.add_subparsers(dest="command")

    import_parser = subparsers.add_parser("import", help="Import playlists and cache thumbnails")
    import_parser.add_argument("--db", default=str(config_path(config, "database")))
    import_parser.add_argument("--thumbs", default=str(config_path(config, "thumbnail_dir")))
    import_parser.add_argument("--cookies", default=str(config_path(config, "youtube_cookies")))
    import_parser.add_argument("--pockettube", required=True)
    import_parser.set_defaults(func=import_playlists)

    discover_parser = subparsers.add_parser(
        "discover-current",
        help="Discover current signed-in YouTube playlists and add ungrouped ones",
    )
    discover_parser.add_argument("--db", default=str(config_path(config, "database")))
    discover_parser.add_argument("--thumbs", default=str(config_path(config, "thumbnail_dir")))
    discover_parser.add_argument("--cookies", default=str(config_path(config, "youtube_cookies")))
    discover_parser.add_argument("--browse-id", default="FEplaylist_aggregation")
    discover_parser.add_argument("--group-key", default="youtube-ungrouped")
    discover_parser.add_argument("--group-name", default="Ungrouped / YouTube")
    discover_parser.add_argument("--include-system", action="store_true")
    discover_parser.set_defaults(func=discover_current_playlists)

    scan_parser = subparsers.add_parser("scan-hidden", help="Scan playlists for unavailable videos")
    scan_parser.add_argument("--db", default=str(config_path(config, "database")))
    scan_parser.add_argument("--cookies", default=str(config_path(config, "youtube_cookies")))
    scan_parser.add_argument("--limit", type=int, default=0, help="Scan only the first N playlists")
    scan_parser.set_defaults(func=scan_hidden)

    archivarix_parser = subparsers.add_parser(
        "archivarix-thumbnails",
        help="Search Archivarix for deleted video thumbnail candidates",
    )
    archivarix_parser.add_argument("--db", default=str(config_path(config, "database")))
    archivarix_parser.add_argument("--thumbs", default=str(config_path(config, "archivarix_thumbnail_dir")))
    archivarix_parser.add_argument("--limit", type=int, default=0, help="Search only the first N affected playlists")
    archivarix_parser.add_argument("--page-size", type=int, default=50)
    archivarix_parser.set_defaults(func=recover_archivarix_thumbnails)

    takeout_parser = subparsers.add_parser("import-takeout", help="Import current playlists from an extracted Takeout")
    takeout_parser.add_argument("--db", default=str(config_path(config, "database")))
    takeout_parser.add_argument("--takeout", default=str(config_path(config, "takeout_dir")))
    takeout_parser.set_defaults(func=import_takeout_playlists)

    history_parser = subparsers.add_parser("import-history", help="Import YouTube Takeout watch/search history")
    history_parser.add_argument("--db", default=str(config_path(config, "database")))
    history_parser.add_argument("--takeout", default=str(config_path(config, "takeout_dir")))
    history_parser.add_argument("--history-key", default="")
    history_parser.set_defaults(func=import_history)

    recover_missing_parser = subparsers.add_parser(
        "recover-missing-thumbnails",
        help="Recover Archivarix metadata for exact unavailable video IDs",
    )
    recover_missing_parser.add_argument("--db", default=str(config_path(config, "database")))
    recover_missing_parser.add_argument("--thumbs", default=str(config_path(config, "archivarix_thumbnail_dir")))
    recover_missing_parser.add_argument("--archivarix-cookies", default=str(config_path(config, "archivarix_cookies")))
    recover_missing_parser.add_argument("--video-id", default="")
    recover_missing_parser.add_argument("--limit", type=int, default=0)
    recover_missing_parser.add_argument("--only-missing", action="store_true")
    recover_missing_parser.add_argument("--likely-unavailable-only", action="store_true")
    recover_missing_parser.add_argument("--no-api", action="store_true", help="Only try direct Archivarix thumbnail URLs")
    recover_missing_parser.add_argument("--delay", type=float, default=3.0, help="Seconds to wait before each Archivarix API search")
    recover_missing_parser.add_argument("--refresh-metadata", action="store_true", help="Use Archivarix API even when a thumbnail is already cached")
    recover_missing_parser.set_defaults(func=recover_unavailable_videos)

    migrate_parser = subparsers.add_parser("migrate", help="Initialize the current database schema")
    migrate_parser.add_argument("--db", default=str(config_path(config, "database")))
    migrate_parser.set_defaults(func=migrate)

    serve_parser = subparsers.add_parser("serve", help="Serve the library manager")
    serve_parser.add_argument("--db", default=str(config_path(config, "database")))
    serve_parser.add_argument("--cookies", default=str(config_path(config, "youtube_cookies")))
    serve_parser.add_argument("--video-thumbs", default=str(config_path(config, "video_thumbnail_dir")))
    serve_parser.add_argument("--takeout", default=str(config_path(config, "takeout_dir")))
    serve_parser.add_argument("--host", default=str(config["host"]))
    serve_parser.add_argument("--port", type=int, default=config_int(config, "port"))
    serve_parser.set_defaults(func=serve)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args([*(argv or []), "serve"])
    _attach_config(args, config)
    if args.command not in {"serve", "migrate"}:
        migrate_database(Path(args.db))
    args.func(args)
    return 0
