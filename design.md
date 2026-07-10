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
- `yt_library/templates/` contains the browser, history, and admin HTML.

Primary surfaces:

- `/`: playlist library browser with PocketTube groups, local playlist pages, hidden-video views, snapshot/missing views, omni-search, thumbnails, channel avatars, and watch-progress bars.
- `/history`: single-column watch history search, sorted by descending watch date, with pagination and metadata-enhanced cards.
- `/admin`: status dashboard and worker control plane for metadata, playlist scans, placeholder recovery, and history.

SQLite is the source of truth for local state. Cached thumbnails and avatars are derived local assets. Cookie files, Takeout zips, databases, logs, and thumbnail folders are private runtime data and should remain uncommitted.

## Data Sources

The design deliberately keeps source streams separate before combining them for display:

- Current YouTube playlist web state: playlist membership, ordering, visible videos, hidden placeholders, playlist metadata, and scan status.
- PocketTube export: group hierarchy and playlist organization.
- YouTube live history web state: recent/history ordering, date-level labels, and account-specific thumbnail progress state.
- Takeout history zips: authoritative exported watch timestamps.
- Takeout playlist snapshots: historical playlist membership and add dates.
- Archivarix: recovery evidence for deleted or memory-holed videos, including thumbnails, titles, descriptions, channel evidence, archive links, and not-found/deleted status.
- YouTube watch/channel pages and exact-ID search result cards: enriched video/channel metadata, thumbnails, channel avatars, and watch progress.

Each source has different reliability. Takeout is best for exact watch timestamps, current YouTube scans are best for present playlist state, and Archivarix is best-effort recovery evidence. The UI should expose those differences rather than flattening everything into a false certainty.

## Storage Model

Raw source tables should preserve what was observed:

- `playlists`, `groups`, and `group_playlists` model the library and PocketTube organization.
- `playlist_videos` stores the current playlist scan result.
- `snapshot_playlists` and `snapshot_videos` store Takeout playlist snapshots.
- `youtube_history_occurrences` stores live YouTube history observations by ordinal.
- `takeout_history_occurrences` stores imported Takeout history rows keyed by Takeout export.
- `snapshot_video_recovery` stores Archivarix recovery evidence.
- `video_metadata` stores YouTube video metadata and account-specific watch status.
- `channels` normalizes channel title, URL, avatar, and Archivarix channel IDs by YouTube channel ID.

Display overlays should combine evidence without overwriting source meaning:

- `playlist_video_reconciled` is the playlist display layer that can restore hidden or missing video identities from Takeout and Archivarix evidence.
- `history_reconciled` is the history display/search layer that merges live YouTube history observations with Takeout watch times.

Channel information is intentionally normalized because video metadata, history, playlist rows, and Archivarix recovery repeat the same creator data. Source tables may still keep raw channel text when no stable channel ID is available.

## Worker Model

Long-running and rate-sensitive tasks run as in-process background workers with persistent run rows and logs:

- Metadata worker: prioritizes missing channel metadata first, then playlist video metadata, then history video metadata. It fetches channel pages directly when keyed by channel and watch pages/search cards when keyed by video.
- Playlist scan worker: scans playlists with yt-dlp first and falls back to the web parser when needed. It records scan counts, hidden counts, and errors.
- Placeholder recovery worker: uses reconciled playlist placeholders to query Archivarix for deleted/private/unavailable video IDs.
- History worker: supports recent fetch and full verification modes, fetching YouTube history in batches and reconciling after each batch.

Workers should be visible and interruptible from `/admin`. Queue counts, queue previews, run summaries, stop buttons, and logs are part of the design, not just debugging conveniences. A server restart interrupts active in-process workers, so run status is reconciled on admin status reads.

## UI Goals

The UI should be a dense local operations tool rather than a marketing page.

- Keep primary views immediately useful: playlist browser, history search, and admin dashboard.
- Prefer local playlist navigation; provide separate external links for opening YouTube.
- Show channel avatars and creator links when normalized channel metadata exists.
- Show uncertainty and source quality with visible badges such as hidden, unavailable, restored from Takeout, Takeout candidate, and Archivarix status.
- Keep controls and queues foldable on admin sections so status cards and run badges remain visible.
- Use cached thumbnails and avatars when available, but tolerate missing media gracefully.

## Prior Art

Most similar projects are archive or download oriented:

- Tube Archivist ([site](https://www.tubearchivist.com/), [GitHub](https://github.com/tubearchivist/tubearchivist)): self-hosted YouTube media server for downloading, indexing, searching, and tracking watched/unwatched archived videos.
- Pinchflat ([GitHub](https://github.com/kieraneglin/pinchflat)): self-hosted YouTube media manager for periodically archiving channels and playlists.
- MeTube ([GitHub](https://github.com/alexta69/metube)): web UI for yt-dlp downloads, including playlists, channels, thumbnails, and queues.
- YouTube History Analyzer ([GitHub](https://github.com/positron48/youtube-history-analyzer)): Takeout/watch-history analytics and reports.
- youtube-playlists-tracker-app ([GitHub](https://github.com/devbret/youtube-playlists-tracker-app)): playlist collection and viewing-progress tracking, especially for playthrough-style playlists.

These overlap with pieces of this project, but none appear to target the same combination of account-library mirroring, PocketTube organization, hidden-video reconciliation, Archivarix recovery, Takeout/live-history reconciliation, and local metadata browsing without making downloading the center of the workflow.

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

Current implementation stores progress fields in video metadata, live history rows, and reconciled history:

- `watch_progress_percent`
- `watch_resume_seconds`

Known extraction shapes:

   - Classic renderer: `thumbnailOverlayResumePlaybackRenderer.percentDurationWatched`
   - New lockup renderer: `thumbnailOverlayProgressBarViewModel.startPercent`
   - Resume candidates: `watchEndpoint.startTimeSeconds`

The watch page may not expose the thumbnail progress overlay for the current video, so metadata fetch can fall back to an exact video-ID search result card. Playlist and history cards render progress as a thin red thumbnail bar plus a `Watched N%` line.

Open question: `startTimeSeconds` may be useful, but it did not match the observed progress percentage in the first test case. Continue treating percentage as the authoritative UI signal until more examples clarify the resume semantics.

## Data Principles

Keep raw sources separate from display overlays:

- YouTube playlist scans represent the current web state.
- Takeout rows preserve historical account export evidence.
- Archivarix rows preserve recovery evidence for removed videos.
- Reconciled tables or views should combine those sources for display without destroying source-specific meaning.

When evidence is uncertain, preserve that uncertainty in the UI instead of silently forcing a match.

## Operational Principles

- Prefer web-interface extraction and local cookies before API usage.
- Be polite with remote services: batch, delay, expose limits, and make workers stoppable.
- Do not require server restarts for data-only changes; API reads should refresh from SQLite. Restart only when code, served HTML/JS, schema/bootstrap, or worker behavior changes.
- Treat `schema.sql` as the canonical fresh-install schema. The project is still early and supports only this local install, so historical upgrade paths should be removed instead of carried as permanent migration code; stale databases should be rebuilt or re-imported.
- Keep personal artifacts out of Git: cookies, Takeout zips, SQLite databases, logs, and cached images.
