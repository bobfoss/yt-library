# YT Library Manager

YT Library Manager is a local Python web app for browsing, enriching, and reconciling a personal YouTube library. It combines current playlist data, live YouTube history pulls, Takeout watch history, cached thumbnails, metadata and recovery fetches, and an admin dashboard into one local interface.

## Features

- Browse current playlists, canonical videos, and retained unavailable videos.
- Search watch history with paginated results across titles, channels, IDs, and fetched metadata descriptions.
- Import YouTube Takeout history zip files without extracting them first.
- Reconcile date-only live YouTube history observations with precise Takeout watch timestamps.
- Cache video thumbnails and creator channel avatars locally.
- Capture YouTube like/dislike reaction state during metadata fetches and expose a derived Liked videos view.
- Monitor and control the persistent queue for playlist scans, metadata fetches, history verification, and unavailable-video recovery from the admin page.

## Project Layout

- `yt_library_manager.py` is a compatibility CLI shim; keep using it for commands.
- `yt_library/core.py` contains schema bootstrap, importers, parsers, metadata fetchers, and reconciliation logic.
- `yt_library/server.py` contains HTTP routing and local API endpoints.
- `yt_library/workers.py` contains background worker orchestration.
- `yt_library/queries.py` contains read models for the library and history views.
- `yt_library/schema.sql` is the SQLite schema, loaded by `yt_library/schema.py`.
- `yt_library/templates/` contains the browser, history, and admin HTML.
- `tests/` contains the basic `unittest` suite for pure helpers, schema bootstrap, and read models.

The browser loads a small navigation bootstrap, then requests playlists, videos, channels, details, search results, and history as separate server-paginated read models. It does not preload the complete video and channel catalog.
- `requirements.txt` lists Python dependencies.
- `yt_library.config.json` is the local runtime configuration file, generated on first setup or serve.
- `AGENTS.md` contains contributor guidance.
- Runtime data such as `yt_library.sqlite3`, cookie files, Takeout zip exports, thumbnail folders, and logs should stay local and uncommitted.

## Setup

```powershell
python -m pip install -r requirements.txt
```

Keep a Netscape-format YouTube cookie file in the project directory or pass its path with `--cookies`.

## Run Locally

```powershell
python yt_library_manager.py
```

With no command, the app creates `yt_library.config.json` if needed, initializes
or migrates `yt_library.sqlite3`, and serves the local UI. Defaults can be edited
in the generated config file:

```json
{
  "database": "yt_library.sqlite3",
  "youtube_cookies": "YT cookies.txt",
  "archivarix_cookies": "archivarix.net cookies.txt",
  "thumbnail_dir": "thumbs",
  "archivarix_thumbnail_dir": "archivarix_thumbs",
  "video_thumbnail_dir": "video_thumbs",
  "takeout_dir": "takeout",
  "host": "127.0.0.1",
  "port": 8765,
  "display_timezone": "",
  "youtube_request_interval_seconds": 0.5,
  "youtube_max_in_flight": 10,
  "archivarix_request_interval_seconds": 3.0,
  "archivarix_max_in_flight": 1
}
```

Existing command-line options still work as one-off overrides, and `migrate`
remains available for explicit setup or upgrade runs.

The default host binds only to the local loopback interface. To expose the app
through Tailscale without binding other interfaces, set `host` to the machine's
Tailscale IPv4 address.
If `display_timezone` is empty, the browser detects an IANA timezone on first
load and saves it to the config file.
The request interval settings control how often each site's next task may launch.
The matching `max_in_flight` settings cap concurrent tasks; long Archivarix
lookups therefore do not delay the YouTube launch cadence.

Open:

- `http://127.0.0.1:8765/` for the library browser.
- `http://127.0.0.1:8765/history` for watch history search.
- `http://127.0.0.1:8765/admin` for worker controls and logs.

## Useful Commands

```powershell
$files = @("yt_library_manager.py") + (Get-ChildItem yt_library -Filter *.py | ForEach-Object { $_.FullName }) + (Get-ChildItem tests -Filter *.py | ForEach-Object { $_.FullName })
python -m py_compile @files
python -m unittest discover -s tests -v
python yt_library_manager.py migrate
python yt_library_manager.py import-history
git diff --check
```

`import-history` uses only the newest Takeout zip in the selected path. It imports current playlists, watch events, and subscriptions, then reconciles exact Takeout times with live-history ordinals. Older exports are not retained as metadata history.

## Testing

The test suite uses the Python standard library `unittest` runner, so there is no separate test dependency. Current coverage focuses on stable, local behavior: date/time normalization, reaction extraction, Takeout watch-history parsing, fresh SQLite schema bootstrap, bootstrap/list/detail read models, and omni/history search filtering, deduplication, sorting, and paging. Tests must not use real cookies, network requests, or personal runtime databases.

## Data Notes

Takeout history is the authoritative source for exact watch times. Live YouTube history is useful for recent observations and ordering, but it may only provide date-level data. Reconciled history rows use compact `source_type`, `match_type`, and `time_quality` values so fetch time is not mistaken for watch time.

The database stores canonical video metadata once in `videos`; playlist membership and history events link to that entity. Metadata revisions are intentionally discarded, except that the last useful state is retained when a video becomes unavailable. Exact timestamps use ISO 8601 UTC. The configured display timezone lives in `yt_library.config.json`; the UI can update it from Admin.

## Security

Cookie files, Takeout archives, SQLite databases, cached thumbnails, and logs can contain personal data. Do not commit them.
