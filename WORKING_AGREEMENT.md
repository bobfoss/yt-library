# YT Library Working Agreement

This document preserves the durable context established while building YT
Library. Read it with `AGENTS.md` at the start of every chat. Verify current
runtime facts such as the active PID, queue depth, host, port, and dirty files;
do not copy stale values from an earlier chat.

## Collaboration Style

- Start from evidence. Read the owning code, inspect the database or API when
  relevant, and understand the existing mechanism before proposing a cause.
- When the user reports a bug from the UI, correlate the rendered behavior with
  the API and database before changing code. Explain when behavior is correct
  but visually misleading.
- Unless the user is explicitly exploring ideas or asks only for an
  explanation, carry changes through implementation, verification, live QA,
  and commit.
- Keep the user informed during longer work with short, meaningful updates.
  Explain what evidence is being gathered and what it shows.
- Prefer consistency and deduplication. Shared filters, cards, polling,
  persistence, and status logic should use common helpers instead of parallel
  implementations that drift.
- Preserve uncertainty rather than inventing metadata. Blank or unknown values
  are better than plausible but incorrect replacements.
- Use the configured display timezone for user-facing timestamps. Stored exact
  timestamps are UTC; the primary user's display timezone is Pacific unless
  the config says otherwise.

## Workspace Safety

- Confirm the active checkout before acting. The live checkout is normally
  `C:\Users\michael.keenan\personal\YT Library`; similarly named sibling
  checkouts exist and must not be assumed active.
- Inspect `git status` before editing. Treat pre-existing tracked and untracked
  changes as user or parallel-task work. Never revert, overwrite, stage, or
  commit them unless they are explicitly part of the request.
- If the requested files contain unrelated edits, isolate the intended hunks
  when staging. Verify the staged diff before committing.
- Runtime data, databases, cookies, logs, Takeout exports, and thumbnail caches
  remain local and must not enter commits.
- Treat IDs, titles, and URLs as shell-hostile. Quote paths and use argparse's
  `--option=value` form for values beginning with `-`.

## Change And Commit Cadence

- Commit every coherent change after it passes verification, without waiting
  for a separate reminder, unless the user explicitly asks not to commit yet.
- Do not push unless the user explicitly requests a push.
- Keep commits focused. Use a concise imperative subject and a substantive body
  describing behavior, schema or operational impact, and verification.
- Before committing, inspect both the staged and unstaged diffs so unrelated
  work remains untouched.
- Schema changes require migrations for existing databases and fresh-schema
  coverage. Do not assume rebuilding the user's database is acceptable.

## Verification Standard

- Use the bundled Python runtime when bare `python` resolves to a Windows Store
  or Cygwin shim:
  `C:\Users\michael.keenan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`.
- For code changes, run `py_compile`, the full `unittest` suite, and
  `git diff --check`. Add focused tests proportional to the behavior changed.
- For schema work, initialize a fresh temporary database and run integrity and
  foreign-key checks where relevant.
- For UI, served JavaScript, admin settings, or persistence changes, perform
  live browser QA in addition to source tests. Exercise the actual interaction,
  reload behavior, loading state, and rapid changes where stale responses could
  overwrite newer input.
- Smoke-test the live service through the configured address, including
  `/api/admin/status` and a small read endpoint appropriate to the change.
- A command returning successfully is not enough for runtime work. Confirm the
  new process is listening and report its PID and relevant queue state.

## Service And Queue Lifecycle

- Read `yt_library.config.json` to discover the live host and port. Do not
  assume localhost; the personal config commonly binds to a Tailscale address,
  while distributable defaults bind to localhost.
- Restart after changes to server code, workers, served HTML or JavaScript,
  schema/bootstrap behavior, or source/default configuration. A database-only
  update normally does not require a restart because requests reopen SQLite.
- Settings designed for live application, such as pacing controls, should take
  effect immediately. Proxy changes intentionally restart the service.
- Before restarting, call `/api/admin/status` and record the current PID,
  whether the unified queue is running, and its pending count.
