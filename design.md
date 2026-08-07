# YT Library Manager Design Notes

## Product Direction

YT Library Manager is a local-first tool for understanding and managing a personal YouTube library. It mirrors playlist structure, enriches videos with metadata and cached images, reconciles hidden or deleted videos from Takeout and Archivarix evidence, and exposes searchable history and playlist views through a small web UI.

The project intentionally favors YouTube web-interface data where practical, using cookies from the local project directory. The YouTube API or third-party libraries should be fallback tools when the web surface cannot provide the needed data.

The core product goal is not to become a downloader or media server. It is a personal library-management layer that makes YouTube account state easier to inspect, search, preserve, and reconcile. Downloading may be adjacent later, but the current center of gravity is metadata, organization, history, and evidence.

This project is still in an early alpha stage. Prefer the design that clarifies the domain and future maintenance, even when that means a large schema, API, UI, or architecture change. Avoid preserving awkward legacy shapes just because they already exist; source evidence and personal data should be protected, but the application structure is still allowed to move.

## System Shape

The app is intentionally compact but no longer single-file. `yt_library_manager.py` is the compatibility CLI shim, while the application code is split across a small Python package:

- `yt_library/cli.py` defines CLI commands and keeps existing command names stable.
- `yt_library/database.py` owns SQLite connection, schema bootstrap, and
  versioned migrations. `yt_library/core.py` owns importers, parsers, metadata
  fetchers, queue helpers, and reconciliation logic.
- `yt_library/server.py` owns HTTP routing and local API endpoints.
- `yt_library/workers.py` owns in-process worker orchestration.
- `yt_library/queries.py` owns read models for browser and history views.
- `yt_library/schema.sql` is the canonical SQLite schema for fresh local databases.
- `yt_library/templates/` contains the browser and admin HTML plus shared
  browser-side modules for timezone, card rendering, History workflow state,
  and Admin request transport.

Primary surfaces:

- `/` normalizes to `/search`.
- `/search` is the unscoped omni-search across videos, clips, playlists, and channels.
- `/videos`, `/clips`, `/playlists`, and `/channels` are canonical category-scoped search views. Query parameters refine text, fields, facets, sort, and pagination; they do not redefine the path's entity scope.
- `/history` is the separate occurrence view. It preserves repeated watches, occurrence ordering, pagination, and the daily heatmap instead of collapsing videos into canonical search results.
- `/videos/{id}`, `/clips/{id}`, `/playlists/{id}`, and `/channels/{id}` are detail views. Playlist details reuse the video facet model for their member list, while channel details retain their playlist and History tabs.
- `/admin`: status dashboard and worker control plane for metadata, playlist scans, placeholder recovery, and history.

Before 1.0, replaced hash routes and obsolete named-view URLs have no compatibility aliases. After 1.0, URL changes should preserve or deliberately migrate public links.

Omni-search uses `/api/search` as its single read model. The server applies title/description and source filters, folds playlist and history evidence into one canonical video result, includes unresolved unavailable memberships, globally sorts and counts videos/channels/playlists, and only then returns the requested page. The browser does not merge a separate history result set.

The main browser is also view-driven. `/api/bootstrap` returns only navigation structure and aggregate counts; playlist, video, channel, and detail endpoints fetch the current view's rows from SQLite with server-side filtering, sorting, and pagination. The browser caches completed request keys for navigation within the session and preserves the currently rendered view while a new page is loading. It does not download a whole-library metadata snapshot during startup.

### Browser routes and search context

The browser path is the authoritative search context. `/search` can enable or disable any result category. A scoped path always searches only its category, even if query parameters are copied or edited. Default and path-implied values should be omitted from generated URLs.

The left sidebar has three distinct responsibilities:

- **Search in** selects searchable fields such as titles, descriptions, and plugin-provided text fields.
- Category sections own their facet trees. Video availability, reactions, completion, playlist membership, uploader category, and plugin video facets live under Videos; clip ownership lives under Clips; playlist availability and ownership live under Playlists; and channel subscription and status live under Channels. There is no separate **Search for** block.
- Category and group names are real links. Plain clicks use in-app navigation, while browser link affordances such as status-bar targets and open-in-new-tab remain available.

On `/search`, each category row is both the category link and the parent selector for its facets. On a scoped category page, the path supplies that parent selection, so the redundant category checkbox is omitted and only the applicable facets are shown. Playlist detail uses the same scoped video facets with the placeholder **Search this playlist**. History hides the facet trees and leaves the search box available; entering a query returns to `/search`. Detail navigation retains the last omni-search URL in session state so returning to Search restores the prior query, counts, filters, sort, and page.

Named sidebar shortcuts such as Liked, Playlisted, Subscribed, and Terminated are represented by category facets rather than separate view implementations. Playlist and channel group trees remain real scoped navigation because they carry user-defined membership context.

Global History and channel-scoped History use the same browser workflow for
page and activity loading, occurrence-card rendering, pagination, adjacent-page
prefetch, and stale-generation rejection. `history-workflow.js` also owns the
transaction boundary for heatmap year, Today, and Sync changes so a failed or
superseded transition restores the complete prior date, page, and sync state.

Admin actions that send URL-parameter POST requests share one JSON/error
transport. Callers continue to own their distinct user-interface policy:
ordinary actions refresh status and schedule follow-up polling, queue stop
updates its controls before the request, and restart-sensitive settings wait
for a replacement service. Cookie replacement remains a separate raw-text
request because its body and security handling are meaningfully different.

SQLite is the source of truth for local state. Cached thumbnails and avatars are derived local assets. Cookie files, Takeout zips, databases, logs, and thumbnail folders are private runtime data and should remain uncommitted.

## Plugin Architecture And Developer Contract

YT Library supports optional, separately packaged Python plugins. The current
host contract is Python plugin API version 2. Browser extensions use a separate
browser API version 1. Both versions are exact compatibility checks, not version
ranges.

