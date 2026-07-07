# YT Library Manager

YT Library Manager is a local Python web app for browsing, enriching, and reconciling a personal YouTube library. It combines current playlist data, PocketTube organization, live YouTube history pulls, Takeout watch history, cached thumbnails, metadata fetches, and an admin dashboard into one local interface.

## Features

- Browse playlists, playlist videos, hidden or missing videos, and Takeout snapshots.
- Search watch history with paginated results across titles, channels, IDs, and fetched metadata descriptions.
- Import YouTube Takeout history zip files without extracting them first.
- Reconcile live YouTube history observations with precise Takeout watch timestamps.
- Cache video thumbnails and creator channel avatars locally.
- Monitor playlist scans, metadata fetches, history verification, and Takeout imports from the admin page.

## Project Layout

- `yt_library_manager.py` is a compatibility CLI shim; keep using it for commands.
- `yt_library/core.py` contains database migrations, importers, parsers, metadata fetchers, and reconciliation logic.
- `yt_library/server.py` contains HTTP routing and local API endpoints.
- `yt_library/workers.py` contains background worker orchestration.
- `yt_library/queries.py` contains read models for the library and history views.
- `yt_library/schema.sql` is the SQLite schema, loaded by `yt_library/schema.py`.
- `yt_library/templates/` contains the browser, history, and admin HTML.
- `requirements.txt` lists Python dependencies.
- `AGENTS.md` contains contributor guidance.
- Runtime data such as `yt_library.sqlite3`, cookie files, Takeout zip exports, thumbnail folders, and logs should stay local and uncommitted.

## Setup

```powershell
python -m pip install -r requirements.txt
```

Keep a Netscape-format YouTube cookie file in the project directory or pass its path with `--cookies`.

## Run Locally

```powershell
python yt_library_manager.py serve --host 0.0.0.0 --port 8765 --db yt_library.sqlite3 --cookies "YT cookies.txt" --video-thumbs video_thumbs --takeout .
```

Open:

- `http://127.0.0.1:8765/` for the library browser.
- `http://127.0.0.1:8765/history` for watch history search.
- `http://127.0.0.1:8765/admin` for worker controls and logs.

## Useful Commands

```powershell
$files = @("yt_library_manager.py") + (Get-ChildItem yt_library -Filter *.py | ForEach-Object { $_.FullName })
python -m py_compile @files
python yt_library_manager.py import-history --db yt_library.sqlite3 --takeout .
git diff --check
```

`import-history` scans the Takeout path for watch history zip files, imports watch rows, and rebuilds reconciliation.

## Data Notes

Takeout history is the authoritative source for exact watch times. Live YouTube history is useful for recent observations and ordering, but it may only provide date-level data. Rows seen without a YouTube watch date are marked `youtube_observed_only` so fetch time is not mistaken for watch time.

## Security

Cookie files, Takeout archives, SQLite databases, cached thumbnails, and logs can contain personal data. Do not commit them.