- If the queue is running, request a clean stop and wait until both running and
  stopping are false. Never clear persistent queue rows merely to restart.
- Restart the service, wait for a healthy response from a different PID, and
  resume the queue only if it was running beforehand. Confirm and report the
  new PID, queue state, and pending count.
- Whenever reporting a restart, include the PID. If a restart or queue start
  fails, diagnose and log the reason rather than reporting partial success.
- A general proxy applies to all outbound connections. A hard proxy outage
  stops dispatch and retains pending rows; queue-stop and restart failures need
  their own clear log entries. Recovery should probe the restored proxy and
  allow the queue to start again.
- YouTube authentication failures should stop or block the YouTube job family
  without preventing Archivarix work. Archivarix holds should not block
  unrelated YouTube work. A shared proxy failure is the exception because it
  affects both.
- Respect configured pacing and concurrency. The goal is to launch eligible
  requests promptly without exceeding the configured delay or hammering either
  service.

## Configuration Ownership

- `yt_library.config.json` is authoritative for runtime settings. Do not
  recreate an `app_settings` table or split user settings between SQLite and
  config.
- User-editable preferences should persist to config and, when practical, take
  effect immediately. Removing an explicit preference should remove its sparse
  config entry and restore the documented default.
- Preserve config-backed card layouts, sort regimes, page size, partial
  completion threshold, filter opt-ins, timezone, proxy, pacing, concurrency,
  update schedule, and admin mode.
- The default display timezone may be blank; server-side behavior then assumes
  UTC until the client supplies a timezone and the server saves it.
- Keep credentials in explicitly configured local files such as
  `youtube_cookies` and Archivarix cookies. Never log or commit cookie values.
- Do not add legacy config compatibility unless it is intentionally requested.

## Product Direction

- Search is the default landing experience. An empty query returns all eligible
  results, newest first, with missing dates sorted last.
- History remains a distinct occurrence-level view. It can show repeated
  watches in chronological order and owns the yearly views heatmap.
- Video, playlist, channel, and playlist-detail pages remain distinct detail
  views. Admin remains separate.
- Sidebar collection entries are search presets rather than independent list
  implementations where feasible. Modifying a preset's filters returns the
  active highlight to Search. History is not a search preset.
- Use the ordering Videos, Playlists, Channels throughout navigation, filters,
  admin metadata sections, and result presentation where applicable.
- Use `filter` for search scope and `meta` or `visibility filter` for result
  facets. Facet counts describe the unfiltered result universe and must not
  change merely because their checkbox is toggled.
- Parent, child, and grandparent checkbox relationships must remain visually
  and behaviorally synchronized. Selecting a child can reactivate its ancestors.
- Default-hidden opt-ins include unavailable videos, removed playlists,
  terminated channels, and completion below the configured partial threshold.
  Explicit user selections persist sparsely in config.
- Preserve already rendered headers, filters, pagination, heatmaps, and result
  content while replacement data is loading. Use restrained progress indicators
  without causing layout jumps or brief blank pages.
- Persist search and history card layouts independently. Current defaults are
  grid for search and compact for history; video detail uses the detailed card.
- Keep desktop and mobile layouts usable. Tables may become mobile rows, but
  identifiers, subjects, messages, and controls must not collapse into
  unreadable one-character columns.
- Use familiar icons and YouTube's recognizable SVG shapes where intentionally
  matched, including unlisted, membership, and notification states.

## History And Heatmap Semantics

- Heatmap cells represent calendar days, not individual watches. Cell intensity
  represents that day's count.
- The heatmap is a rolling 53-week window with prior-year navigation. With Sync
  enabled, year navigation and history paging move together; disabling Sync
  allows them to move independently.
- Channel history heatmaps are scoped to that channel's canonical videos.
- When a heatmap appears sparse or has a gap, compare database rows, activity
  API output, selected date range, page offsets, Takeout coverage, and live
  history evidence before assuming data loss.