A breaking Python contract change must bump `PLUGIN_API_VERSION`; a breaking
browser registration or host-object change must independently bump
`window.YTLibraryBrowserPlugins.apiVersion`. Update host contract tests and
reference plugins in the same development slice as either change.

This section is the handoff for designing and implementing another plugin. The
authoritative implementation is `yt_library/plugins.py`; HTTP integration lives
in `yt_library/server.py`, queue dispatch lives in `yt_library/workers.py`, and
the browser host lives in `yt_library/templates/index.js`. YT Subtitles is the
reference implementation in the sibling `YT Subtitles` repository, especially
`yt_subtitles/plugin.py`, `yt_subtitles/browser.js`, and `pyproject.toml`.

### Architectural boundary

The plugin system must preserve these properties:

- YT Library has no import, package, schema, database, or startup dependency on
  a plugin. Removing the plugin package must leave normal YTL behavior intact.
- A plugin is its own project and owns its domain data, schema, migrations,
  configuration, caches, and source artifacts. It must not add domain tables to
  the YTL database or ask YTL to migrate or write its database.
- YTL owns only host operational state: plugin activation, common queue rows,
  worker runs, worker logs, lifecycle dispatch, and generic UI placement.
- Integration uses stable identifiers and bounded data returned through the
  contract. Video-oriented plugins join by YouTube video ID; playlist-oriented
  plugins join by YouTube playlist ID.
  Do not create cross-database foreign keys or attach a plugin database to YTL.
- YTL core code must remain domain-neutral. Plugin-specific terms, queries,
  markup, and styles belong in the plugin. If a new host hook is required, add
  a generic capability that another plugin could also use.
- Installing and enabling are separate. Installation only makes an entry point
  discoverable; activation requires an explicit YTL configuration entry.
- Plugin discovery, startup, status, request, planning, execution, asset, and
  shutdown failures are contained. A broken optional plugin must not prevent
  YTL from starting or rendering its core views.

### Generic host rule

Every concept that exists only because a plugin is installed is plugin-owned.
This includes domain labels, default or catch-all groups, matching and
classification rules, facets, badges, views, actions, API vocabulary, and
configuration. The plugin declares those concepts through the versioned
contract; YTL may validate, namespace, place, persist generic UI state for, and
resolve them against canonical YTL identifiers.

Core YTL must not create or preserve plugin semantics in native importers,
discovery paths, tables, rows, configuration keys, routes, queries, templates,
or styles. Disabling or uninstalling a plugin must remove its domain behavior
and presentation without a plugin-specific condition in core and without
requiring core data to stand in for the absent plugin.

A host change for a plugin feature is valid only when it is expressed entirely
in domain-neutral terms and is reusable by an unrelated plugin. The contract
defines generic inputs, bounds, validation, containment, and outputs. The
plugin supplies the domain name, labels, policy, and data. If a proposed YTL
change cannot be specified and tested without naming the requesting plugin or
its domain, implement it in the plugin instead.

Host contract tests use generic fake plugins and prove behavior without loading
a real plugin package. Plugin repositories separately test their declarations
against the public contract. YTL must not import a plugin implementation merely
to test or support it.

Removing historical coupling may require a bounded, versioned YTL migration
that deletes obsolete core-owned plugin artifacts while preserving canonical
library data and unrelated user state. Such a migration is compatibility
cleanup, not permission to retain plugin-specific runtime behavior. After the
migration, no active YTL path may recreate or depend on those artifacts.

The plugin object is loaded into the YTL service process and may be called from
HTTP request threads and background worker threads. Its status, definitions,
filters, projections, and caches must therefore be inexpensive, deterministic,
and thread-safe. Expensive scans should be materialized in the plugin's own
store or cached behind a revision marker rather than repeated per request.

### Packaging, discovery, and activation

Plugins are discovered through the `yt_library.plugins` Python entry-point
group. The entry-point name, configured key, and the object returned as
`plugin_id` must match and must satisfy `^[a-z][a-z0-9_-]*$`.

A minimal `pyproject.toml` contains:

```toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "yt-example"
version = "0.1.0"
requires-python = ">=3.12"

[project.entry-points."yt_library.plugins"]
example = "yt_example.plugin:create_plugin"

[tool.setuptools]
packages = ["yt_example"]

[tool.setuptools.package-data]
yt_example = ["browser.css", "browser.js"]
```

The plugin must be installed into the same Python environment that runs YTL.
For local development, use the YTL virtual environment rather than creating or
invoking a separate plugin environment:

```powershell
$python = "C:\Users\michael.keenan\personal\YT Library\.venv\Scripts\python.exe"
& $python -m pip install -e "C:\path\to\YT Example"
```

YTL configuration is keyed by plugin ID. The host interprets `enabled`; all
other fields are opaque plugin configuration copied into `PluginContext`:

```json
{
  "plugins": {
    "example": {
      "enabled": true,
      "config": "../YT Example/yt_example.config.json"
    }
  }
}
```

Boolean shorthand is accepted (`"example": true`), but the object form is
preferred because it can carry a pointer to plugin-owned configuration. Paths
should be resolved with `context.resolve_path(...)`; relative values are based
on the directory containing `yt_library.config.json`, not the process working
directory. `context.root` is the YTL repository root, `context.config_path` is
the active YTL config path, `context.plugin_id` is the configured ID, and
`context.plugin_config` is a copy of that plugin's settings.

On service startup, YTL follows this lifecycle:

1. Retain a status record for every valid configured plugin.
2. Do not discover or import a disabled plugin. Its state is `disabled` and it
   remains visible in Advanced Admin so it can be re-enabled.
3. For an enabled plugin, require exactly one installed entry point with the
   configured name.
