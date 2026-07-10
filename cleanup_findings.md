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

## Removal Gate

Remove a vestigial candidate only when all are true:

- No active read/write path depends on it.
- No API payload or template path renders it.
- No current-schema behavior test protects it.
- No current local data operation needs it.

## Ranked Remaining Cleanup

### 1. History Video Card Duplication

`index.html` and `history.html` still implement separate versions of the same video-card vocabulary. Extract shared card helpers into one browser-side module and validate playlist, history, unavailable, progress, reaction, and description rendering.

### 2. Unified Server-Side Omni Search

The browser still combines client-side library data with server-paged history results. Add one server-side search endpoint that ranks, deduplicates, counts, and pages playlists, channels, canonical videos, unavailable memberships, and history events.

### 3. Worker Lifecycle Duplication

Metadata, playlist, history, recovery, and dispatcher classes repeat run status, stop handling, counters, logs, and completion/error updates. Introduce a narrow lifecycle helper while keeping fetch/parse/save behavior worker-specific.

### 4. Collection Card Duplication

Playlist and channel cards share some framing but still have meaningfully different content. Revisit only after the video-card renderer settles.

### 5. Foreign Playlist Continuation Extraction

Foreign playlists can expose fewer rows than their reported count. Continue preserving the best nonzero scan and logging reported versus exposed counts. Investigate continuation behavior only with a concrete fixture and never synthesize unavailable rows from a count gap.

## Suggested Order

1. Share video-card rendering.
2. Add unified server-side search.
3. Extract worker lifecycle helpers.
4. Revisit collection cards.
5. Investigate foreign playlist continuation extraction.
