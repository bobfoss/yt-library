# YT Library Manager

YT Library Manager is a local Python web app for browsing, enriching, and reconciling a personal YouTube library. It combines current playlist data, live YouTube history pulls, Takeout watch history, cached thumbnails, metadata and recovery fetches, and an admin dashboard into one local interface.

## Features

- Browse current playlists, canonical videos, and retained unavailable videos.
- Search watch history with paginated results across titles, channels, IDs, and fetched metadata descriptions.
- Import YouTube Takeout history zip files without extracting them first.
- Collect exact YouTube watch and channel-subscription timestamps from Google My Activity directly into SQLite.
- Collect subscription, owned-playlist creation, and playlist-item-added timestamps through the YouTube Data API.
- Reconcile date-only live YouTube history observations with precise Takeout watch timestamps.
- Cache video thumbnails and creator channel avatars locally.
- Capture YouTube like/dislike reaction state during metadata fetches and expose a derived Liked videos view.
- Monitor and control the persistent queue for playlist scans, metadata fetches, history verification, and unavailable-video recovery from the admin page.

## Project Layout

- `yt_library_manager.py` is a compatibility CLI shim; keep using it for commands.
- `yt_library/core.py` contains importers, parsers, metadata fetchers, and reconciliation logic.
- `yt_library/database.py` contains SQLite connection, schema bootstrap, and migrations.
- `yt_library/server.py` contains HTTP routing and local API endpoints.
- `yt_library/workers.py` contains background worker orchestration.
- `yt_library/queries.py` contains read models for the library and history views.
- `yt_library/schema.sql` is the SQLite schema, loaded by `yt_library/schema.py`.
- `yt_library/templates/` contains the browser and admin HTML plus their served JavaScript assets.
- `tests/` contains focused `unittest` modules for helpers, schema, configuration, server routes, workers, templates, JavaScript behavior, and read models.

The browser loads a small navigation bootstrap, then requests playlists, videos, channels, details, search results, and history as separate server-paginated read models. It does not preload the complete video and channel catalog.
- `requirements.txt` lists runtime dependencies; `requirements-dev.txt` adds development-only static analysis.
- `yt_library.config.json` is the local runtime configuration file, generated on first setup or serve.
- Optional plugins are installed separately and activated explicitly through
  the `plugins` object in `yt_library.config.json`; disabled or missing plugins
  must not prevent the base service from starting.
- `AGENTS.md` contains contributor guidance.
- Runtime data such as `yt_library.sqlite3`, cookie files, Takeout zip exports, thumbnail folders, and logs should stay local and uncommitted.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the application from this environment. The application imports the
`yt_dlp` Python module, so a standalone `yt-dlp.exe` on `PATH` is not a
substitute for installing `requirements.txt` into the active interpreter.

