# Cleanup Findings

This document tracks code and schema cleanup opportunities that match the recent `playlist_video_reconciled` pattern: store compact factual data, derive display labels in code, and avoid persisting explanatory prose or mixed-meaning fields.

## Playlist Availability Display

Status: completed.

- `playlist_videos.availability` and `playlist_video_reconciled.availability` should represent actual video availability only.
- Blank means availability cannot be determined, usually because there is no video ID.
- `yt_library/templates/index.html` now renders blank availability as no availability badge.
- Match/recovery badges remain responsible for explanatory context.

## History Reconciliation Semantics

Status: completed.

- `history_reconciled.source_quality` currently mixes source, match state, and time quality.
- Current values include examples such as `matched`, `takeout_exact`, `youtube_date_only`, and `youtube_observed_only`.
- Cleanup completed by replacing `source_quality` with compact `source_type`, `match_type`, and `time_quality` fields.
- Derive user-facing labels from code mappings instead of rendering raw keys.

## History Match Notes

Status: completed.

- `history_reconciled.match_notes` stores generated prose, including the YouTube observed-time explanation.
- Cleanup completed by removing `match_notes` and deriving explanatory text from `time_quality`.
- Keep free-form notes only if we later identify truly source-authored or user-authored notes.

## Raw History UI Badges

Status: completed.

- `yt_library/templates/history.html` renders `source_quality` directly as a badge.
- Cleanup completed by rendering derived `history_badges` from the history search read model.

## Snapshot Source File Columns

Status: completed.

- `snapshot_playlists.source_file` and `snapshot_videos.source_file` may be redundant now that `snapshot_key` identifies the Takeout import.
- Cleanup completed by removing the columns and keeping snapshot identity at the snapshot level.

## Suggested Order

1. Fix the remaining raw playlist availability display path.
2. All tracked cleanup items are complete.
