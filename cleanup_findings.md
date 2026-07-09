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

## Shared Video Card Rendering

Status: completed for active library video cards.

- `yt_library/templates/index.html` now routes playlist/search videos, raw unavailable videos, and snapshot missing / likely unavailable videos through a shared `videoCardFor()` renderer.
- Badges are rendered as their own vertical block, and creator/channel chips render on their own line.
- Remaining cleanup: share this same video-card concept with the history page so watch-history results do not drift from library video cards.

## History Video Card Duplication

- `yt_library/templates/history.html` still has its own `watchCard()`, `creatorHtml()`, and `watchedLineHtml()` helpers.
- This card renders the same kind of object as the library video cards: thumbnail, title, badges, channel, video ID, watch progress, and description.
- Cleanup: move common browser-side card helpers into a shared static JS module or shared template include, then adapt both `index.html` and `history.html` to use it.

## Collection Card Duplication

- Normal playlist cards, snapshot playlist cards, and channel cards all repeat a broad "card with title, details, links, and optional description/media" structure.
- These cards differ enough that the payoff is smaller than the video-card cleanup, but they are candidates if UI drift continues.
- Cleanup: consider a generic collection/entity card builder only after the history video-card duplication is resolved.

## Unused Archivarix Candidate Card

- `yt_library/templates/index.html` still defines `candidateCardFor()`, but no current UI path calls it.
- It appears to be leftover from the earlier Archivarix candidate workflow that rendered rows from `data.archivarixCandidates` / `archivarix_candidates`.
- The richer snapshot and playlist video card paths now cover the active recovered-video display needs.
- Cleanup: remove `candidateCardFor()` and then review whether the `archivarixCandidates` API payload and table path are still useful.

## Hidden Naming Cleanup

- User-facing playlist/video-row language is moving from `hidden` to `Unavailable`.
- Internal schema and API names still use terms such as `hidden_count`, `hiddenVideos`, `snapshotLikelyHidden`, and `__hidden_playlists__`.
- Cleanup: when convenient, migrate internal names to `unavailable` equivalents while preserving existing data and backwards-compatible hash aliases.

## Suggested Order

1. Share the video-card renderer with `history.html`.
2. Remove the unused Archivarix candidate card path.
3. Revisit hidden/unavailable internal naming.
4. Revisit collection/entity card duplication only if playlist, snapshot playlist, and channel cards continue to drift.