- Exact history timestamps are stored in UTC. Date-only YouTube observations
  retain `watched_at = NULL`, a local `watch_date`, and ordinal ordering; do not
  fabricate exact times.
- Match live history by video occurrence within a day, not by global ordinal.
  Ordinals shift whenever newer watches are inserted.

## Metadata And Data Model Semantics

- The database is a current-state metadata model with occurrence-level history,
  not a metadata archive. `videos` owns canonical video identity and current
  playability; `playlist_items` owns membership; `history_events` owns watches.
- Watch completion belongs to history occurrences. Manual video metadata scans
  must not attach the observed completion to an arbitrary history event.
- Video-level displayed completion is derived from occurrence data. The greatest
  observed completion is useful for display; aggregate resume seconds remain an
  explicitly rough calculation.
- Never replace a useful nonzero completion with an unrelated zero. More
  generally, failed or unavailable metadata must not erase the last useful
  identity.
- Do not use video IDs or URLs as title fallbacks. Leave a missing title blank
  so incomplete metadata remains visible and repairable.
- Channel `first_seen` means earliest library evidence, not the day metadata was
  fetched. Reconcile it from history, playlists, and other trustworthy evidence.
- Video reactions are canonical `videos.reaction` values (`L`, `D`, or empty).
  Liked videos is a derived view, not a normal stored playlist.
- Fetch direct YouTube video, channel, and playlist targets by ID or URL. Do not
  perform YouTube searches as a metadata fallback.
- When authenticated cookies are configured, verify authentication throughout
  long queue runs so expired sessions fail promptly instead of silently losing
  private, member, subscription, or reaction metadata.
- Classify members-only content correctly even when the configured account is
  not a member and cannot play it.
- Keep unavailable, members-only, unlisted, private, removed, and unknown
  semantics distinct where the source can support the distinction.

## Playlist Semantics

- Scan-all playlist work includes live discovery of new playlists, then scans
  existing playlists. Log meaningful membership or count changes.
- Treat the current YouTube page-header count and visibility as authoritative
  evidence, while recognizing that only a subset of member rows may be exposed.
- Never replace a fuller existing playlist scan with a suspiciously short
  extractor result. Preserve and log source/count evidence.
- Keep deleted library playlists as rows marked removed. Use unavailable when
  ownership or deletion evidence is insufficient.
- Do not synthesize unavailable playlist members solely from a gap between the
  displayed count and exposed rows. Create placeholders only from explicit
  hidden/unavailable evidence.
- Playlist ownership, visibility, status, and member-video availability are
  separate facets. A playlist itself is not `unavailable` merely because some
  member videos are unavailable.

## Logging And Admin Behavior

- Queue and log tables use a general `ID` column for video, channel, or playlist
  IDs and a separate Subject column for human-readable titles.
- Keep queue and log polling incremental and efficient. Preserve pagination and
  controls while updates arrive.
- Log important stop, block, retry, recovery, and proxy events separately from
  the individual job failure that triggered them.
- Avoid repetitive lifecycle noise such as one `Placeholder recovery started`
  event per single-item recovery.
- History-fetch summary logs describe the run or batch and should not be labeled
  with an arbitrary video ID or title.
- Successful video metadata history logs may report watch completion, but manual
  metadata scans do not write occurrence completion.
- Basic admin mode exposes initialization when needed, Update, history, queue,
  and logs. Advanced mode exposes specialized scans, backfills, dispatch, and
  tuning controls.

## Documentation And Future Work

- `TODO.md` is the canonical deferred-work list. References to TODOs, cleanup
  findings, or future work all mean that file.
- Keep README, configuration examples, schema behavior, and admin labels aligned
  with shipped behavior.
- Before the 1.0 release, URL compatibility can still change intentionally.
  After 1.0, preserve or migrate public URL formats and document compatibility.
- Do not turn an exploratory prototype into shipped behavior without explicit
  approval. Preserve set-aside prototypes cleanly and keep them out of unrelated
  commits.
