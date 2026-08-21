# YouTube Personal-Data Collection Prior-Art Survey

Survey date: 2026-07-29; continuation validation updated 2026-08-01
Maintenance cutoff: at least one repository commit since 2025-07-29
Survey phase: advertised-capability screening plus targeted code and live-account verification

## Scope

This survey covers open-source projects and official client libraries that collect
information from YouTube or YouTube Music. It emphasizes sources that can access
signed-in or otherwise personal data, including:

- Private and unlisted playlists.
- Playlist creation and update times.
- The time a video was added to a playlist.
- Watch-history event times.
- Watch percentage, resume position, or actual watch duration.
- Likes, subscriptions, notifications, and similar account data.

This first pass intentionally favors documented or advertised capabilities over
deep code review. Maintenance eligibility was checked against repository activity
as of the survey date.

## Executive summary

Fifteen eligible projects or implementation tracks were identified. No single
project covers all the target metadata. The strongest design is a hybrid:

1. Use Google's official YouTube Data API for subscription, owned-playlist, and
   playlist-item timestamps.
2. Poll Google My Activity's signed-in structured page data for prospective
   exact watch-event timestamps.
3. Keep Google Takeout as the historical seed, or use Data Portability where the
   account region is eligible.
4. Use local browser instrumentation when accurate future watch duration or
   segment-level behavior is required.

The most important finding is that Google's Data Portability schema explicitly
advertises fields for playlist creation time, playlist update time, and playlist
video creation time—the latter representing when an item was added to a
playlist. It also covers private, unlisted, and public playlists. The YouTube Data
API exposes closely related `publishedAt` fields on both playlists and playlist
items.

The official YouTube Data API does not expose Watch History or Watch Later.
YouTube Analytics percentages are creator-facing aggregate metrics, not an
individual viewer's personal playback progress.

## Verified drill-down

### Google My Activity provides exact watch-event instants

A live signed-in test of `https://myactivity.google.com/product/youtube?hl=en`
confirmed that the visible activity cards and their detail dialog show only a
minute-level time. However, the page's structured `AF_initDataCallback` bootstrap
payload carries a Unix-microsecond timestamp and a distinct opaque activity token
for each activity record. Watched records also carry the video title and YouTube
watch URL. This is materially better than scraping the rendered text:

- Each repeat watch remains a distinct event because its activity token differs.
- The timestamp retains seconds and subsecond precision.
- The `hl=en` request makes the `Watched` activity discriminator deterministic
  for the current parser.

The bootstrap is only an initial recent window, but its continuation transport is
now reproduced. The page exposes a dynamic RPC ID, request template, build label,
session ID, and anti-CSRF value. Posting the opaque continuation token through
Google's framed `batchexecute` endpoint returned consecutive pages of 100 activity
records. A bounded live test fetched 300 distinct activity records across three
pages, including 170 distinct exact watch events.

The repository's `collect-my-activity` Python harness now follows those structured
continuations with a caller-supplied page bound. It validates every response,
rejects repeated continuation tokens and no-progress pages, deduplicates events,
and waits for the requested sequence to succeed before appending. The opaque
activity token is hashed before storage. The internal endpoint is undocumented,
so the initial-page overlap guard remains useful for frequent prospective polls.

`import-my-activity` now imports that evidence into `history_events`. It retains
the hashed My Activity ID, replaces a matching YouTube video/date occurrence while
preserving its ordinal and progress, and merges a later Takeout row for the same
video and UTC second. Repeated imports are idempotent, including Takeout imported
after My Activity.

### Official APIs cover the other requested timestamps

The YouTube Data API exposes the following authorized, timestamped resources:

- [`subscriptions.list(mine=true)`](https://developers.google.com/youtube/v3/docs/subscriptions/list)
  returns the authenticated user's subscriptions, and
  [`subscription.snippet.publishedAt`](https://developers.google.com/youtube/v3/docs/subscriptions)
  is the time the subscription was created.
- [`playlists.list(mine=true)`](https://developers.google.com/youtube/v3/docs/playlists/list)
  returns playlists owned by the authenticated user, and
  [`playlist.snippet.publishedAt`](https://developers.google.com/youtube/v3/docs/playlists)
  is the playlist creation time.
- [`playlistItem.snippet.publishedAt`](https://developers.google.com/youtube/v3/docs/playlistItems)
  is the time the item was added to the playlist.

This means browser scraping is not the preferred implementation for these three
fields. A normal read-only YouTube OAuth collector should be the next production
slice.

The [Data Portability YouTube schema](https://developers.google.com/data-portability/schema-reference/youtube)
also includes playlist creation/update timestamps and playlist-item creation
timestamps. Its subscription export contains channel ID, URL, and title, but no
subscription timestamp; the Data API is stronger for that field. The
[My Activity portability schema](https://developers.google.com/data-portability/schema-reference/my_activity)
does define timestamped `Watched` and `Subscribed` activities.

Data Portability is not a universal replacement for Takeout. It requires a
Google Cloud project with billing, uses a separate OAuth grant, produces an
archive asynchronously, and is currently
[limited to listed European countries and the United Kingdom](https://support.google.com/accounts/answer/14452558?hl=en).
In particular, it is unavailable to an account associated with the United
States.

### Prior-art code audit

`yt-digest` is useful proof that YouTube History and My Activity can be combined,
but its current collector is not a reliable exact-event source:

- Its My Activity DOM extraction is locale-specific and records only `HH:MM`.
- Its own specification reports time matches for only about 60 percent of a
  sample run.
- It merges on `videoId + date`, which collapses repeat watches of the same
  video on the same day onto one time.
- Its proposed Takeout automation still clicks through the export UI and waits
  for the archive email; it does not avoid Google's bulk export latency.

`yt-dlp :ythistory` and the current YouTube history page remain useful for titles,
ordering, progress overlays, and reconciliation, but neither supplies the exact
watch-event instants exposed by My Activity.

The older [youtube-stats extension](https://github.com/bhavyaw/youtube-stats)
provided a useful implementation clue by naming My Activity's continuation
endpoint and token parameter. Its direct `ct` request no longer works on the
current page: the current collector must supply the page's anti-CSRF value and
dynamic framed-RPC metadata.

### Takeout can be scheduled, but it remains a bulk fallback

Google officially supports
[scheduled Takeout archives every two months](https://support.google.com/accounts/answer/3024190?hl=en)
and a customized Takeout URL can preselect products, a cloud destination, and
that frequency. This removes some clicking, but it does not provide prompt
incremental collection: Google still builds an archive asynchronously and says
creation can take minutes to days. Browser automation around the Takeout UI
would add authentication and layout fragility without removing that wait.

## High-priority candidates

| Candidate | Recent maintenance evidence | Advertised capabilities | Initial assessment |
|---|---|---|---|
| [Google APIs Python Client](https://github.com/googleapis/google-api-python-client) | [2026-07-14 commit](https://github.com/googleapis/google-api-python-client/commit/ce8b4331dddfa4026c2c97514720c4fb34650a18) | Official OAuth client for the YouTube Data API and Data Portability API. | Strongest authoritative source. The Data API supports a user's own and private playlists. Data Portability covers private, unlisted, and public playlists, subscriptions, comments, and My Activity exports. |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | [2026-07-23 commit](https://github.com/yt-dlp/yt-dlp/commit/fdcc954df4955267ec1627cbeb347b661a110e7c) | Cookie authentication, private content, and special signed-in feeds including `:ythistory`, `:ytwatchlater`, `:ytfav`, `:ytsubs`, and notifications. | The broadest mature extractor. It is likely useful for playlist membership and video metadata, but the target playlist timestamps and playback progress are not advertised as standard output fields. |
| [YouTube.js](https://github.com/LuanRT/YouTube.js) | [2026-07-03 commit](https://github.com/LuanRT/YouTube.js/commit/14825d7712e32b208830895701973a5a934a3522) | Cookie authentication, limited TV OAuth, history, library, user playlists, subscriptions, and notifications. | The richest reusable InnerTube implementation found. Its typed models include history continuation and `percent_duration_watched`, making it especially promising for playback-progress research. |
| [Kaset](https://github.com/sozercan/kaset) | Active 2026 development with documented YouTube request profiles and sanitized parser fixtures. | Authenticated `WEB` InnerTube requests for regular YouTube, including search, subscriptions, Shorts, Watch Later, history, comments, watch metadata, and continuations. | A useful implementation reference alongside yt-dlp and YouTube.js for identifying API capabilities, authentication requirements, current renderer shapes, and fail-closed parsing patterns. Treat it as prior art, not a runtime dependency or behavioral authority. |
| [ytmusicapi](https://github.com/sigma67/ytmusicapi) | [2026-07-25 commit](https://github.com/sigma67/ytmusicapi/commit/39251815605c2b5078f525e86e2e01c02c952e41) | Cookie or OAuth authentication; library, likes, subscriptions, playlists, uploads, and play history. | Strong for YouTube Music account data. `get_history` advertises a `played` value, but the documented responses do not advertise playlist creation time, item-added time, or watch percentage. |
| [yt-digest](https://github.com/corca-ai/yt-digest) | [2026-04-21 commit](https://github.com/corca-ai/yt-digest/commit/c10a198f02d220095761427ea5afa4dd9209c9b9) | Uses a logged-in browser to combine YouTube History with Google My Activity. | Highly relevant to the target problem. Its specification advertises date, exact time, duration, progress percentage, and last resume position. |
| [google_takeout_parser](https://github.com/purarue/google_takeout_parser) | [2026-01-23 commit](https://github.com/purarue/google_takeout_parser/commit/aebb2dcade4b9156c59ea04a4ced6063dc209c82) | Parses and merges Google Takeout data including YouTube and My Activity history, comments, live chat, and likes. | A useful offline import baseline with evolving-format and deduplication handling. Playlist timestamp coverage is not prominently advertised. |
| [SSW.YouTubeWatchStats](https://github.com/SSWConsulting/SSW.YouTubeWatchStats) | [2026-06-26 commit](https://github.com/SSWConsulting/SSW.YouTubeWatchStats/commit/3ca98f2ae91499e8f4be319d3e8eebdfd7e0bab9) | Uses a persistent logged-in browser to collect roughly 30 days of YouTube history. | Useful as a current browser-automation example. Its watch-time figures are estimates based on video length rather than measured personal playback time. |
| [pytubefix](https://github.com/JuanBindez/pytubefix) | [2026-07-29 commit](https://github.com/JuanBindez/pytubefix/commit/eeae13f5ee6dfb0e43124b654023ce93a3ac3bb5) | OAuth access to private videos and playlists. | A useful Python-native extractor. It advertises playlist `last_updated`, but not account history, playlist creation time, item-added time, or playback progress. |

### Supporting documentation

- YouTube.js: [authentication](https://ytjs.dev/guide/authentication),
  [Innertube API](https://ytjs.dev/api/classes/Innertube), and
  [`ThumbnailOverlayResumePlayback`](https://ytjs.dev/api/youtubei.js/namespaces/YTNodes/classes/ThumbnailOverlayResumePlayback).
- Kaset: [YouTube mode architecture](https://github.com/sozercan/kaset/blob/main/docs/youtube.md)
  and [overall API architecture](https://github.com/sozercan/kaset/blob/main/docs/architecture.md).
- ytmusicapi: [library reference](https://ytmusicapi.readthedocs.io/en/stable/reference/library.html)
  and [playlist reference](https://ytmusicapi.readthedocs.io/en/stable/reference/playlists.html).
- yt-digest: [data-collection specification](https://github.com/corca-ai/yt-digest/blob/main/docs/spec.md).
- pytubefix: [quickstart and OAuth documentation](https://pytubefix.readthedocs.io/en/latest/user/quickstart.html).

## Secondary candidates and comparison projects

| Candidate | Recent maintenance evidence | Advertised capabilities | Relevance and limitation |
|---|---|---|---|
| [WatchProof](https://github.com/nuthanm/WatchProof) | [2026-07-02 commit](https://github.com/nuthanm/WatchProof/commit/d94d3bc9206039c09c1efd972411b48d3d5a5638) | Local browser extension recording 30-second playback segments, seeks, sessions, timestamps, and page visibility. | Excellent prospective collection of actual viewing behavior, but it cannot reconstruct history from before installation. |
| [TubeArchivist](https://github.com/tubearchivist/tubearchivist) | [2026-07-27 commit](https://github.com/tubearchivist/tubearchivist/commit/e5b802e2f2324179b64bf223046b3391632c3dcd) | Uses yt-dlp metadata, supports cookie forwarding and subscriptions, and maintains local watched/unwatched state. | Valuable integration architecture, although its watched state is primarily local rather than imported YouTube account history. |
| [Metrolist](https://github.com/MetrolistGroup/Metrolist) | [2026-07-29 commit](https://github.com/MetrolistGroup/Metrolist/commit/e2b6a7ce35c0c5e3e797378228f084a4c13052de) | YouTube Music login and synchronization of songs, artists, albums, and playlists. | Another maintained InnerTube implementation to compare with ytmusicapi. It does not advertise timestamp-rich export. |
| [NewPipe Extractor](https://github.com/TeamNewPipe/NewPipeExtractor) | [2026-05-23 release commit](https://github.com/TeamNewPipe/NewPipeExtractor/commit/df389f5) | Public extraction of videos, channels, playlists, comments, and continuations without a Google account. | Useful for comparing public metadata and playlist completeness, but not for signed-in personal account data. See also the [NewPipe capability description](https://github.com/TeamNewPipe/NewPipe). |
| [FreeTube](https://github.com/FreeTubeApp/FreeTube) | [2026-07-29 commit](https://github.com/FreeTubeApp/FreeTube/commit/04b4e11236b145420f4e11a81f22b087a6ad1ada) | Built-in or Invidious extraction with local subscriptions, playlists, and history. | Its local data model and import/export paths may be instructive, but it intentionally avoids YouTube login and cookies. |
| [Invidious](https://github.com/iv-org/invidious) | [2026-07-28 commit](https://github.com/iv-org/invidious/commit/9d1291a0b812683a4f6671e0a3a56f9e3535a2c3) | YouTube metadata plus Invidious-local accounts and playlists. | A useful extraction and local-account comparison, not a source of an existing Google account's private data. |
| [Piped](https://github.com/TeamPiped/Piped) | [2026-07-06 commit](https://github.com/TeamPiped/Piped/commit/335b10d0c02e407b4ba9113e32912b0d783ad455) | Piped-local accounts and synchronization of history and playlists with clients such as LibreTube. | Relevant as a self-hosted data model, but not as a collector for existing private YouTube account data. |

## Preliminary metadata map

| Target metadata | Most promising source | Preliminary evidence |
|---|---|---|
| Date a video was added to a playlist | YouTube Data API; Data Portability API | The Data API exposes [`playlistItem.snippet.publishedAt`](https://developers.google.com/youtube/v3/docs/playlistItems). The [Data Portability YouTube schema](https://developers.google.com/data-portability/schema-reference/youtube) explicitly lists `Playlist Video Creation Timestamp`. |
| Playlist creation date | YouTube Data API; Data Portability API | The Data API exposes [`playlist.snippet.publishedAt`](https://developers.google.com/youtube/v3/docs/playlists). Data Portability lists `Playlist Create Timestamp`. |
| Playlist last-update date | Data Portability API; page-derived extractors | Data Portability explicitly lists a playlist update timestamp. yt-dlp and pytubefix may expose page-derived update information, but its meaning and precision require verification. |
| Exact watch-event time | Direct signed-in My Activity structured pages; Takeout until continuation backfill reaches the historical end | Live validation found a distinct token and Unix-microsecond timestamp per watched event. The collector follows the current framed continuation RPC, and the SQLite importer reconciles My Activity, Takeout, and date-level YouTube occurrences without discarding source identity. |
| Channel subscription time | YouTube Data API | `subscription.snippet.publishedAt` is the creation time for a subscription returned by an authorized `subscriptions.list(mine=true)` request. |
| Historical progress or resume position | yt-digest; YouTube.js; direct history-page extraction | Both yt-digest's advertised output and YouTube.js's resume-overlay model suggest that percentage or resume state is exposed in unofficial signed-in surfaces. Completeness and semantics require live verification. |
| Accurate seconds or watched segments | WatchProof or similar local instrumentation | Prospective browser instrumentation can observe actual playback and seeking behavior more accurately than estimating from video length. |
| YouTube Music play history | ytmusicapi | `get_history` advertises a `played` field. Its exact granularity and consistency need testing. |
| Watch Later and Watch History through the YouTube Data API | Not available | Google's [Data API revision history](https://developers.google.com/youtube/v3/revision_history) documents the removal of access to Watch History and Watch Later through the API. |
| Aggregate viewing percentages for channel owners | YouTube Analytics API | The [Analytics metrics](https://developers.google.com/youtube/analytics/metrics) include creator-facing aggregate metrics, not a viewer's private per-video progress. |

## Recommended implementation drill-down

The next phase should investigate these sources in roughly this order:

1. **YouTube Data API collector** — authorize read-only access; ingest
   subscriptions, owned playlists, and playlist items with their `publishedAt`
   values.
2. **YouTube.js** — trace the history response models, continuations, resume
   overlays, timestamps, and authenticated completeness guards.
3. **yt-dlp** — inventory the actual output fields for its signed-in special feeds
   and compare them with direct playlist extraction.
4. **Kaset** — compare its authenticated `WEB` request profiles, parser fixtures,
   continuation handling, and capability boundaries with YT Library's direct web
   extraction before implementing unfamiliar YouTube surfaces.
5. **ytmusicapi** — determine the precision and semantics of the `played` value
   and whether undocumented playlist timestamps are present in raw responses.
6. **google_takeout_parser** — map supported Takeout formats, timestamp
   preservation, deduplication behavior, and playlist coverage.

Across all candidates, the drill-down should explicitly test:

- Continuation handling and completeness for playlists and history.
- Hidden, deleted, unavailable, or private videos.
- Locale and account-layout differences.
- Whether a timestamp is authoritative, display-derived, inferred, or local.
- Whether watch progress is an exact playback position, a rounded percentage,
  an estimated duration, or merely a watched/unwatched flag.
- The behavior of cookies, OAuth clients, bot checks, and authenticated YouTube
  client identities.

## Caveats

- Most candidates remain at advertised-capability depth. Google My Activity and
  `yt-digest` received targeted live/code validation as described above.
- Login support can be affected by cookie freshness, client identity, bot
  challenges, and network reputation even when a project nominally supports
  authentication.
- Cookies, OAuth tokens, browser profile directories, Takeout archives, and
  exported personal data should remain local-only and must not be committed.
- Creator analytics and an individual viewer's playback history are distinct
  data domains; aggregate creator metrics do not substitute for personal watch
  progress.
- Small projects were retained when their advertised behavior was unusually
  relevant. Repository size or popularity was not used as an exclusion criterion.
