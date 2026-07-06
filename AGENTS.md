# Repository Guidelines

## Project Structure & Module Organization

This repository is a small Python web app for browsing and enriching a personal YouTube library.

- `yt_library_manager.py` contains the HTTP server, SQLite schema/migrations, workers, importers, and HTML templates.
- `requirements.txt` lists Python dependencies, including `yt-dlp`.
- `yt_playlists.sqlite3`, cookie files, thumbnail folders, and Takeout zip exports are local runtime data and should not be committed.
- Common generated asset folders include `thumbs/`, `video_thumbs/`, and `archivarix_thumbs/`.

## Build, Test, and Development Commands

Use the repository root as the working directory.

```powershell
python -m pip install -r requirements.txt
python -m py_compile yt_library_manager.py
python yt_library_manager.py serve --host 0.0.0.0 --port 8765 --db yt_playlists.sqlite3 --cookies "YT cookies.txt" --video-thumbs video_thumbs --takeout .
python yt_library_manager.py import-history --db yt_playlists.sqlite3 --takeout .
```

- `py_compile` catches syntax errors without running workers.
- `serve` starts the local browser/admin UI.
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

## Coding Style & Naming Conventions

Prefer Python implementations and keep changes inside `yt_library_manager.py` unless a real module split is justified. Use 4-space indentation, type hints for new helper functions, and descriptive snake_case names. Keep comments rare and useful. Follow existing patterns for SQLite helpers, worker classes, and API route handling.

## Testing Guidelines

There is no formal test suite yet. For each change, at minimum run:

```powershell
python -m py_compile yt_library_manager.py
git diff --check
```

For schema, import, or worker changes, also run the relevant command against a local copy of `yt_playlists.sqlite3` and smoke test `/api/admin/status` and `/api/history/search?limit=1`.

Restart the local service when necessary, not automatically after every action. Restart after server code, served HTML/JS, config, schema/bootstrap, or worker behavior changes so the running app picks them up. A database-only update usually does not need a restart because API requests read SQLite fresh; verify with an endpoint instead.

## Data Modeling Notes

Keep raw source tables and display overlays separate. `playlist_videos` should reflect the current YouTube scan, while `playlist_video_reconciled` is the overlay that restores hidden/missing identities from Takeout and Archivarix evidence. Do not overwrite raw scan rows just to improve presentation.

For hidden or memory-holed playlist videos, keep uncertainty visible. Preserve badges that distinguish `Unavailable`, `restored from Takeout`, `Takeout candidate`, and Archivarix statuses such as `DELETED_FULL_META` or `NOT_FOUND`. Avoid forcing ambiguous hidden-slot matches; show candidates when counts or positions do not support a confident mapping.

## Commit & Pull Request Guidelines

Git history uses concise, imperative commit subjects such as `Clean up history storage schema` and `Import Takeout history from zip archives`. Keep commits focused and avoid staging personal data artifacts. Pull requests should summarize behavior changes, schema migrations, verification commands, and UI impact. Include screenshots for visible UI changes.

## Security & Configuration Tips

Cookie files, Takeout zips, SQLite databases, cached thumbnails, and logs can contain personal data. Treat them as local-only runtime state. Prefer passing cookies via explicit file paths, and never paste cookie values into commits, logs, or PR descriptions.
