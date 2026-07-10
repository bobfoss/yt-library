# Cleanup Findings

This review uses the current code as truth and ranks cleanup work by duplication
risk. The project is still early and supports only this local/fresh-install
schema, so historical database upgrade code should not be preserved merely for
compatibility with earlier refactors.

## Completed In This Slice

Status: implemented.

- Collapsed `migrate_database()` to schema initialization from `yt_library/schema.sql`.
- Removed historical upgrade helpers for old playlist owner columns, old channel denormalization, old reconciliation fields, old history tables, and old table rebuilds.
- Moved Takeout subscription syncing out of migration and into `import-history`, where Takeout-derived data belongs.
- Added channel-related indexes to `schema.sql` so fresh databases keep the intended query shape.
- Updated tests to compare migrated databases against `schema.sql`, verify schema-only behavior on an existing database, and cover subscription import from a synthetic Takeout zip.
- Updated `README.md`, `AGENTS.md`, and `design.md` to describe the modular code shape and fresh-install migration posture.

Removed migration categories:

- `*_from_legacy` conversion helpers for retired reconciliation fields.
- Table-rebuild helpers for old history, playlist reconciliation, snapshot, metadata, and recovery schemas.
- Deprecated column-drop/backfill routines that only existed to upgrade earlier local database shapes.
- Legacy table cleanup for tables no longer present in the canonical schema.

## Review Gate For Future Removals

Remove a vestigial candidate only when all are true:

- No active read/write path depends on it.
- No API payload or template path renders it.
- No current-schema test protects it for behavior rather than migration history.
- No current local data operation still needs it; if it does, move that operation to an explicit importer/admin command first.

## Ranked Remaining Cleanup

### 1. History Video Card Duplication

Status: open, highest deduplication payoff.

Evidence:

- `yt_library/templates/index.html` has shared library video-card helpers such as `videoCardFor()`, `playlistVideoCardFor()`, `creatorHtml()`, and `watchedLineHtml()`.
- `yt_library/templates/history.html` still has separate `watchCard()`, `creatorHtml()`, and `watchedLineHtml()` helpers for the same card vocabulary: thumbnail, title, badges, channel, ID, progress, and description.

Cleanup:

- Extract shared browser-side video-card helpers into a small shared static JS/template include.
- Adapt both library search/playlist views and history results to use the same renderer with mode options.

Validation:

- Smoke `/` and `/history`.
- Verify playlist/search cards, history cards, unavailable cards, badges, channel chips, watch progress, and description matches still render.

### 2. Unified Server-Side Omni Search

Status: open, correctness plus deduplication.

Evidence:

- Browser omni-search merges client-side `/api/data` rows with server-paged `/api/history/search` rows.
- The current `index.html` dedupes video-like search results client-side, but pagination/counts are still limited by the mixed source window.

Cleanup:

- Add `/api/search` that ranks, filters, dedupes, counts, and pages playlists, channels, playlist videos, missing/unavailable rows, and history rows server-side.
- Keep `/api/history/search` for the dedicated history page.

Validation:

- Unit-test ranking, dedupe, unavailable inclusion, source filters, description matches, and pagination counts.
- Smoke browser omni-search and dedicated history search.

### 3. Worker Queue/Run/Log Pattern Duplication

Status: open, medium/high risk because workers are operationally important.

Evidence:

- Metadata, playlist scan, live history, placeholder recovery, and dispatcher classes repeat run lifecycle, stop handling, progress counters, logs, status summaries, and queue preview patterns.
- Some duplication is intentional domain behavior; the smell is repeated lifecycle plumbing.

Cleanup:

- Introduce a narrow worker-run helper for shared status/log/progress updates.
- Keep worker-specific fetch/parse/save logic in each worker class.

Validation:

- Unit-test stop behavior, failed/completed run status, queue clearing, and dispatcher summary.
- Smoke `/api/admin/status` and queue start/stop endpoints.

### 4. Archivarix Candidate Path

Status: investigate before removal.

Evidence:

- `index.html` still defines `candidateCardFor()`.
- `fetch_app_data()` still returns `archivarixCandidates`.
- The active recovered-video display appears to use richer snapshot/reconciled card paths instead.

Cleanup:

- Trace all references to `candidateCardFor`, `archivarixCandidates`, and `archivarix_candidates`.
- If no active UI consumes them, remove the unused card helper and decide whether the table/API payload remains useful as an admin/debug source.

Validation:

- Unit-test `fetch_app_data()` if the payload changes.
- Smoke missing/unavailable snapshot views and Archivarix recovery worker output.

### 5. Hidden vs Unavailable Naming

Status: open, broad rename best done late.

Evidence:

- User-facing UI now prefers unavailable language.
- Internal names still include `hidden_count`, `hiddenVideos`, `snapshotLikelyHidden`, and `__hidden_playlists__`.

Cleanup:

- Rename internal API/schema/read-model names toward `unavailable` only when touching adjacent code.
- Avoid schema churn unless the field meaning changes; if renamed, update tests and hash aliases together.

Validation:

- Search all `hidden` references and classify as YouTube source language, unavailable semantics, or obsolete wording.
- Smoke playlist unavailable views and scan logs.

### 6. Collection/Entity Card Duplication

Status: open, lower priority than video cards.

Evidence:

- Playlist cards, snapshot playlist cards, and channel cards repeat a broad card-with-title/details/links/media pattern.
- They differ enough that premature abstraction could obscure domain-specific behavior.

Cleanup:

- Revisit after video-card sharing. Extract only if the card variants continue to drift.

Validation:

- Visual smoke on playlist, snapshot, channel, and search result views.

### 7. Foreign Playlist Continuation Extraction

Status: investigation, not pure cleanup.

Evidence:

- Example: `Alt Tabby 2025` (`PL0Yl36ZlaYcRi8NREnUoajhs1bwWcKdtk`) reported `361 videos`; prior scans exposed 100 to 200 rows.
- Current policy correctly saves the best nonzero exposed row set and does not synthesize unavailable rows from the header gap.

Cleanup:

- Investigate continuation shapes or browser-style scrolling only after a concrete fixture is available.

Validation:

- Preserve reported/exposed count logging and never replace a fuller previous scan with a short transient result.

## Suggested Order

1. Share video-card rendering between library and history.
2. Add unified server-side omni-search.
3. Extract worker lifecycle/log helpers.
4. Remove or quarantine the unused Archivarix candidate UI/API path.
5. Rename hidden/unavailable internals opportunistically.
6. Revisit collection/entity card abstraction only after video cards settle.
7. Investigate foreign playlist continuation extraction with a concrete fixture.