4. Load its zero-argument factory, create the plugin object, and validate its
   ID, Python API version, browser assets, and worker definitions.
5. Call `start(context)` and contain any exception as plugin state `error`.
6. On each status response, call `status()` and expose it as `pluginStatus`.
7. On service shutdown, call `shutdown()` in reverse load order and suppress a
   shutdown failure so core shutdown can finish.

Missing, duplicate, incompatible, or failing entry points are reported as
`missing`, `error`, or `incompatible`; the host continues running. Enabling or
disabling from Advanced Admin writes YTL config and restarts the service. The
host refuses that state change while any worker is active. Pending plugin queue
rows are retained while disabled and become eligible again after re-enabling.

A ready plugin's generic status record contains `id`, `name`, `enabled`,
`state`, `version`, `apiVersion`, sorted `capabilities`, optional
`browserAssets`, optional `workerProcesses`, and the plugin's complete status
object under `pluginStatus`. Unavailable records instead retain `id`, configured
`name`, `enabled`, `state`, and `message`. Browser code should consume this
generic shape and keep domain-specific details nested under `pluginStatus`.

### Python plugin object

The entry-point factory takes no arguments and returns one object. A minimal
backend-only implementation is:

```python
from __future__ import annotations

from typing import Any


class ExamplePlugin:
    plugin_id = "example"
    plugin_name = "YT Example"
    plugin_version = "0.1.0"
    plugin_api_version = 2
    capabilities = frozenset({"example_status"})
    browser_assets: tuple[dict[str, str], ...] = ()

    def __init__(self) -> None:
        self._ready = False

    def start(self, context: Any) -> None:
        configured = str(context.plugin_config.get("config") or "").strip()
        self._config_path = context.resolve_path(configured) if configured else None
        self._ready = True

    def status(self) -> dict[str, Any]:
        return {"state": "ready" if self._ready else "stopped"}

    def handle_api(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
    ) -> tuple[int, Any] | None:
        if method == "GET" and path == "status":
            return 200, self.status()
        return None

    def shutdown(self) -> None:
        self._ready = False


def create_plugin() -> ExamplePlugin:
    return ExamplePlugin()
```

A complete plugin should provide the following metadata and methods. The host
strictly validates `plugin_id` and `plugin_api_version`; the explicit name,
version, capabilities, API handler, and shutdown method keep status, browser
assets, and failure behavior predictable rather than relying on host fallbacks:

- `plugin_id`: exact entry-point/config ID.
- `plugin_name`: user-facing name.
- `plugin_version`: plugin release string. It is also used to cache-bust browser
  asset URLs.
- `plugin_api_version`: currently exactly `2`.
- `capabilities`: iterable of strings advertised at the top level of plugin
  status. Browser features should declare and check a capability rather than
  infer support from the plugin name.
- `start(context)`: validate plugin-owned configuration and initialize bounded
  resources. Do not start an untracked background loop here.
- `status()`: return a JSON object. Return `state: "ready"` only when advertised
  features are usable. This method is called by browser bootstrap and frequent
  Admin polling, so it must not scan a large database.
- `handle_api(method, path, query)`: return `(HTTP status, JSON-serializable
  payload)` or `None` for an unknown route.
- `shutdown()`: close plugin-owned resources and make future accidental calls
  harmless.

Optional features add `browser_assets` and `handle_browser_asset`,
`filter_videos`, `project_videos`, `project_playlist_groups`,
`project_channel_groups`, or the worker
methods described below. A plugin can avoid importing YTL at runtime by using
`Any` or plugin-owned typing protocols for host contexts. The dependency
direction remains plugin to host; YTL must never import the plugin package
directly.

### Namespaced backend HTTP API

The current generic HTTP surfaces are:

- `GET /api/plugins` returns all configured plugin status records.
- `GET /api/plugins/{plugin_id}/{path}` delegates to `handle_api`.
- `GET /plugins/{plugin_id}/assets/{path}` delegates declared browser assets.
- `/api/bootstrap` and `/api/admin/status` include the same generic plugin
  status records.

Custom plugin API routes are GET-only in the current contract. `path` has no
leading slash. Query parameters come from `urllib.parse.parse_qs`, so every
present parameter maps to a list of strings. The plugin should parse, validate,
and bound every parameter, explicitly cap pagination and batch IDs, and return
400 for invalid input. Query values are URL-decoded, but plugins should
explicitly use `urllib.parse.unquote` for dynamic route segments. Returning
`None` produces 404. Plugin exceptions are contained and returned as 503
without taking down the request handler.

Do not use browser API routes for arbitrary mutation. Long-running or mutating
operations belong in the host-owned worker queue. If a future plugin requires a
different mutation primitive, extend the host with a generic reviewed contract
instead of adding a plugin name to `server.py`.

### Browser assets and browser API version 1

A plugin declares assets as dictionaries containing `path` and `type`:

```python
from pathlib import Path

browser_assets = (
    {"path": "browser.css", "type": "style"},
    {"path": "browser.js", "type": "script"},
)

def handle_browser_asset(self, path: str) -> tuple[str, bytes]:
    content_types = {
        "browser.css": "text/css; charset=utf-8",
        "browser.js": "text/javascript; charset=utf-8",
    }
    return content_types[path], Path(__file__).with_name(path).read_bytes()
```

Asset types are only `style` and `script`. Paths must start with an alphanumeric
character, contain only letters, digits, `.`, `_`, `/`, or `-`, and may not
contain a `..` segment. Only declared exact paths are served. Bodies may be
bytes or text. YTL sends `no-cache`, and appends `plugin_version` to asset URLs
for cache busting.

Assets load only when the plugin is enabled, its effective state is `ready`,
and the asset declaration validates. Plugin scripts run in the YTL page and are
therefore trusted local code. They must namespace their CSS classes, avoid
unnecessary globals, tolerate missing DOM elements, and contain their own
failures. Plugin CSS and JavaScript belong in the plugin package, not YTL's
templates.

