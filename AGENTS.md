# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python web app for browsing, enriching, and reconciling a personal YouTube library.

- `yt_library_manager.py` is the compatibility CLI shim; keep existing commands routed through it.
- `yt_library/core.py` contains database migrations, importers, parsers, metadata fetchers, and reconciliation logic.
- `yt_library/server.py` contains HTTP routing and local API endpoints.
- `yt_library/workers.py` contains background worker orchestration.
- `yt_library/queries.py` contains read models for the browser and history search.
- `yt_library/schema.sql` is the SQLite schema, loaded by `yt_library/schema.py`.
- `yt_library/templates/` contains the browser, history, and admin HTML.
- `tests/` contains the standard-library `unittest` suite for helpers, schema bootstrap, and read models.
- `requirements.txt` lists Python dependencies, including `yt-dlp`.
- `yt_library.sqlite3`, cookie files, thumbnail folders, and Takeout zip exports are local runtime data and should not be committed.
- Common generated asset folders include `thumbs/`, `video_thumbs/`, and `archivarix_thumbs/`.

## Build, Test, and Development Commands

Use the repository root as the working directory.

```powershell
python -m pip install -r requirements.txt
$files = @("yt_library_manager.py") + (Get-ChildItem yt_library -Filter *.py | ForEach-Object { $_.FullName }) + (Get-ChildItem tests -Filter *.py | ForEach-Object { $_.FullName })
python -m py_compile @files
python -m unittest discover -s tests -v
python yt_library_manager.py migrate --db yt_library.sqlite3
python yt_library_manager.py serve --host 0.0.0.0 --port 8765 --db yt_library.sqlite3 --cookies "YT cookies.txt" --video-thumbs video_thumbs --takeout .
python yt_library_manager.py import-history --db yt_library.sqlite3 --takeout .
```

- `py_compile` catches syntax errors without running workers.
- `migrate` applies schema migrations and one-time data repairs. Run it deliberately before starting a server after schema changes; normal connections and requests never run migrations.
- `serve` starts the local browser/admin UI and requires an already-migrated database.
- `import-history` imports Takeout watch history zips from the selected path and rebuilds reconciliation.

This repo is normally operated from PowerShell. Avoid Bash-only syntax such as `python - <<'PY'` here-docs. Prefer PowerShell-safe forms like `python -c "..."`, checked-in or temporary helper scripts when warranted, or explicit PowerShell here-strings piped intentionally. This project does not use `ENVIRONMENT.md`.

## Operational Notes

Treat video IDs, titles, and URLs as shell-hostile strings. YouTube IDs can start with `-`, and titles or copied values can contain leading spaces. When passing a dash-leading value to argparse, use the equals form so it cannot be parsed as an option:

```powershell
python yt_library_manager.py recover-missing-thumbnails --video-id=-R3PbSzyD9I
```

For ad hoc Python probes in PowerShell, avoid Bash here-doc syntax and prefer piping a PowerShell here-string into the bundled Python. When printing web/API payloads, force UTF-8 output to avoid Windows console encoding failures:

```powershell
$code = @'
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
'@
$code | & "C:\Users\michael.keenan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -B -
```

When using `Start-Process`, pass a single quoted argument string or otherwise verify paths with spaces remain intact; cookie files such as `"YT cookies.txt"` must not be split into separate arguments.

SQLite can be held open by long-running ad hoc probes. If migrations fail with `database is locked`, inspect local `python.exe`/`pwsh.exe` processes for stale diagnostic scripts before changing application code. Stop only the stale probe, not the active server, unless a server restart is needed.

Metadata workers materialize their queue when a run starts. Queue priority changes, new source labels, or extractor fixes require stopping/restarting the metadata worker or restarting the service before the running worker will use the new ordering. A server restart interrupts active in-process workers.

YouTube creator avatars may appear in newer watch-page data under `avatarViewModel.image.sources`, not only older `videoOwnerRenderer` or `channelThumbnailWithLinkRenderer` thumbnail shapes. Keep the channel avatar extractor broad enough to handle both.

### Current YouTube Playlist UI

YouTube's current playlist page uses `pageHeaderRenderer.content.pageHeaderViewModel.metadata.contentMetadataViewModel.metadataRows` for header facts. The row can contain `Playlist`, the visibility label, the authoritative displayed count (for example `150 videos`), and the view count. Do not rely only on older `playlistHeaderRenderer` fields; parse this newer shape when refreshing playlist metadata or validating scan completeness.

