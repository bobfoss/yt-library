# YT Library Manager Design Notes

## Product Direction

YT Library Manager is a local-first tool for understanding and managing a personal YouTube library. It mirrors playlist structure, enriches videos with metadata and cached images, reconciles hidden or deleted videos from Takeout and Archivarix evidence, and exposes searchable history and playlist views through a small web UI.

The project intentionally favors YouTube web-interface data where practical, using cookies from the local project directory. The YouTube API or third-party libraries should be fallback tools when the web surface cannot provide the needed data.

## Prior Art

Most similar projects are archive or download oriented:

- Tube Archivist: self-hosted YouTube media server for downloading, indexing, searching, and tracking watched/unwatched archived videos.
- Pinchflat: self-hosted YouTube media manager for periodically archiving channels and playlists.
- MeTube: web UI for yt-dlp downloads, including playlists, channels, thumbnails, and queues.
- YouTube History Analyzer: Takeout/watch-history analytics and reports.
- youtube-playlists-tracker-app: playlist collection and viewing-progress tracking, especially for playthrough-style playlists.

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

## Watch Progress Implementation Sketch

Add watch-progress capture as a separate enrichment path:

1. Store a nullable progress percentage for videos/history rows.
2. Extend live history parsing to extract both known shapes:
   - Classic renderer: `thumbnailOverlayResumePlaybackRenderer.percentDurationWatched`
   - New lockup renderer: `thumbnailOverlayProgressBarViewModel.startPercent`
3. For playlist/library videos outside the current history feed, add a low-rate worker that searches exact video IDs and reads the result-card progress overlay.
4. Render the value as a thin progress bar on local video cards, keeping the raw percentage available for debugging.

Progress data is account-specific and volatile. It should be refreshed carefully, logged like other workers, and never treated as durable viewing history in the same way as Takeout timestamps.

## Data Principles

Keep raw sources separate from display overlays:

- YouTube playlist scans represent the current web state.
- Takeout rows preserve historical account export evidence.
- Archivarix rows preserve recovery evidence for removed videos.
- Reconciled tables or views should combine those sources for display without destroying source-specific meaning.

When evidence is uncertain, preserve that uncertainty in the UI instead of silently forcing a match.
