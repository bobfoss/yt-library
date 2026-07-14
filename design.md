# YT Library Manager Design Notes

## Product Direction

YT Library Manager is a local-first tool for understanding and managing a personal YouTube library. It mirrors playlist structure, enriches videos with metadata and cached images, reconciles hidden or deleted videos from Takeout and Archivarix evidence, and exposes searchable history and playlist views through a small web UI.

The project intentionally favors YouTube web-interface data where practical, using cookies from the local project directory. The YouTube API or third-party libraries should be fallback tools when the web surface cannot provide the needed data.

The core product goal is not to become a downloader or media server. It is a personal library-management layer that makes YouTube account state easier to inspect, search, preserve, and reconcile. Downloading may be adjacent later, but the current center of gravity is metadata, organization, history, and evidence.

This project is still in an early alpha stage. Prefer the design that clarifies the domain and future maintenance, even when that means a large schema, API, UI, or architecture change. Avoid preserving awkward legacy shapes just because they already exist; source evidence and personal data should be protected, but the application structure is still allowed to move.

## System Shape

The app is intentionally compact but no longer single-file. `yt_library_manager.py` is the compatibility CLI shim, while the application code is split across a small Python package:

- `yt_library/cli.py` defines CLI commands and keeps existing command names stable.
- `yt_library/core.py` owns schema bootstrap, importers, parsers, metadata fetchers, queue helpers, and reconciliation logic.
- `yt_library/server.py` owns HTTP routing and local API endpoints.
- `yt_library/workers.py` owns in-process worker orchestration.
- `yt_library/queries.py` owns read models for browser and history views.
- `yt_library/schema.sql` is the canonical SQLite schema for fresh local databases.
- `yt_library/templates/` contains the browser, history, and admin HTML plus shared browser-side modules for timezone and video-card rendering.

Primary surfaces:

- `/`: playlist library browser with local groups, playlist pages, liked and unavailable-video views, omni-search, history heatmaps, thumbnails, channel avatars, and watch-progress bars.
- `/history`: single-column watch history search, sorted by descending watch date, with pagination and metadata-enhanced cards.
- `/admin`: status dashboard and worker control plane for metadata, playlist scans, placeholder recovery, and history.

SQLite is the source of truth for local state. Cached thumbnails and avatars are derived local assets. Cookie files, Takeout zips, databases, logs, and thumbnail folders are private runtime data and should remain uncommitted.

## Data Sources

Source parsers provide evidence for one best-known current state:

- Current YouTube playlist web state: playlist membership, ordering, visible videos, hidden placeholders, playlist metadata, and scan status.
- YouTube live history web state: recent/history ordering, date-level labels, and account-specific thumbnail progress state.
- Takeout history zips: authoritative exported watch timestamps.
- The newest Takeout export: current playlist membership, subscriptions, exact history timestamps, and recovery input.
- YouTube's Liked videos system playlist: current per-video like state.
- Archivarix: recovery evidence for deleted or memory-holed videos, including thumbnails, titles, descriptions, channel evidence, archive links, and not-found/deleted status.
- YouTube watch/channel pages and exact-ID search result cards: enriched video/channel metadata, thumbnails, channel avatars, and watch progress.

Each source has different reliability. Takeout is best for exact watch timestamps, current YouTube scans are best for present playlist and metadata state, and Archivarix is best-effort recovery evidence. Source fields are consumed during import instead of being retained as parallel metadata histories.

PocketTube import is intentionally deferred. The compatibility import command and existing group records remain, but PocketTube is not part of current configuration or routine ingestion. A future group-import design may use a different mechanism.

## Storage Model

The database models the best-known current state of YouTube. Imports and scans replace superseded metadata rather than preserving revisions. When content becomes unavailable, the last known useful state is retained so removed content remains identifiable.

- `videos` owns canonical video metadata, current playability, availability, reaction, progress, and fetch state.
- `channels` owns canonical channel metadata and subscription state.
- `playlists`, `groups`, and `group_playlists` model the current library organization.
- `playlist_items` links playlists to videos and retains only membership, position, unavailable-slot, and reconciliation facts.
- `history_events` stores watch events. Exact Takeout timestamps and date-only live observations share this table without fabricating precision.
- `video_recovery` stores only current Archivarix recovery status, capture time, media availability, and errors.
- `worker_queue` stores prioritized metadata, playlist, history, and recovery tasks. Queue events and worker-specific run/log tables provide operational history.

Parsers may use titles, channels, descriptions, and URLs transiently to update canonical entities, then discard those source copies. Metadata revisions and complete historical playlist snapshots are intentionally not retained.

Runtime settings, including the display timezone, request launch intervals, concurrency limits, cookie paths, and bind address, live in `yt_library.config.json`, not in SQLite. An empty display timezone is treated as UTC by the server until the browser detects an IANA timezone and saves it through the settings endpoint.

Unknown playlist slots use `NULL` video IDs and structured unavailable state. Stable YouTube video, playlist, and channel URLs are generated from IDs. Wayback links are generated from a video ID plus the retained capture timestamp.

## Worker Model

Long-running and rate-sensitive tasks run as in-process background workers with persistent queue rows, run records, and logs. The unified dispatcher selects the next eligible `worker_queue` row by priority before each launch:

- Metadata tasks fetch channel pages directly when keyed by channel and watch pages/search cards when keyed by video. Each authenticated request verifies that YouTube still accepts the configured cookie; authentication failure stops further YouTube dispatch.
- Playlist tasks scan playlists with yt-dlp first and fall back to the web parser when needed. They record reported, exposed, and unavailable counts without replacing a fuller scan with a short result.
- Placeholder tasks query Archivarix for deleted/private/unavailable video IDs and preserve rate-limited tasks for a later retry.
- History tasks support recent fetch and full verification modes, fetching YouTube history in batches and reconciling after each batch.

