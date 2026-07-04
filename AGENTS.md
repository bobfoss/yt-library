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

## Coding Style & Naming Conventions

Prefer Python implementations and keep changes inside `yt_library_manager.py` unless a real module split is justified. Use 4-space indentation, type hints for new helper functions, and descriptive snake_case names. Keep comments rare and useful. Follow existing patterns for SQLite helpers, worker classes, and API route handling.

## Testing Guidelines

There is no formal test suite yet. For each change, at minimum run:

```powershell
python -m py_compile yt_library_manager.py
git diff --check
```

For schema, import, or worker changes, also run the relevant command against a local copy of `yt_playlists.sqlite3` and smoke test `/api/admin/status` and `/api/history/search?limit=1`.

## Commit & Pull Request Guidelines

Git history uses concise, imperative commit subjects such as `Clean up history storage schema` and `Import Takeout history from zip archives`. Keep commits focused and avoid staging personal data artifacts. Pull requests should summarize behavior changes, schema migrations, verification commands, and UI impact. Include screenshots for visible UI changes.

## Security & Configuration Tips

Cookie files, Takeout zips, SQLite databases, cached thumbnails, and logs can contain personal data. Treat them as local-only runtime state. Prefer passing cookies via explicit file paths, and never paste cookie values into commits, logs, or PR descriptions.