For development, install the development requirements and run Ruff alongside
the standard test suite:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
```

Keep a Netscape-format YouTube cookie file in the project directory or pass its path with `--cookies`.
Authenticated yt-dlp calls use a temporary copy so yt-dlp cannot rewrite the
configured cookie export. Install Deno for yt-dlp's recommended JavaScript
challenge runtime; the `yt-dlp[default]` dependency includes the matching EJS
challenge scripts.

## Run Locally

```powershell
.\.venv\Scripts\python.exe yt_library_manager.py
```

With no command, the app creates `yt_library.config.json` if needed, initializes
or migrates `yt_library.sqlite3`, and serves the local UI. Defaults can be edited
in the generated config file:

```json
{
  "database": "yt_library.sqlite3",
  "youtube_cookies": "yt_cookies.txt",
  "my_activity_cookies": "my_activity_cookies.txt",
  "archivarix_cookies": "archivarix_cookies.txt",
  "youtube_oauth_client_secrets": "youtube_oauth_client_secret.json",
  "youtube_oauth_token": "youtube_oauth_token.json",
  "thumbnail_dir": "thumbs",
  "archivarix_thumbnail_dir": "archivarix_thumbs",
  "video_thumbnail_dir": "video_thumbs",
  "takeout_dir": "takeout",
  "host": "127.0.0.1",
  "port": 8765,
  "display_timezone": "",
  "search_card_layout": "grid",
  "history_card_layout": "compact",
  "sort_preferences": {},
  "search_filter_tree_expanded": [
    "kind:videos",
    "kind:playlists",
    "kind:channels"
  ],
  "update_daily": false,
  "update_time": "03:00",
  "use_proxy": false,
  "proxy": "",
  "dispatch_mode": "delay",
  "job_dispatch_delay_seconds": 5.0,
  "request_delay_min_seconds": 6.0,
  "request_delay_max_seconds": 10.0,
  "youtube_max_in_flight": 10,
  "archivarix_max_in_flight": 1,
  "archivarix_request_timeout_seconds": 15.0,
  "archivarix_stream_timeout_seconds": 30.0,
  "archivarix_retry_attempts": 3,
  "archivarix_retry_backoff_seconds": 2.0
}
```

Existing command-line options still work as one-off overrides, and `migrate`
remains available for explicit setup or upgrade runs.

Existing databases are supported through versioned migrations in
`yt_library/database.py`. Schema changes must preserve the fresh-install shape
in `schema.sql` and provide an upgrade path for supported prior versions.

The default host binds only to the local loopback interface. To expose the app
through Tailscale without binding other interfaces, set `host` to the machine's
Tailscale IPv4 address.
If `display_timezone` is empty, the browser detects an IANA timezone on first
load and saves it to the config file.
The search and history card selectors save `search_card_layout` and
`history_card_layout` immediately, without changing the current URL.
Sort selectors save independent preferences for unscoped search, each sidebar
preset, and playlist-detail video lists in `sort_preferences`.
The Admin **Update** action incrementally discovers new playlists, fetches recent
history and Liked videos, refreshes due playlist memberships, and enriches
metadata that has never been fetched. `update_daily` and `update_time` attach
the daily schedule to that complete Update workflow; the configured time uses
the display timezone. Existing `history_fetch_daily` and `history_fetch_time`
settings are migrated when an older config is loaded.
Set `use_proxy` to `true` and `proxy` to a URL such as
`socks5h://127.0.0.1:1080` to route every outbound YouTube and Archivarix page,
API, stream, thumbnail, and yt-dlp request through a SOCKS5 proxy. Use `socks5h`
for proxy-side DNS resolution. The Admin Advanced panel has tabs for controls
and the YouTube, Google, and Archivarix cookie files. Cookie updates are
validated as Netscape exports and saved atomically; existing cookie values are
never sent back to the browser. Changing either proxy setting restarts the
service so all new network clients use the saved configuration.
`dispatch_mode` selects one of two queue policies. In `delay` mode,
`job_dispatch_delay_seconds` spaces every worker launch, regardless of worker
type. In `throttle` mode, jobs launch without that dispatch delay and every
direct YouTube, Archivarix, and yt-dlp request passes through one global
randomized request gate using `request_delay_min_seconds` and
`request_delay_max_seconds`.
The domain-based `youtube_max_in_flight` and `archivarix_max_in_flight` settings
still cap concurrent jobs. Lowering a cap does not stop active jobs; it prevents
new jobs from launching until the active count falls below the new limit.
The Admin worker queue exposes the two policies as stacked **Dispatch mode**
radio choices, along with both in-flight caps. Changes are saved to the config
file and update an active dispatcher and request pacer without restarting the
service.
When YouTube rejects an authenticated request, the worker stops its YouTube task
group and records one cached, low-volume yt-dlp authentication probe in the debug
log. Public-only yt-dlp clients are diagnostic only and are not used to complete
authenticated metadata tasks that require private access or reaction state.
Legacy per-site dispatch and jitter settings are migrated automatically: the
largest prior site delay becomes the global job delay, and the largest prior
minimum and maximum become the shared throttle range.

Open:

- `http://127.0.0.1:8765/` for the library browser.
- `http://127.0.0.1:8765/#view=history` for watch history.
- `http://127.0.0.1:8765/admin` for worker controls and logs.

## Useful Commands

```powershell
$files = @("yt_library_manager.py") + (Get-ChildItem yt_library -Filter *.py | ForEach-Object { $_.FullName }) + (Get-ChildItem tests -Filter *.py | ForEach-Object { $_.FullName })
.\.venv\Scripts\python.exe -m py_compile @files
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe yt_library_manager.py migrate
.\.venv\Scripts\python.exe yt_library_manager.py import-history
.\.venv\Scripts\python.exe yt_library_manager.py collect-my-activity
.\.venv\Scripts\python.exe yt_library_manager.py authorize-youtube-data-api
.\.venv\Scripts\python.exe yt_library_manager.py collect-youtube-data-api
git diff --check
```

`import-history` reads all Takeout zips in the selected path, skips duplicate
watch events, imports current playlists and subscriptions, then reconciles exact
Takeout times with live-history ordinals.

### Personal activity and library dates

`collect-my-activity` reads the structured bootstrap data behind the signed-in
[YouTube My Activity page](https://myactivity.google.com/product/youtube). Unlike
the visible page, which rounds times to the minute, the bootstrap records include
an exact UTC timestamp. Google can expose multiple opaque tokenized records for
one occurrence, so the collector identifies a watch by video ID plus its exact
timestamp. The same stream includes timestamped `Subscribed to` events when
Google exposes them.

Export a separate Netscape-format cookie file that includes `google.com` cookies
to `my_activity_cookies.txt`, then run:

```powershell
python yt_library_manager.py collect-my-activity
```

The command writes normalized evidence directly to dedicated SQLite source
tables and projects watch events into canonical history and subscription events
onto channels. It stores a hashed exact-occurrence identity and is idempotent,
including when Google repeats the occurrence under different opaque tokens. By
default it fetches at most 25 pages and reports whether the collection overlaps
stored events. A non-overlapping run is preserved but logged as a possible
coverage gap.

For a bounded historical backfill, follow Google's structured continuation pages:

```powershell
python yt_library_manager.py collect-my-activity --max-pages 10
```

`--max-pages` includes the initial page. The command validates every continuation
response and rejects token loops and pages that make no progress. It reports
whether older activity is still available; increase the bound until it reports
that it reached the end.
Each invocation starts at the newest page, so progressively larger bounds refetch
and deduplicate the already-seen prefix. The collector keeps the stable exact
occurrence ID as source identity, replaces matching YouTube date-only occurrences
with the exact event
while retaining their ordinal and progress fields, and merges a matching Takeout
event by video ID and UTC second. This prevents a later Takeout import from
duplicating the same watch. Retain Takeout until the My Activity continuation
backfill reaches the end of the available history.

A saved signed-in page can be tested without a network request using
`--html path\to\my-activity.html`; saved HTML supports only one page.

The YouTube Data API supplies the authoritative current subscription snapshot
and its subscription `publishedAt` dates, plus owned-playlist creation dates and
playlist-item-added dates. Enable YouTube Data API v3 in a Google Cloud project,
create an OAuth client for a desktop application, and save the downloaded client
file as `youtube_oauth_client_secret.json`. Then authorize the read-only grant:

```powershell
python yt_library_manager.py authorize-youtube-data-api
python yt_library_manager.py collect-youtube-data-api
```

The first command opens Google's OAuth consent flow and saves the refreshable
token locally. Both account sources are optional during normal Update scans:
missing credentials produce an informational worker log; expired or rejected
credentials produce a warning and do not halt the rest of the queue. Channel
date sorting uses the My Activity subscription date as the gold standard, then
the Data API subscription date, then a first watch observed directly in history.
Metadata and playlist backfills do not assign channel first-seen dates.

## Testing

The test suite uses the Python standard library `unittest` runner, so there is no separate test dependency. Current coverage focuses on stable, local behavior: date/time normalization, reaction extraction, Takeout and My Activity watch-history parsing and reconciliation, fresh SQLite schema bootstrap, bootstrap/list/detail read models, and omni/history search filtering, deduplication, sorting, and paging. Tests must not use real cookies, network requests, or personal runtime databases.

## Data Notes

Takeout remains the historical seed for exact watch times until a My Activity
continuation backfill reaches the end. My Activity is the prospective exact-time
source. Live YouTube history is useful for recent observations and ordering, but
it may only provide date-level data. Reconciled history rows use compact
`source_type`, `match_type`, and `time_quality` values so fetch time is not
mistaken for watch time.

Recent history fetches use 200-entry batches and stop after two consecutive complete days have the same per-video occurrence counts as the prior YouTube observation. Full history verification retains 1,000-entry batches and scans to the end. A live watch occurrence is reused by video ID, local watch date, and occurrence number within that video/day group; `youtube_ordinal` records current display order and is not event identity.

The database stores canonical video metadata once in `videos`; playlist membership and history events link to that entity. Metadata revisions are intentionally discarded, except that the last useful state is retained when a video becomes unavailable. Exact timestamps use ISO 8601 UTC. The configured display timezone lives in `yt_library.config.json`; the UI can update it from Admin.

## Optional plugins

YT Library discovers separately installed plugins through the
`yt_library.plugins` Python entry-point group, but loads only plugins explicitly
enabled in local configuration. Plugin API routes are namespaced below
`/api/plugins/{plugin_id}/`; discovery, startup, status, and request failures are
contained so the base application remains usable.

Plugin API version 2 adds optional worker processes without giving plugins
direct access to the YT Library database. A plugin declares validated process
metadata, plans bounded tasks from a read-only library-video projection, and
runs one task at a time through the common persistent queue. YT Library owns
the operational run rows and logs, applies global YouTube or Archivarix
capacity limits, retains interrupted queue items, and exposes generic Basic or
Advanced Admin buttons. Plugins may opt into the `library_initialize` and
`library_update` lifecycle hooks; no hook runs unless the plugin declares it.
Admin actions use
`POST /api/admin/plugins/{plugin_id}/processes/{worker_id}/enqueue`, and plugin
logs appear in the common log view under a `plugin:{plugin_id}` source.
Process-specific query parameters are forwarded to the plugin planner;
repeated parameter names remain ordered value lists.
Processes may also declare validated `adminActions`. An action can remain in
its plugin workstream or use the generic `videos` placement, and can describe
required text inputs whose values are sent as process query parameters. YTL
renders and submits these controls without knowing their plugin semantics.
Browser plugins receive a generic `host.ui.searchHighlight` helper. Its
`textHtml` method safely highlights a literal query in plain text, while
`excerptHtml` centers a word-bounded excerpt on the first literal match and
`snippetHtml` escapes a server-generated snippet while admitting only its
exact `<mark>` delimiters. Highlight styling and HTML safety therefore remain
owned by YT Library rather than individual plugins.

The first plugin is the sibling YT Subtitles project. A local activation uses:

```json
{
  "plugins": {
    "subtitles": {
      "enabled": true,
      "config": "../YT Subtitles/yt_subtitles.config.json"
    }
  }
}
```

YT Subtitles owns its separate database schema. YT Library never writes that
database and joins plugin results to canonical videos only by video ID.
When the plugin is enabled and ready, it contributes a Subtitles option under
**Search in** and a subtitle-presence facet under Videos. Search-field state and
presence filtering are independent: transcript matches join title and
description matches by video ID without requiring the presence facet to narrow
the result set. Subtitle text remains on namespaced plugin routes; the host
receives only match IDs and read-only projections. Plugin-owned browser assets
are loaded from the generic `/plugins/<id>/assets/` namespace, and the core
templates contain only generic registration, search-field, video-facet,
search-provider, and detail-panel extension hooks.

## Security

Cookie files, Takeout archives, SQLite databases, cached thumbnails, and logs can contain personal data. Do not commit them.
