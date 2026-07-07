# Cleanup Findings

This document tracks code and schema cleanup opportunities that match the recent `playlist_video_reconciled` pattern: store compact factual data, derive display labels in code, and avoid persisting explanatory prose or mixed-meaning fields.

## Playlist Availability Display

- `playlist_videos.availability` and `playlist_video_reconciled.availability` should represent actual video availability only.
- Blank means availability cannot be determined, usually because there is no video ID.
- `yt_library/templates/index.html` still has at least one path that renders blank raw availability as `Hidden`.
- Cleanup: render only actual availability there, and use match/recovery badges for explanatory context.

## History Reconciliation Semantics

- `history_reconciled.source_quality` currently mixes source, match state, and time quality.
- Current values include examples such as `matched`, `takeout_exact`, `youtube_date_only`, and `youtube_observed_only`.
- Cleanup: split this into compact factual fields, likely something like source lineage plus `match_type` and/or `time_quality`.
- Derive user-facing labels from code mappings instead of rendering raw keys.

## History Match Notes

- `history_reconciled.match_notes` stores generated prose, including the YouTube observed-time explanation.
- Cleanup: replace generated prose with a compact key, then map that key to display text in code.
- Keep free-form notes only if we later identify truly source-authored or user-authored notes.

## Raw History UI Badges

- `yt_library/templates/history.html` renders `source_quality` directly as a badge.
- Cleanup: introduce a mapping function similar to playlist match labels so internal keys do not leak into the UI.

## Snapshot Source File Columns

- `snapshot_playlists.source_file` and `snapshot_videos.source_file` may be redundant now that `snapshot_key` identifies the Takeout import.
- Cleanup: review whether per-file forensic traceability is still useful inside a zip import.
- If not useful, migrate these columns away and keep snapshot identity at the snapshot level.

## Suggested Order

1. Fix the remaining raw playlist availability display path.
2. Normalize `history_reconciled` source/match/time semantics.
3. Replace `history_reconciled.match_notes` with mapped keys.
4. Update history UI badges to use derived labels.
5. Revisit snapshot `source_file` columns after the history cleanup.