The browser script registers synchronously when loaded:

```javascript
(() => {
  'use strict';
  const api = window.YTLibraryBrowserPlugins;
  if (!api || api.apiVersion !== 1) return;

  api.register({
    id: 'example',
    videoDetail: {
      capability: 'example_detail',
      render: async (videoId, host) => {
        if (!host.supports('example_detail')) return null;
        const payload = await host.requestJson(
          `videos/${encodeURIComponent(videoId)}`,
        );
        const panel = document.createElement('article');
        panel.className = 'card example-panel';
        panel.textContent = String(payload.summary || '');
        return panel;
      },
    },
  });
})();
```

The registered `id` must equal the backend plugin ID. Duplicate or invalid IDs
are rejected. A browser feature should name a backend capability; the host
enables it only when the plugin is enabled, ready, and advertises that
capability.

The browser `host` object passed to extension functions contains:

- `pluginId`: current plugin ID.
- `status`: current generic status record, including `pluginStatus`.
- `supports(capability)`: readiness and capability check.
- `requestJson(path, params)`: namespaced GET request to the plugin. Scalar or
  array values become query parameters; non-2xx responses throw.
- `libraryVideos(videoIds)`: bounded lookup of canonical YTL video summaries;
  the host batches requests in groups of 100 and returns a `Map` keyed by ID.
- `ui.createSearchVideoCard(video, options)` and
  `ui.createVideoCard(video, options)`: shared card construction.
- `ui.escapeHtml(value)` and `ui.localVideoHref(videoId)`.
- `ui.searchHighlight.textHtml(text, query)`: escaped full text with matches.
- `ui.searchHighlight.excerptHtml(text, query, options)`: escaped excerpt
  centered on the first match; `before` and `after` are optional lengths.
- `ui.searchHighlight.snippetHtml(snippet)`: escapes everything and restores
  only literal `<mark>`/`</mark>` delimiters as YTL search-highlight markup.

Use `textContent` for ordinary plugin data. When markup is required, use host
escaping and highlighting helpers. Never insert an untrusted API string through
raw `innerHTML`.

### Native entity-card extensions

Browser API version 1 includes the additive `entityCards` feature. Plugins
should feature-detect it with `api.features?.entityCards === 1`; the browser API
version remains 1 because plugins that use only the earlier optional `search`
and `videoDetail` surfaces continue to register unchanged.

`entityCards` decorates native cards without requiring the plugin to implement
search. It is capability-gated and supports the canonical native kinds
`video`, `clip`, `playlist`, and `channel`:

```javascript
(() => {
  'use strict';
  const api = window.YTLibraryBrowserPlugins;
  if (!api || api.apiVersion !== 1 || api.features?.entityCards !== 1) return;

  api.register({
    id: 'example',
    entityCards: {
      capability: 'example_cards',
      kinds: ['video', 'clip'],
      prepare: async (entities, host, context) => {
        const ids = entities.map(entity => entity.id);
        return host.requestJson('card-summaries', {
          id: ids,
          view: context.view,
        });
      },
      render: (entity, preparedState, _host, context) => {
        const summary = preparedState?.summaries?.[entity.id];
        if (!summary) return null;
        const action = document.createElement('button');
        action.type = 'button';
        action.textContent = String(summary.actionLabel || 'Open');
        action.dataset.view = context.view;
        const metadata = document.createElement('span');
        metadata.textContent = String(summary.label || '');
        return {
          actions: [action],
          secondaryMetadata: [metadata],
        };
      },
    },
  });
})();
```

Each descriptor is a frozen `{kind, id, item}` object. `id` is the canonical
YT Library identity (`video_id`, `clip_id`, `playlist_id`, or `channel_id`), and
`item` is the bounded read model already used to render that card. Cards without
a canonical ID are not offered to extensions. `context` is a frozen
`{view, layout}` object. Current views include `search`, `playlist`, `history`,
`channel-history`, `channel-playlists`, `video-detail`, and `clip-detail`; layout
is `grid`, `compact`, or `detailed` where the view supports it.

The host deduplicates descriptors by kind and ID, then calls `prepare` at most
once per plugin for the rendered batch. A repeated history occurrence is still
rendered separately, but it does not cause another preparation request. Do all
bounded I/O in `prepare`; `render` must return synchronously with `actions` and
`secondaryMetadata` arrays containing plugin-owned `HTMLElement` instances, or
return `null`. Actions are placed beside native title actions. Secondary
metadata follows native facts such as uploader category and precedes native
descriptions and source lists.

Plugins compose in browser registration order. The host wraps contributions by
plugin ID, replaces a plugin's previous contribution on re-decoration, contains
preparation and per-card rendering failures, and rejects stale asynchronous
work after navigation. Readiness and the declared capability are checked before
preparation, so disabled, unavailable, and capability-missing plugins do not
decorate. One plugin's asset, preparation, or rendering failure does not block
native cards or another plugin.

The older `search.decorateCoreResults` and
`search.decorateCoreResultCard` hooks remain supported for query-specific read
model decoration and presentation. The per-card compatibility hook now runs
through the same card batch path as `entityCards`. Existing
`videoDetail: {capability, render}` panels also remain independent and supported.

### Search, facets, cards, and virtual videos

There are two browser search patterns. Choose one deliberately.

The preferred pattern for data associated with videos is a video facet. It
keeps the result a canonical YTL video while allowing plugin presence and text
matches to participate in the host query:

```javascript
api.register({
  id: 'example',
  search: {
    capability: 'example_search',
    label: 'Example data',
    searchField: {
      key: 'example',
      label: 'Example data',
      defaultEnabled: true,
    },
    videoFacet: {
      presentLabel: 'has example data',
      absentLabel: 'no example data',
      presentHashParam: 'with-example',
      absentHashParam: 'without-example',
    },
    forceRelevance: 'query',
    catalogCount: status => Number(status?.pluginStatus?.itemCount || 0),
    decorateCoreResults: async (results, host, {query}) => {},
    decorateCoreResultCard: (card, result, host) => {},
  },
});
```

`searchField` adds a checkbox under **Search in**. Its key follows the plugin ID
syntax and must be unique. `videoFacet` adds a facet under the Videos section,
with independently selectable present and absent values. Both
are enabled on a fresh search by default, so simply installing a plugin does
not narrow the core video set. Optional
`presentDisabledPreferenceKey`/`absentDisabledPreferenceKey` values may make
those disabled states persist; omit them when the facet should reset on reload.
Saved keys must follow YTL's plugin preference namespaces:
`plugins.{plugin_id}.search` or
`plugins.{plugin_id}.filters.{lowercase_key}`. A separate, non-facet plugin
search kind starts disabled unless its valid `preferenceKey` is enabled in YTL
filter preferences.

The matching backend method is:

```python
def filter_videos(self, query: str) -> dict[str, object]:
    return {
        "video_ids": all_video_ids_with_plugin_data,
        "search_match_ids": matching_video_ids if query.strip() else (),
    }
```

Both values must be iterable collections of nonempty IDs, not strings or
mappings. `search_match_ids` must be a subset of `video_ids`. The host may call
this for facet membership with an empty query, for text matching with the
current query, or both. Cache the complete membership behind a cheap plugin
revision and use an index for query matches. Returning hundreds of thousands of
IDs is supported, but building them by scanning large text or relational tables
on every search is not.

The host applies the returned sets to its normal search model, preserves native
availability/reaction/completion/membership filtering, computes stable present
and absent facet counts, and annotates page results with:

- `result.pluginFacets[plugin_id]`: whether the video has plugin data.
- `result.pluginSearchMatches`: plugin IDs whose text matched the query.

`decorateCoreResults` can batch-fetch display details after the core page is
known. `decorateCoreResultCard` can then add a badge or replace the displayed
description with a match snippet. The result remains a video and should retain
the host's normal video-card semantics.

The second pattern is a separate result type. Use it only when results are not
best represented as canonical videos. Omit `videoFacet`, implement
`search.fetch({query, limit, offset}, host)`, and return:

```javascript
{
  total: 123,
  totalIsExact: true,
  results: [/* bounded plugin-owned result objects */],
}
```

Implement `search.renderResult(item, host)` to return an `HTMLElement`.
`fetchEmptyQuery: true` opts into empty-query fetches. `preferenceKey` can make
the separate search kind an opt-in saved filter. The host composes plugin and
core pagination, surfaces request failures without failing core search, and
uses `catalogCount` for unloaded counts. The plugin owns the ordering within
its separate result page. A plugin that participates in text relevance should
set `forceRelevance` to `true` or `"query"`; otherwise users retain all normal
sort options.

A video-oriented plugin may expose read-only virtual videos for IDs absent from
YTL:

```python
def project_videos(self, requested_video_ids: frozenset[str]):
    return [
        {"video_id": video_id, "title": title_snapshot}
        for video_id in requested_video_ids
        if video_id in self_catalog
    ]
```

The response must be an iterable of unique objects containing a requested,
nonempty `video_id` and optional `title`. Returning an unrequested or duplicate
ID is a contract error. YTL combines projections from ready plugins, renders a
virtual video with an unknown availability and `Not in library` badge, and may
use it for search or `/api/videos/{video_id}` detail fallback. It never inserts
or hydrates that projection into the YTL database.

`videoDetail: {capability, render}` adds a lazy plugin-owned panel to video
detail. `render(videoId, host)` returns an `HTMLElement` or `null`. Failures are
contained so the core detail card remains usable. Fetch large data only when
the user expands or requests it, paginate it, and avoid loading full transcripts
or other large payloads during the initial detail render.

### Navigation-group projections

A ready plugin advertising `playlist_groups` must implement
`project_playlist_groups()` and return one bounded current-state projection:

```python
{
    "revision": "optional-plugin-revision",
    "groups": [
        {
            "group_key": "local-key",
            "name": "Group name",
            "parent_key": None,
            "position": 0,
            "icon": None,
        }
    ],
    "memberships": [
        {"group_key": "local-key", "playlist_id": "PL...", "position": 0}
    ],
}
```

The host accepts at most 10,000 groups and 250,000 memberships, requires unique
group keys and group/playlist pairs, validates a cycle-free parent hierarchy,
and rejects invalid projections as a contained plugin error. It namespaces keys
as `plugin:<plugin_id>:<local-key>` so plugin and native groups cannot collide.
Names, keys, optional icons, and revision markers are length-bounded.

A plugin may mark exactly one group in a projection with
`"include_unmatched": True`. That group must not declare explicit memberships.
When YTL supplies its bounded set of canonical identifiers to the projection,
the generic host derives memberships for identifiers absent from every explicit
group in that plugin projection. The plugin owns the group key, name, and rule;
the host owns only validation and set resolution. Without a canonical identifier
set, no derived memberships are emitted.

`/api/bootstrap` merges valid projected groups with native groups and retains
memberships only for playlist IDs already present in YTL. Selecting a projected
group resolves its descendants and passes the resulting explicit playlist-ID
set through the normal playlist search model. Missing playlist IDs are not
inserted into YTL, and the plugin never receives a YTL database connection.
Projection failures appear in bootstrap diagnostics without preventing native
navigation or search.

The parallel `channel_groups` capability requires `project_channel_groups()`
with the same group shape and memberships containing `channel_id` instead of
`playlist_id`. The host namespaces these keys as
`plugin-channel:<plugin_id>:<local-key>`, retains memberships only for canonical
channels already present in YTL, and renders the complete validated hierarchy
under Channels. Selecting a group includes all descendants and filters the
normal channel search model through an explicit channel-ID set. Unknown channel
references remain plugin-owned and never create YTL rows.

