# Channel Normalization Refactor Plan

Goal: normalize channel data into a dedicated `channels` table keyed by YouTube channel ID, while preserving raw source evidence where a channel ID is unknown.

## Schema Shape

Add a normalized channel table:

```sql
channels (
  channel_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  archivarix_channel_id TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0
)
```

Add `channel_id` references to `video_metadata`, `snapshot_video_recovery`, `playlist_videos`, `playlist_video_reconciled`, `youtube_history_occurrences`, `takeout_history_occurrences`, `history_reconciled`, and `archivarix_candidates`. Keep `archivarix_channel_id` on `snapshot_video_recovery`.

## Normalize vs Preserve

Fully normalize enriched tables: `video_metadata` and `snapshot_video_recovery` should stop owning channel title, URL, and avatar fields after migration.

Preserve raw source evidence in source tables: `playlist_videos`, `youtube_history_occurrences`, and `takeout_history_occurrences` may keep observed channel text and URL because they are useful when no `UC...` channel ID is known.

Derived tables should carry `channel_id` and keep raw fallback text only where needed for unresolved rows.

## Migration Strategy

On `connect()`:

- Create `channels`.
- Add missing `channel_id` columns.
- Backfill `channels` from existing duplicated channel fields in video metadata, Archivarix recovery, playlist scans, and history rows.
- Parse channel IDs from `/channel/UC...` URLs where possible.
- Rebuild `history_reconciled` and `playlist_video_reconciled`.

Prefer a safe two-step cleanup: migrate and update reads/writes first, then rebuild/drop legacy duplicated columns in a later cleanup commit if needed.

## Write Paths

Metadata worker:

- Extract YouTube channel ID from owner browse ID or channel URL.
- Upsert `channels`.
- Store `channel_id` in `video_metadata`.

Archivarix recovery:

- Capture `search:channel_resolved` and `search:channel_update`.
- Resolve numeric Archivarix channel IDs to YouTube `UC...` IDs using an in-memory cache for the recovery session.
- Upsert `channels` with title, URL, avatar, and Archivarix internal ID.
- Store `channel_id` and `archivarix_channel_id` in `snapshot_video_recovery`.

History and playlist imports:

- Parse and store `channel_id` where available.
- Preserve raw channel text as source evidence.

## Read/API Updates

`fetch_app_data()` and `history_search_data()` should join `channels` and prefer normalized channel title, URL, and avatar path for display. Fall back to raw source channel text only when no normalized channel row exists.

Metadata queue logic should check joined `channels.url` instead of old duplicated `video_metadata.channel_url`.

## Validation

Run:

```powershell
python -B -m py_compile yt_library_manager.py
git diff --check
```

Smoke checks:

- `PRAGMA table_info(channels)`
- channel row count
- known example `-R3PbSzyD9I` displays `Dark Brandon`
- `/api/data`
- `/api/history/search?limit=1`
- `/api/admin/status`

Restart the service after schema/API/server changes.
