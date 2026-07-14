# Cleanup Findings

This review uses the current code as truth and ranks remaining cleanup by duplication risk. The project supports only a fresh current schema; historical database upgrade code is intentionally excluded.

## Completed Cleanup

### Fresh Schema Bootstrap

- `migrate` initializes only `yt_library/schema.sql`.
- Historical table rebuilds, legacy column conversions, and upgrade-only tests are removed.
- The newest selected Takeout is imported as current input rather than accumulated as metadata history.

### Canonical Current-State Model

- `videos` is the only owner of video metadata, playability, availability, reaction, and current progress.
- `playlist_items` stores playlist membership and unavailable-slot facts without copying video metadata.
- `history_events` stores exact or date-only watch events without copying video metadata or fabricating midnight timestamps.
- `video_recovery` stores only current Archivarix status, capture time, media availability, and errors.
- The former raw/reconciled playlist, snapshot, history, metadata, and candidate tables are removed.
- Metadata and playlist queues now share `worker_queue`; the old persisted metadata queue is removed.
- The superseded `CHANNEL_NORMALIZATION_PLAN.md` is removed; `design.md` now owns the current model.

### Time And URL Normalization

- Exact timestamps are ISO 8601 UTC values ending in `Z`.
- Live-history rows retain date and ordinal when an exact time is unavailable.
- Browser JavaScript detects and saves an IANA timezone only when the setting is missing; Admin can override it.
- Stable YouTube and Archivarix URLs are generated from IDs and archive capture timestamps.
- Schema, API, and template state use `unavailable` rather than the retired `hidden` compatibility names.

### Configuration And Queue Operations

- Runtime defaults live in `yt_library.config.json`; the database no longer contains `app_settings`.
- New installs bind to `127.0.0.1` by default, while a specific Tailscale address can be configured for remote access.
- YouTube and Archivarix cookie paths, launch intervals, and concurrency limits have explicit config keys.
- The persistent queue dispatches tasks by priority and applies independent YouTube and Archivarix launch cadence and concurrency limits.
- YouTube authentication is checked throughout metadata work so an expired cookie stops the run instead of silently degrading later tasks.
- Admin queue and log views use incremental polling rather than repeatedly transferring full snapshots.

### Browser Workflow

- Liked videos are derived from canonical reaction metadata and have video-count, unavailable, and sort controls.
- History and channel-history views have navigable activity heatmaps that remain stable across pagination and year changes.
- Internal Takeout/YouTube source and match badges are retained in data where needed but are not rendered to users.
- Video and channel detail pages avoid repeated headings, and exact video timestamps render in the configured display timezone.
- Omni-search is server-owned: SQLite filters source and text fields, deduplicates canonical videos across playlist/history evidence, ranks all entity types, counts the complete result set, and returns the requested page.
- The browser no longer blocks on a whole-library `/api/data` snapshot. A lightweight bootstrap supplies navigation counts, while playlist, video, channel, and detail read models return only the requested page and hydrate only visible cards.

### Shared Video Card Rendering

- `video-card.js` owns video-card DOM construction, thumbnails, progress, creator chips, watch summaries, sparklines, reactions, details, and descriptions.
- The main browser and standalone history page provide thin row adapters to the shared renderer.
- Page-specific CSS remains responsible for layout, so standalone history keeps wide horizontal cards while playlist and search views keep compact grids.

### Shared Worker Lifecycle

- A private lifecycle base owns worker locks, background threads, stop events, running/stopping state, blocked reasons, duplicate-start protection, and standard start/stop responses.
- Metadata, playlist, history, placeholder recovery, and queue dispatcher workers retain their task-specific fetching, persistence, logging, and completion behavior.
- Placeholder recovery attempts now persist run IDs, lifecycle and recovery outcomes, and dedicated run-linked logs just like the other external workers.
- Dispatcher site cadence, concurrency limits, queue priority, and per-request YouTube authentication checks are unchanged.

## Removal Gate

Remove a vestigial candidate only when all are true:

- No active read/write path depends on it.
- No API payload or template path renders it.
- No current-schema behavior test protects it.
- No current local data operation needs it.

## Ranked Remaining Cleanup

### 1. Archivarix Backoff And Retry Controls

Archivarix 429 and quota responses stop further recovery dispatch for the current run, but the blocked state and retry path are not explicit enough in Admin. Expose the reason and retry eligibility, preserve pending tasks, and provide a deliberate retry action after credentials or quota state change. Do not automatically hammer a rate-limited endpoint.

### 2. Collection Card Duplication

Playlist and channel cards share some framing but still have meaningfully different content. Revisit only after the video-card renderer settles.

### 3. Foreign Playlist Continuation Extraction

Foreign playlists can expose fewer rows than their reported count. Continue preserving the best nonzero scan and logging reported versus exposed counts. Investigate continuation behavior only with a concrete fixture and never synthesize unavailable rows from a count gap.

## Deferred Decisions

- PocketTube import is deferred and is not a current configuration concern. Revisit group ingestion as a new design rather than restoring the removed config directive.
- Previous-database queue backfill remains a one-off recovery operation. Promote it to a supported command only if the workflow repeats and can define source-version and conflict rules.
- `watch_resume_seconds` remains less trustworthy than the observed progress percentage. Do not expand resume-time behavior until additional examples explain the mismatch.
- Foreign playlist continuation work remains fixture-driven; current best-nonzero preservation is the safe behavior.

## Suggested Order

1. Add explicit Archivarix backoff and retry controls.
2. Reassess collection-card duplication after the shared video-card renderer has settled.
3. Investigate foreign playlist continuations only with a reproducible fixture.
4. Revisit collection cards.
5. Investigate foreign playlist continuation extraction when a reproducible fixture is available.