Native and projected navigation hierarchies share one disclosure model. Parent
rows expose a separate accessible toggle, while the group label remains the
search action. Only collapsed node IDs are saved in config, so new groups are
expanded by default and no plugin-specific state enters the core contract.

### Host-owned worker queue

A plugin may declare worker processes through a `worker_processes` iterable or
zero-argument method. If any processes are declared, `plan_worker` and
`run_worker` are required. The plugin does not create its own queue tables or
untracked host threads.

```python
def worker_processes(self):
    return (
        {
            "id": "fetch",
            "name": "Fetch example data",
            "description": "Queue one bounded retrieval per eligible video.",
            "service": "youtube",
            "max_in_flight": 4,
            "admin_surface": "advanced",
            "button_label": "Fetch example data",
            "confirm": "Queue retrieval for all eligible videos?",
            "hooks": ("video_scan",),
            "admin_actions": (
                {
                    "id": "fetch-video",
                    "placement": "videos",
                    "surface": "advanced",
                    "button_label": "Fetch example data",
                    "inputs": (
                        {
                            "name": "video_id",
                            "label": "Video ID",
                            "placeholder": "11-character YouTube ID",
                            "required": True,
                            "max_length": 11,
                        },
                    ),
                },
            ),
        },
    )
```

Process and action rules:

- Process, action, input, hook, and worker outcome IDs must start with a
  lowercase letter, contain only lowercase letters, digits, `_`, or `-`, and be
  at most 80 characters.
- `service` is `local`, `youtube`, or `archivarix`. It classifies capacity; it
  does not provide an HTTP client, cookies, proxy, or retry policy. `youtube`
  and `archivarix` tasks share their respective YTL global in-flight limit.
  Every process also honors its own `max_in_flight`, clamped to 1-100.
- `admin_surface` is `none`, `basic`, or `advanced`. A non-`none` value creates
  a default bulk action in the **Plugins** panel using the process description,
  button label, and confirmation.
- Explicit `admin_actions` use unique IDs, `surface` `basic` or `advanced`, and
  placement `plugin` or `videos`. `plugin` renders in the Plugins panel;
  `videos` renders in Advanced Admin's Videos panel. The Enabled switch itself
  is always an Advanced Admin control.
- Inputs are currently text inputs. Each supports `name`, `label`,
  `placeholder`, `required`, and `max_length` (clamped to 1-2000). Query values
  arrive at `plan_worker` as lists of strings.
- Snake-case and the corresponding camel-case definition keys are accepted,
  but plugin Python should prefer snake_case. If `admin_surface` creates the
  legacy `default` action, do not also declare an explicit action named
  `default`.

The Admin enqueue route is:

```text
POST /api/admin/plugins/{plugin_id}/processes/{worker_id}/enqueue?name=value
```

The endpoint invokes planning inside YTL's queue transaction, records a queue
log, and starts the common dispatcher. Manual actions set the queue row's
`manual` flag. Plugins must not depend on partial generator rollback for direct
Admin actions: validate and preflight the entire request before yielding any
task.

`plan_worker(worker_id, context, params)` receives a bounded, read-only
`PluginPlanningContext`:

- `context.plugin_id` identifies the current plugin.
- `context.library_videos()` streams dictionaries with `video_id`, `title`,
  `availability`, and `is_playable`, ordered by video ID.
- `context.latest_worker_outcomes(worker_id)` returns the most recent run per
  nonempty subject for that plugin and worker, including `outcome`, `status`,
  `finished_at`, and `message`.

Planning must return an iterable of task dictionaries. The host accepts at most
250,000 tasks from one plan. Each normalized task has:

- required `task_id`: stable identity within this plugin and process, truncated
  by the host to 500 characters;
- optional `subject_id`, defaulting to `task_id`, for logs and outcome history,
  truncated to 500 characters;
- optional `video_id`, truncated to 500 characters, and `title`, truncated to
  2,000 characters, for common queue display;
- optional JSON-object `payload`, limited to 64 KiB encoded UTF-8;
- optional integer `priority`, clamped to -1000 through 1000. Lower values run
  first; equal priorities prefer the most recently updated row and then the
  higher queue ID.

YTL deduplicates queued work by
`plugin:{plugin_id}:{worker_id}:{task_id}`. Replanning updates the payload and
subject, retains an existing title unless it was empty, upgrades `manual`, and
keeps the lower/more urgent priority. Use a deterministic `task_id`; do not put
timestamps or random values in it unless repeat work really must coexist.

`run_worker(worker_id, task, runtime)` receives:

```python
{
    "queue_id": 123,
    "subject_id": "...",
    "video_id": "...",
    "title": "...",
    "payload": {},
}
```

`runtime.stop_requested()` is the cooperative cancellation signal. Check it
before remote work and between bounded phases. `runtime.log(level, message,
subject_id="")` writes to YTL's plugin log; customary levels are `debug`,
`info`, `warn`, and `error`. Messages are capped at 10,000 characters and
subject IDs at 500.

Return a result object with an ID-shaped `outcome` and optional nonnegative
`processed`, `found`, `failed`, `skipped`, and `message` values. Defaults are
`outcome: complete` and `processed: 1`. A normal completion removes the queue
row and records the result. A stop request marks the run interrupted and keeps
the task queued. An exception is contained as `worker_error`, logged, and
removes the task; implement explicit retry planning if the domain requires it.
On service startup, stale `running` plugin runs are marked `interrupted`.

YTL persists plugin operational data in `worker_queue`, `plugin_worker_runs`,
and `plugin_worker_log`. Admin status adds `queuedCount`, `runningCount`, and
`latestRun` to each process definition and offers plugin-specific log sources.
Domain payloads still belong entirely to the plugin.