Playlist pages may contain login-related strings such as `ServiceLogin` even when the provided cookie is accepted and the private playlist header is visible. Treat parsed header metadata as authoritative: if title, visibility, and video count are present, do not classify the page as signed out just because login markers are also present. Use login markers to explain failures only when the header count is missing.

The initial playlist page commonly exposes only the first 100 `playlistVideoRenderer` entries. Its continuation surface is currently fragile: the generic JSON continuation request can be treated as logged out, while the authenticated `youtubei` request may return only a logged-in response shell with no playlist entries. Adding click-tracking context alone did not resolve that behavior. Treat the displayed header count as the completeness guard and reject an extractor result that is short of it rather than replacing a fuller existing scan.

`yt-dlp` is useful but can return a different or transient playlist membership/count from the web page. It has returned a short partial list in one scan and a fuller list later for the same playlist. Compare its row count to the live page header before saving; preserve the prior raw scan on a short result. Keep the source/count evidence in worker logs so discrepancies are diagnosable.

For playlists owned by others, YouTube can report a larger displayed header count than the rows exposed to the current account. Do not synthesize unavailable rows from that count gap alone. Save the best nonzero visible row set, keep the displayed header count as reported playlist metadata, and log the exposed/reported mismatch. Only create no-ID unavailable placeholder rows when YouTube explicitly exposes an unavailable row or hidden-video notice.

## Coding Style & Naming Conventions

Prefer Python implementations and put changes in the module that owns the behavior. Use 4-space indentation, type hints for new helper functions, and descriptive snake_case names. Keep comments rare and useful. Follow existing patterns for SQLite helpers, worker classes, API route handling, and template edits.

## Testing Guidelines

The formal test suite uses Python's standard `unittest` runner. For each change, at minimum run:

```powershell
$files = @("yt_library_manager.py") + (Get-ChildItem yt_library -Filter *.py | ForEach-Object { $_.FullName }) + (Get-ChildItem tests -Filter *.py | ForEach-Object { $_.FullName })
python -m py_compile @files
python -m unittest discover -s tests -v
git diff --check
```

Current tests cover pure helpers, Takeout watch-history parsing, temporary SQLite schema bootstrap, and history search read models. Keep tests local-only: do not require real cookies, network access, personal Takeout zips, or the live `yt_library.sqlite3`.

For schema, import, or worker changes, also run the relevant command against a local copy of `yt_library.sqlite3` and smoke test `/api/admin/status` and `/api/history/search?limit=1`.

Restart the local service when necessary, not automatically after every action. Restart after server code, served HTML/JS, config, schema/bootstrap, or worker behavior changes so the running app picks them up. A database-only update usually does not need a restart because API requests read SQLite fresh; verify with an endpoint instead.

## Data Modeling Notes

Keep raw source tables and display overlays separate. `playlist_videos` should reflect the current YouTube scan, while `playlist_video_reconciled` is the overlay that restores hidden/missing identities from Takeout and Archivarix evidence. Do not overwrite raw scan rows just to improve presentation.

For hidden or memory-holed playlist videos, keep uncertainty visible. Preserve badges that distinguish `Unavailable`, `restored from Takeout`, `Takeout candidate`, and Archivarix statuses such as `DELETED_FULL_META` or `NOT_FOUND`. Avoid forcing ambiguous hidden-slot matches; show candidates when counts or positions do not support a confident mapping.

Video like/dislike state is stored on `video_metadata.reaction` as a compact per-video value: `L`, `D`, or empty. The `Liked videos` browser view is derived from metadata instead of being stored as a normal playlist.

## Commit & Pull Request Guidelines

Git history uses concise, imperative commit subjects such as `Clean up history storage schema` and `Import Takeout history from zip archives`. Every commit should also include a substantive body covering behavior changes, relevant schema or operational impact, and verification performed. Keep commits focused and avoid staging personal data artifacts. Pull requests should summarize behavior changes, schema migrations, verification commands, and UI impact. Include screenshots for visible UI changes.

## Security & Configuration Tips

Cookie files, Takeout zips, SQLite databases, cached thumbnails, and logs can contain personal data. Treat them as local-only runtime state. Prefer passing cookies via explicit file paths, and never paste cookie values into commits, logs, or PR descriptions.
