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

### Archivarix Backoff And Retry Controls

- Archivarix authentication and rate-limit failures persist a service block linked to the triggering placeholder run and queue item.
- The dispatcher preserves blocked placeholder tasks and continues eligible YouTube, playlist, and history work.
- Admin shows the block reason and local time and provides a deliberate retry action after credentials or quota state changes.
- Service restarts retain the block, and Archivarix does not retry automatically without an explicit retry request.

### Shared Collection Card Rendering

- `collection-card.js` owns the common playlist/channel card shell, media region, result-kind label, title/action row, and body container.
- Playlist and channel adapters retain their distinct metadata, visibility, status, owner/subscription, description, and source-link content.
- The shared builder preserves linked playlist thumbnails, playlist placeholders, and the existing channel thumbnail behavior.

## Removal Gate

Remove a vestigial candidate only when all are true:

- No active read/write path depends on it.
- No API payload or template path renders it.
- No current-schema behavior test protects it.
- No current local data operation needs it.

## Ranked Remaining Cleanup

### 1. Foreign Playlist Continuation Extraction

Foreign playlists can expose fewer rows than their reported count. Continue preserving the best nonzero scan and logging reported versus exposed counts. Investigate continuation behavior only with a concrete fixture and never synthesize unavailable rows from a count gap.

### 2. Preset-Backed Library Views

Consolidate eligible left-navigation list pages as named omni-search presets instead of maintaining separate fetch, filter, pagination, loading, and rendering branches. A preset should declaratively own its title, entity kinds, source constraints, meta filters, default sort, and any additional server-side scope.

Good initial candidates are All playlists, Channels, Subscribed channels, and Terminated channels because the omni-search model already represents their entity and visibility constraints. Playlist videos, Liked videos, Playlists with unavailable, and playlist group pages need additional parity work:

- Add an explicit liked-video or `reaction = L` source constraint.
- Add playlist `has_unavailable_videos` and `group_key` constraints, including parent-plus-child group membership.
- Preserve page-specific sort semantics such as recently added, playlist order, and most unavailable instead of mapping them silently to omni-search Newest.
- Apply a complete preset state on navigation so filters from a previous search cannot leak into the selected view.
- Keep preset URLs canonical and decide when modifying a preset should retain its sidebar identity versus become a custom search.
- Push preset constraints into candidate selection before counting, sorting, and pagination so consolidation does not turn narrow pages into broad whole-library queries.
- Verify result totals, meta counts, ordering, cards, and pagination against each existing endpoint before removing its dedicated path.

History and channel history must remain specialized because they display watch occurrences rather than distinct videos and own the activity heatmap, year navigation, date jumps, and scroll positioning. Playlist, video, and channel detail pages also remain specialized because they carry entity chrome, tabs, playlist positions, membership state, and scoped actions that are not generic search-result behavior.

Implement this as a shared saved-view/search specification with optional specialized chrome, not as hard-coded mutations of the current global search controls. This creates a path toward user-defined saved searches while retaining dedicated controllers where the data semantics require them.

## Deferred Decisions

- PocketTube import is deferred and is not a current configuration concern. Revisit group ingestion as a new design rather than restoring the removed config directive.
- Previous-database queue backfill remains a one-off recovery operation. Promote it to a supported command only if the workflow repeats and can define source-version and conflict rules.
- `watch_resume_seconds` remains less trustworthy than the observed progress percentage. Do not expand resume-time behavior until additional examples explain the mismatch.
- Watch-progress history needs an evidence survey before expanding the model. `history_events` already stores progress observed on individual live-history occurrences, while metadata scans maintain the latest canonical percentage on `videos`; there is no durable chronology that associates successive metadata observations with distinct rewatches. Survey progress coverage by watch date and observation date to estimate when YouTube stops exposing completion data. If repeated watches produce lower percentages alongside corresponding new history occurrences, evaluate retaining those observations per occurrence instead of replacing the prior canonical completion.
- Playlist Newest sorting currently uses the newest member video's upload date so metadata refreshes and owner backfills do not reorder playlists. This is only a content-freshness approximation: adding an old video to a playlist will not make that playlist recent. Revisit playlist chronology by capturing a reliable membership-added or first-seen timestamp on `playlist_items`, then decide whether Newest should represent playlist activity, newest content, or offer both sorts.
- Foreign playlist continuation work remains fixture-driven; current best-nonzero preservation is the safe behavior.

## Suggested Order

1. Investigate foreign playlist continuation extraction when a reproducible fixture is available.
2. Define the preset/search specification and migrate the parity-safe playlist and channel list views.
3. Add the missing liked, unavailable-playlist, playlist-group, and page-specific sort semantics before migrating the remaining list views.