### Lifecycle hooks

Processes opt into hooks by listing their IDs in `hooks`. The current host
events are:

- `library_initialize`: emitted when Admin Initialize plans the first library
  work.
- `library_update`: emitted for manual and scheduled Update planning.
- `video_scan`: emitted when Admin resolves an individual video ID or direct
  video URL through the common add-target control. Channel and playlist targets
  do not emit it.

The planner receives `params["hook"]` plus event parameters. `video_scan`
currently includes `video_id` as a list containing the resolved ID. Hook plans
are wrapped in a savepoint: an exception rolls back that plugin's partial plan,
writes a `queue error` log, and allows core planning and other plugins to
continue. A hook should be an idempotent planning notification, not the work
itself.

### Admin enablement and operational behavior

Advanced Admin contains one **Plugins** panel. Every configured plugin remains
listed there even when disabled or unavailable. Each plugin row contains its
name, an **Enabled** switch, state/message, and any plugin-placement actions.
The panel is Advanced-only unless at least one loaded plugin declares a Basic
action. Video-placement actions appear in the existing Videos panel.

The state endpoint is:

```text
POST /api/admin/plugins/{plugin_id}/enabled?enabled=0|1
```

It operates only on an existing configured plugin, refuses changes while any
worker is alive, saves the current display name for the disabled state, and
restarts the service. Disabling does not delete the plugin's data, queue rows,
run history, or logs. Because the plugin entry point is not loaded while
disabled, its routes, assets, processes, filters, and projections are
unavailable until re-enabled.

### Scale, safety, and failure expectations

- Treat IDs, titles, filenames, and URLs as shell-hostile. Validate IDs in
  Python and use argument arrays or safe library APIs rather than interpolated
  shell commands.
- Bound every API page, ID batch, worker task, payload, and in-memory result.
  Do not return an entire large catalog when the browser only needs one page.
- Maintain constant-time or cached status counters. Admin polls status often.
- Preflight plans before yielding and make queue planning idempotent. A service
  stop, browser retry, or repeated lifecycle hook must not duplicate domain
  data.
- Store exact timestamps in UTC with `Z`. Present them through YTL's configured
  display timezone when using host UI.
- Keep plugin databases, config containing private paths or credentials,
  source captures, logs, and caches ignored in the plugin repository.
- Do not expose secrets in status payloads, queue payloads, log messages, API
  responses, browser code, or committed fixtures.
- Browser errors, API errors, missing plugin records, incompatible schemas, and
  unavailable domain data need explicit empty/error states. Do not fabricate
  YTL rows or erase last useful plugin data to make an error disappear.
- A plugin instance is shared across request and worker threads. Protect
  mutable caches, avoid connection reuse unless the database library permits
  it, and keep each operation's transaction ownership clear.

### Development and verification checklist

Use this sequence when creating or extending a plugin:

1. Create a separate repository and package with its own README, design notes,
   config example, tests, and ignored runtime data.
2. Define domain ownership and the stable join identifier before integration.
   Prefer a plugin-owned database and read-only projections into YTL.
3. Add the entry point and implement lifecycle/status with no browser or worker
   features. Install it editable into the YTL virtual environment.
4. Add the disabled YTL config entry, start YTL, and verify the plugin is listed
   without being imported. Enable it only after standalone startup/status tests
   pass.
5. Add bounded namespaced API routes and unit tests. Keep tests local-only; do
   not require real cookies, network calls, or personal databases.
6. Add browser assets behind explicit capabilities. Test fresh reload, search
   defaults, filter counts, pagination, all card layouts, virtual videos,
   detail loading, missing data, and plugin failure containment.
7. Add worker planning and execution last. Test deterministic deduplication,
   targeted and bulk plans, hook parameters, capacity, recent-outcome policy,
   cooperative stop, errors, logs, and restart recovery.
8. Run YTL's full `py_compile`, `unittest`, Ruff, browser-asset tests, and
   `git diff --check`, plus the plugin's independent suite.
9. Preserve queue state, restart YTL, smoke-test `/api/admin/status` and
   `/api/history/search?limit=1`, then perform live browser QA with the plugin
   enabled, disabled, and re-enabled.
10. Commit coherent changes separately in the YTL and plugin repositories.
    Never commit either project's runtime database or private configuration.

Useful YTL contract tests live in `tests/test_plugins.py`, `tests/test_server.py`,
`tests/test_templates.py`, and `tests/js/browser-assets.test.js`. A host change
must remain generic and should add a fake-plugin contract test. Plugin-specific
behavior belongs in the plugin repository's tests. As a standing guard, YTL's
core query module and browser templates must not acquire plugin-domain terms.

## Data Sources

Source parsers provide evidence for one best-known current state:

- Current YouTube playlist web state: playlist membership, ordering, visible videos, hidden placeholders, playlist metadata, and scan status.
- YouTube live history web state: recent/history ordering, date-level labels, and account-specific thumbnail progress state.
- Takeout history zips: authoritative exported watch timestamps.
- The newest Takeout export: current playlist membership, subscriptions, exact history timestamps, and recovery input.
- YouTube's Liked videos system playlist: full current per-video like state during initialization or an explicit all-playlist scan.
- Archivarix: recovery evidence for deleted or memory-holed videos, including thumbnails, titles, descriptions, channel evidence, archive links, and not-found/deleted status.
- YouTube watch and channel pages: enriched video/channel metadata, thumbnails, channel avatars, watch progress, and raw `LIKE`, `DISLIKE`, or `INDIFFERENT` reaction state when the direct page exposes it. A missing reaction entity does not overwrite prior state.

Each source has different reliability. Takeout is best for exact watch timestamps, current YouTube scans are best for present playlist and metadata state, and Archivarix is best-effort recovery evidence. Source fields are consumed during import instead of being retained as parallel metadata histories.

