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

- `yt_library_manager.py` is the main application, including the HTTP server, SQLite schema, workers, importers, and HTML templates.
- `requirements.txt` lists Python dependencies.
- `AGENTS.md` contains contributor guidance.
- Runtime data such as `yt_playlists.sqlite3`, cookie files, Takeout zip exports, thumbnail folders, and logs should stay local and uncommitted.

## Setup

```powershell
python -m pip install -r requirements.txt
```

Keep a Netscape-format YouTube cookie file in the project directory or pass its path with `--cookies`.

## Run Locally

```powershell
python yt_library_manager.py serve --host 0.0.0.0 --port 8765 --db yt_playlists.sqlite3 --cookies "YT cookies.txt" --video-thumbs video_thumbs --takeout .
```

Open:

- `http://127.0.0.1:8765/` for the library browser.
- `http://127.0.0.1:8765/history` for watch history search.
- `http://127.0.0.1:8765/admin` for worker controls and logs.

## Useful Commands

```powershell
python -m py_compile yt_library_manager.py
python yt_library_manager.py import-history --db yt_playlists.sqlite3 --takeout .
git diff --check
```

`import-history` scans the Takeout path for watch history zip files, imports watch rows, and rebuilds reconciliation.

## Data Notes

Takeout history is the authoritative source for exact watch times. Live YouTube history is useful for recent observations and ordering, but it may only provide date-level data. Rows seen without a YouTube watch date are marked `youtube_observed_only` so fetch time is not mistaken for watch time.

## Security

Cookie files, Takeout archives, SQLite databases, cached thumbnails, and logs can contain personal data. Do not commit them.