YouTube metadata and Archivarix recovery have independent launch intervals and `max_in_flight` limits from the config file, so a slow request to one site does not stall the other site's cadence. Playlist and history tasks remain worker-specific and run through the same prioritized queue. The dispatcher checks SQLite again before each launch, so priority changes and newly queued work can affect later dispatches without rebuilding an in-memory batch.

Workers should be visible and interruptible from `/admin`. Queue counts, previews, timing estimates, stop buttons, and incrementally polled logs are part of the design, not just debugging conveniences. A server restart interrupts active in-process workers, so run status is reconciled on admin status reads.

## UI Goals

The UI should be a dense local operations tool rather than a marketing page.

- Keep primary views immediately useful: playlist browser, history search, and admin dashboard.
- Prefer local playlist navigation; provide separate external links for opening YouTube.
- Show channel avatars and creator links when normalized channel metadata exists.
- Show actionable availability, visibility, and Archivarix status while keeping internal source and reconciliation-match labels out of the user interface.
- Keep controls and queues foldable on admin sections so status cards and run badges remain visible.
- Use cached thumbnails and avatars when available, but tolerate missing media gracefully.

## Prior Art

Most similar projects are archive or download oriented:

- Tube Archivist ([site](https://www.tubearchivist.com/), [GitHub](https://github.com/tubearchivist/tubearchivist)): self-hosted YouTube media server for downloading, indexing, searching, and tracking watched/unwatched archived videos.
- Pinchflat ([GitHub](https://github.com/kieraneglin/pinchflat)): self-hosted YouTube media manager for periodically archiving channels and playlists.
- MeTube ([GitHub](https://github.com/alexta69/metube)): web UI for yt-dlp downloads, including playlists, channels, thumbnails, and queues.
- YouTube History Analyzer ([GitHub](https://github.com/positron48/youtube-history-analyzer)): Takeout/watch-history analytics and reports.
- youtube-playlists-tracker-app ([GitHub](https://github.com/devbret/youtube-playlists-tracker-app)): playlist collection and viewing-progress tracking, especially for playthrough-style playlists.

These overlap with pieces of this project, but none appear to target the same combination of account-library mirroring, unavailable-video reconciliation, Archivarix recovery, Takeout/live-history reconciliation, and local metadata browsing without making downloading the center of the workflow.

## Watch Progress Discovery

YouTube exposes thumbnail watch status as card-renderer metadata, not as ordinary watch-page metadata.

Observed test video:

- URL: `https://www.youtube.com/watch?v=6RTNO-nMGBc`
- Title: `AT&T Fiber Without the Gateway (It Actually Works)`
- Exact-ID search result exposed `thumbnailOverlayResumePlaybackRenderer.percentDurationWatched = 10`.
- History feed lockup exposed `thumbnailOverlayProgressBarViewModel.startPercent = 10`.
- Direct watch page did not expose those progress fields in the useful metadata path.

The same card also included `watchEndpoint.startTimeSeconds = 7`, but that does not line up cleanly with 10% of an 11:34 video. Treat the thumbnail progress percentage as the authoritative display signal until proven otherwise.

## Watch Progress Design

Watch progress is account-specific, volatile state. It should be captured as enrichment, not treated as equivalent to history evidence.

Current video progress is stored on `videos`; progress observed on a particular live-history card remains on that `history_events` occurrence:

- `watch_progress_percent`
- `watch_resume_seconds`

Known extraction shapes:

   - Classic renderer: `thumbnailOverlayResumePlaybackRenderer.percentDurationWatched`
   - New lockup renderer: `thumbnailOverlayProgressBarViewModel.startPercent`
   - Resume candidates: `watchEndpoint.startTimeSeconds`

The watch page may not expose the thumbnail progress overlay for the current video, so metadata fetch can fall back to an exact video-ID search result card. Playlist and history cards render progress as a thin red thumbnail bar plus a `Watched N%` line.

Open question: `startTimeSeconds` may be useful, but it did not match the observed progress percentage in the first test case. Continue treating percentage as the authoritative UI signal until more examples clarify the resume semantics.

## Time And Data Principles

- Store every exact instant as ISO 8601 UTC with a trailing `Z`.
- Store date-only YouTube history as `watch_date` with `watched_at = NULL`; ordinal preserves relative feed order.
- Detect the browser IANA timezone only when no saved value exists. Admin may override it.
- Convert exact timestamps for display, but never timezone-shift a date-only observation.
- Current YouTube metadata supersedes Takeout and Archivarix metadata. Empty or failed responses never erase useful values.
- When a video becomes unavailable, update playability and availability while retaining its last useful identity.
- When evidence is uncertain, preserve that uncertainty in membership or match fields instead of inventing an identity or timestamp.

## Operational Principles

- Prefer web-interface extraction and local cookies before API usage.
- Be polite with remote services: batch, delay, expose limits, and make workers stoppable.
- Do not require server restarts for data-only changes; API reads should refresh from SQLite. Restart only when code, served HTML/JS, schema/bootstrap, or worker behavior changes.
- Treat `schema.sql` as the canonical fresh-install schema. The project is still early and supports only this local install, so historical upgrade paths should be removed instead of carried as permanent migration code; stale databases should be rebuilt or re-imported.
- Keep personal artifacts out of Git: cookies, Takeout zips, SQLite databases, logs, and cached images.