The optional YT PocketTube plugin owns PocketTube exports, its database, and its
import lifecycle. When separately installed and enabled, it contributes a
read-only playlist- and channel-group hierarchy joined by YouTube playlist or
channel ID. YT Library does not write the plugin database or ingest unmatched
references. The plugin's playlist projection owns the derived `Uncategorized`
rule for canonical YTL playlists absent from every explicit PocketTube group;
YTL owns no PocketTube-specific navigation records. The generic navigation host
nests projected roots beneath a count-free
plugin label in each applicable section. Plugin parents and their descendant
branches use separate disclosure nodes whose collapsed IDs persist through the
normal YTL configuration preference endpoint.

## Storage Model

The database models the best-known current state of YouTube. Imports and scans replace superseded metadata rather than preserving revisions. When content becomes unavailable, the last known useful state is retained so removed content remains identifiable.

- `videos` owns canonical video metadata, current playability, availability, reaction, progress, and fetch state.
- `channels` owns canonical channel metadata and subscription state.
- `playlists`, `groups`, and `group_playlists` model the current library organization.
- `playlist_items` links playlists to videos and retains only membership, position, unavailable-slot, and reconciliation facts.
- `history_events` stores watch events. Exact Takeout timestamps and date-only live observations share this table without fabricating precision.
- `video_recovery` stores only current Archivarix recovery status, capture time, media availability, and errors.
- `worker_queue` stores prioritized account, Clip, metadata, playlist, History,
  recovery, and plugin tasks. Queue events and worker-specific run/log tables
  provide operational history.

Parsers may use titles, channels, descriptions, and URLs transiently to update canonical entities, then discard those source copies. Metadata revisions and complete historical playlist snapshots are intentionally not retained.

Runtime settings, including the display timezone, request launch intervals, concurrency limits, cookie paths, and bind address, live in `yt_library.config.json`, not in SQLite. An empty display timezone is treated as UTC by the server until the browser detects an IANA timezone and saves it through the settings endpoint.

Unknown playlist slots use `NULL` video IDs and structured unavailable state. Stable YouTube video, playlist, and channel URLs are generated from IDs. Wayback links are generated from a video ID plus the retained capture timestamp.

## Worker Model

Long-running and rate-sensitive tasks run as in-process background workers with persistent queue rows, run records, and logs. The unified dispatcher selects the next eligible `worker_queue` row by priority before each launch:

`yt_library/worker_runs.py` owns the common persistence transitions for every
host run family. Workers provide only their validated family-specific fields;
the recorder applies running and terminal statuses, timestamps, progress
updates, atomic counters, and restart interruption while leaving commits and
rollbacks to the surrounding worker transaction. Fetching, queue disposition,
logs, retry policy, and error classification remain owned by each worker.

- Metadata tasks fetch channel pages directly when keyed by channel and watch pages directly when keyed by video. They never use YouTube's search interface as a metadata fallback. Each authenticated request verifies that YouTube still accepts the configured cookie; authentication failure stops further YouTube dispatch.
- Playlist tasks scan playlists with yt-dlp first and fall back to the web parser when needed. They record reported, exposed, and unavailable counts without replacing a fuller scan with a short result.
- Placeholder tasks query Archivarix for deleted/private/unavailable video IDs, persist each recovery attempt and its run-linked logs, and preserve rate-limited tasks for a later retry.
- History tasks support recent fetch and full verification modes, fetching YouTube history in batches and reconciling after each batch.

YouTube metadata and Archivarix recovery have independent launch intervals and `max_in_flight` limits from the config file, so a slow request to one site does not stall the other site's cadence. Playlist and history tasks remain worker-specific and run through the same prioritized queue. The dispatcher checks SQLite again before each launch, so priority changes and newly queued work can affect later dispatches without rebuilding an in-memory batch.

Initialize, Update, and Rebuild use one declarative library queue planner.
Initialize selects a full playlist scan including Liked videos, full History
verification, all Clip discovery, and due metadata without clearing pending
work. Update adds only playlist scans with an integrity signal (never scanned,
failed, or reported-count mismatch), recent History, new Clip and playlist
discovery, and never-fetched metadata. Scan age alone does not make a playlist
due; the explicit Scan all action is the force-refresh path. Update and Rebuild
do not poll Liked videos. Rebuild replaces the regenerable account, History,
metadata, and playlist plan rows, while preserving pending Clip, Archivarix
recovery, plugin, and future non-plan rows. It then applies the due-work plan and
does not start the dispatcher automatically. Plugins receive `library_initialize` and
`library_update` hooks; Rebuild preserves plugin rows because the host contract
does not define a generic rebuild hook.

Workers should be visible and interruptible from `/admin`. Queue counts, previews, timing estimates, stop buttons, and incrementally polled logs are part of the design, not just debugging conveniences. A server restart interrupts active in-process workers, so unfinished metadata, playlist, history, and placeholder recovery runs are marked interrupted during startup.

## UI Goals

The UI should be a dense local operations tool rather than a marketing page.

- Keep primary views immediately useful: unscoped search, scoped category lists, occurrence History, detail views, and the admin dashboard.
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

The watch page may not expose the thumbnail progress overlay for the current video. Metadata refresh leaves progress unknown in that case rather than searching YouTube; playlist and history cards remain the account-specific progress sources and render progress as a thin red thumbnail bar plus a `Watched N%` line.

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
- Treat `schema.sql` as the canonical fresh-install schema and preserve supported
  existing databases through ordered migrations in `database.py`. Every schema
  change must update `SCHEMA_VERSION`, provide a data-preserving migration, and
  include both fresh-bootstrap and upgrade-path tests.
- Keep personal artifacts out of Git: cookies, Takeout zips, SQLite databases, logs, and cached images.
