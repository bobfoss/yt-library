const VideoCard = window.YTLibraryVideoCard;
const CollectionCard = window.YTLibraryCollectionCard;
const badgeRowsHtml = VideoCard.badgeRowsHtml;
const creatorHtml = VideoCard.creatorHtml;
const detailRowHtml = VideoCard.detailRowHtml;
const escapeHtml = VideoCard.escapeHtml;
const membersOnlyIconHtml = VideoCard.membersOnlyIconHtml;
const reactionIconsHtml = VideoCard.reactionIconsHtml;
const reactionLabel = VideoCard.reactionLabel;
const searchHighlight = VideoCard.searchHighlight;
const uploaderCategoryHtml = VideoCard.uploaderCategoryHtml;
const thumbnailWithProgress = VideoCard.thumbnailWithProgress;
const thumbIconHtml = VideoCard.thumbIconHtml;
const watchProgressPercent = VideoCard.watchProgressPercent;
const watchedLineHtml = VideoCard.watchedLineHtml;
const watchSparklineHtml = (video, detail = false) => VideoCard.watchSparklineHtml(video, { detail });
const defaultDocumentTitle = 'YT Library';
const pageConfig = window.YT_LIBRARY_CONFIG || {};
const filterPreferenceKeys = {
  unavailableVideos: 'videos.unavailable',
  lowPartialCompletion: 'completion.partial_below_minimum',
  unavailablePlaylistVideos: 'playlist_videos.unavailable',
  removedPlaylistVideos: 'playlist_videos.removed',
  removedPlaylists: 'playlists.removed',
  terminatedChannels: 'channels.terminated',
};
const filterPreferences = Object.fromEntries(
  Object.entries(pageConfig.filterPreferences || {}).filter(([, enabled]) => enabled === true)
);
const defaultSearchFilterTreeExpanded = [
  'kind:videos',
  'kind:playlists',
  'kind:channels',
];
const searchFilterTreeExpanded = new Set(
  (Array.isArray(pageConfig.searchFilterTreeExpanded)
    ? pageConfig.searchFilterTreeExpanded
    : defaultSearchFilterTreeExpanded)
    .filter(node => typeof node === 'string')
);
const navigationGroupTreeCollapsed = new Set(
  (Array.isArray(pageConfig.navigationGroupTreeCollapsed)
    ? pageConfig.navigationGroupTreeCollapsed
    : [])
    .filter(node => typeof node === 'string')
);

function filterPreferenceEnabled(key) {
  return filterPreferences[key] === true;
}

function setDocumentTitle(itemTitle = '') {
  const normalizedTitle = String(itemTitle || '').trim();
  document.title = normalizedTitle
    ? `${defaultDocumentTitle} - ${normalizedTitle}`
    : defaultDocumentTitle;
}

let data = null;
let playlistMemberships = new Map();
let playlistChildren = new Map();
let channelMemberships = new Map();
let channelChildren = new Map();
let navigationGroupTreeDomId = 0;
const browserPlugins = new Map();
const loadedBrowserPluginAssets = new Set();
const pluginSearchVisibility = new Map();
const pluginVideoFacetVisibility = new Map();

function browserSearchFieldDefinition(plugin) {
  const definition = plugin?.search?.searchField;
  return definition && typeof definition === 'object' ? definition : null;
}

function browserSearchFieldKey(plugin) {
  const definition = browserSearchFieldDefinition(plugin);
  return String(definition?.key || plugin?.id || '');
}

function browserPluginSearchFieldEnabled(plugin) {
  const key = browserSearchFieldKey(plugin);
  return Boolean(key && activeSearchFields().has(key));
}

function registerBrowserPluginSearchField(plugin) {
  const definition = browserSearchFieldDefinition(plugin);
  if (!definition) return;
  const key = browserSearchFieldKey(plugin);
  const labelText = String(definition.label || '').trim();
  if (!/^[a-z][a-z0-9_-]*$/.test(key)) {
    throw new TypeError(`Plugin search field key is invalid: ${key}`);
  }
  if (!labelText) throw new TypeError(`Plugin search field label is required: ${plugin.id}`);
  if (searchFields.some(input => input.dataset.searchField === key)) {
    throw new Error(`Search field is already registered: ${key}`);
  }
  const label = document.createElement('label');
  label.className = 'filter';
  label.dataset.browserPluginSearchField = plugin.id;
  const input = document.createElement('input');
  input.className = 'search-field';
  input.type = 'checkbox';
  input.dataset.searchField = key;
  input.checked = definition.defaultEnabled !== false;
  label.append(input, document.createTextNode(` ${labelText}`));
  searchInFields.append(label);
  searchFields.push(input);
  bindSearchField(input);
}

function browserVideoFacetDefinition(plugin) {
  const definition = plugin?.search?.videoFacet;
  return definition && typeof definition === 'object' ? definition : null;
}

function defaultBrowserVideoFacetVisibility(plugin) {
  const definition = browserVideoFacetDefinition(plugin);
  return {
    present: !filterPreferenceEnabled(String(definition?.presentDisabledPreferenceKey || '')),
    absent: !filterPreferenceEnabled(String(definition?.absentDisabledPreferenceKey || '')),
  };
}

function registerBrowserPlugin(plugin) {
  if (!plugin || typeof plugin !== 'object') throw new TypeError('Plugin registration is required');
  const pluginId = String(plugin.id || '');
  if (!/^[a-z][a-z0-9_-]*$/.test(pluginId)) throw new TypeError('Plugin id is invalid');
  if (browserPlugins.has(pluginId)) throw new Error(`Plugin is already registered: ${pluginId}`);
  browserPlugins.set(pluginId, plugin);
  if (plugin.search) {
    registerBrowserPluginSearchField(plugin);
    if (browserVideoFacetDefinition(plugin)) {
      pluginVideoFacetVisibility.set(
        pluginId,
        defaultBrowserVideoFacetVisibility(plugin),
      );
    } else {
      pluginSearchVisibility.set(
        pluginId,
        filterPreferenceEnabled(String(plugin.search.preferenceKey || '')),
      );
    }
  }
}

window.YTLibraryBrowserPlugins = Object.freeze({
  apiVersion: 1,
  register: registerBrowserPlugin,
});

function browserPluginStatus(pluginId) {
  return (data?.plugins || []).find(plugin => plugin.id === pluginId) || null;
}

function browserPluginSupports(pluginId, capability) {
  const status = browserPluginStatus(pluginId);
  return Boolean(
    status
    && status.enabled
    && status.state === 'ready'
    && (!capability || (status.capabilities || []).includes(capability))
  );
}

function browserSearchPlugins() {
  return [...browserPlugins.values()].filter(plugin => (
    plugin.search
    && browserPluginSupports(plugin.id, plugin.search.capability)
  ));
}

function browserSearchPlugin(pluginId) {
  return browserSearchPlugins().find(plugin => plugin.id === pluginId) || null;
}

function browserVideoFilterPlugins() {
  return browserSearchPlugins().filter(plugin => Boolean(browserVideoFacetDefinition(plugin)));
}

function browserVideoSearchFieldPlugins() {
  return browserVideoFilterPlugins().filter(plugin => (
    browserPluginSearchFieldEnabled(plugin) && searchKindEnabled(plugin.id)
  ));
}

function browserVideoFacetState(plugin) {
  return pluginVideoFacetVisibility.get(plugin.id) || { present: false, absent: false };
}

function browserVideoFacetSearchActive(plugin) {
  const state = browserVideoFacetState(plugin);
  return state.present && !state.absent;
}

function browserPluginStateKey(plugin) {
  if (browserVideoFacetDefinition(plugin)) {
    const state = browserVideoFacetState(plugin);
    return `${plugin.id}:${state.present ? '1' : '0'}${state.absent ? '1' : '0'}`;
  }
  return searchKindEnabled(plugin.id) ? plugin.id : '';
}

function saveBrowserVideoFacetPreferences(plugin) {
  const definition = browserVideoFacetDefinition(plugin);
  const state = browserVideoFacetState(plugin);
  if (definition.presentDisabledPreferenceKey) {
    saveFilterPreference(definition.presentDisabledPreferenceKey, !state.present);
  }
  if (definition.absentDisabledPreferenceKey) {
    saveFilterPreference(definition.absentDisabledPreferenceKey, !state.absent);
  }
}

function browserResultSearchPlugins() {
  return browserSearchPlugins().filter(plugin => (
    !browserVideoFacetDefinition(plugin)
    && typeof plugin.search.fetch === 'function'
  ));
}

function browserPluginAssetUrl(pluginId, path, version = '') {
  const encodedPath = String(path || '').split('/').map(encodeURIComponent).join('/');
  const baseUrl = `/plugins/${encodeURIComponent(pluginId)}/assets/${encodedPath}`;
  return version ? `${baseUrl}?v=${encodeURIComponent(version)}` : baseUrl;
}

async function loadBrowserPluginAsset(pluginId, asset, version = '') {
  const path = String(asset?.path || '');
  const type = String(asset?.type || '');
  if (!path || !['script', 'style'].includes(type)) return;
  const url = browserPluginAssetUrl(pluginId, path, version);
  if (loadedBrowserPluginAssets.has(url)) return;
  const element = type === 'style'
    ? document.createElement('link')
    : document.createElement('script');
  if (type === 'style') {
    element.rel = 'stylesheet';
    element.href = url;
  } else {
    element.src = url;
  }
  await new Promise((resolve, reject) => {
    element.addEventListener('load', resolve, { once: true });
    element.addEventListener('error', () => reject(new Error(`Plugin asset failed: ${url}`)), { once: true });
    document.head.append(element);
  });
  loadedBrowserPluginAssets.add(url);
}

async function loadBrowserPlugins(statuses) {
  for (const status of statuses || []) {
    if (!status.enabled || status.state !== 'ready') continue;
    for (const asset of status.browserAssets || []) {
      await loadBrowserPluginAsset(status.id, asset, status.version);
    }
  }
}

let selected = '';
function defaultPlaylistVideoVisibility() {
  return {
    public: true,
    unlisted: true,
    private: true,
    members_only: true,
    unavailable: filterPreferenceEnabled(filterPreferenceKeys.unavailablePlaylistVideos),
    unknown: true,
    removed: filterPreferenceEnabled(filterPreferenceKeys.removedPlaylistVideos),
  };
}
let playlistVisibility = defaultPlaylistVideoVisibility();
let partialCompletionMinimumPercent = boundedPartialMinimumPercent(
  pageConfig.partialCompletionMinPercent
);
function defaultPartialBelowMinimumEnabled() {
  return partialCompletionMinimumPercent <= 1
    || filterPreferenceEnabled(filterPreferenceKeys.lowPartialCompletion);
}
let playlistCompletionVisibility = {
  complete: true,
  partial: true,
  partial_below_minimum: defaultPartialBelowMinimumEnabled(),
  unknown: true,
  never_watched: true,
};
let playlistDuplicatesOnly = false;
let completionMinimumInputTimer = null;
const noUploaderCategoryFilter = '__no_category__';
const defaultSearchMetaVisibility = {
  videos: {
    public: true,
    unlisted: true,
    private: true,
    members_only: true,
    unavailable: filterPreferenceEnabled(filterPreferenceKeys.unavailableVideos),
    unknown: true,
  },
  reactions: { none: true, liked: true, disliked: true },
  completion: {
    complete: true,
    partial: true,
    partial_below_minimum: defaultPartialBelowMinimumEnabled(),
    unknown: true,
    never_watched: true,
  },
  membership: { member: true, non_member: true },
  uploaderCategory: { [noUploaderCategoryFilter]: true },
  channelSubscription: { subscribed: true, non_subscribed: true },
  channelStatus: {
    active: true,
    terminated: filterPreferenceEnabled(filterPreferenceKeys.terminatedChannels),
  },
  playlistVisibility: { private: true, public: true, unlisted: true, unknown: true },
  playlistOwnership: { mine: true, others: true, ownership_unknown: true },
  playlistStatus: {
    active: true,
    removed: filterPreferenceEnabled(filterPreferenceKeys.removedPlaylists),
  },
};
let searchMetaVisibility = Object.fromEntries(
  Object.entries(defaultSearchMetaVisibility).map(([groupName, visibility]) => [
    groupName,
    { ...visibility },
  ])
);
let uploaderCategorySelectionExplicit = false;
const searchMetaParamNames = {
  videos: 'vm',
  reactions: 'vr',
  completion: 'vc',
  membership: 'vpm',
  uploaderCategory: 'vuc',
  channelSubscription: 'csub',
  channelStatus: 'cstatus',
  playlistVisibility: 'pm',
  playlistOwnership: 'po',
  playlistStatus: 'ps',
};
const searchOptInMetaFilters = [
  {
    groupName: 'videos', key: 'unavailable', paramName: 'unavailable',
    preferenceKey: filterPreferenceKeys.unavailableVideos,
  },
  {
    groupName: 'completion', key: 'partial_below_minimum', paramName: 'partial_below',
    preferenceKey: filterPreferenceKeys.lowPartialCompletion,
  },
  {
    groupName: 'playlistStatus', key: 'removed', paramName: 'removed',
    preferenceKey: filterPreferenceKeys.removedPlaylists,
  },
  {
    groupName: 'channelStatus', key: 'terminated', paramName: 'terminated',
    preferenceKey: filterPreferenceKeys.terminatedChannels,
  },
];
const playlistVideoOptInFilters = [
  {
    key: 'unavailable',
    preferenceKey: filterPreferenceKeys.unavailablePlaylistVideos,
  },
  {
    key: 'removed',
    preferenceKey: filterPreferenceKeys.removedPlaylistVideos,
  },
];
const searchSortOptions = new Set([
  'relevance', 'title', 'newest', 'oldest', 'most_watched', 'type',
]);
const playlistVideoSortOptions = new Set([
  'newest_added', 'title', 'oldest_added', 'most_watched', 'playlist_order',
]);
const sortPreferences = { ...(pageConfig.sortPreferences || {}) };
let searchResultsSort = 'newest';
let searchSortExplicit = false;
let activeSearchPreset = '';
let searchPlaylistGroupKey = '';
let searchChannelGroupKey = '';
const cardLayouts = new Set(['grid', 'detailed', 'compact']);
let searchCardLayout = cardLayouts.has(pageConfig.searchCardLayout)
  ? pageConfig.searchCardLayout
  : 'grid';
let playlistCardLayout = cardLayouts.has(pageConfig.playlistCardLayout)
  ? pageConfig.playlistCardLayout
  : 'grid';
let historyCardLayout = cardLayouts.has(pageConfig.historyCardLayout)
  ? pageConfig.historyCardLayout
  : 'compact';
const cardLayoutSaveChains = {
  search: Promise.resolve(),
  playlist: Promise.resolve(),
  history: Promise.resolve(),
};
const cardLayoutSaveVersions = { search: 0, playlist: 0, history: 0 };
let sortPreferenceSaveChain = Promise.resolve();
const sortPreferenceSaveVersions = new Map();
let pageSizeSaveChain = Promise.resolve();
let pageSizeSaveVersion = 0;
let partialCompletionMinimumSaveChain = Promise.resolve();
let partialCompletionMinimumSaveVersion = 0;
let filterPreferenceSaveChain = Promise.resolve();
const filterPreferenceSaveVersions = new Map();
let searchFilterTreeSaveChain = Promise.resolve();
let searchFilterTreeSaveVersion = 0;
let navigationGroupTreeSaveChain = Promise.resolve();
let navigationGroupTreeSaveVersion = 0;
const searchPresetDefinitions = {
  videos: { kind: 'videos', sort: 'newest' },
  'playlist-videos': { kind: 'videos', sort: 'newest' },
  'liked-videos': { kind: 'videos', sort: 'newest' },
  'all-playlists': { kind: 'playlists', sort: 'title' },
  channels: { kind: 'channels', sort: 'title' },
  subscribed: { kind: 'channels', sort: 'title' },
  terminated: { kind: 'channels', sort: 'title' },
  'playlist-group': { kind: 'playlists', sort: 'title' },
  'channel-group': { kind: 'channels', sort: 'title' },
};

function browserSearchPresets(section = '') {
  return browserSearchPlugins().flatMap(plugin => {
    const preset = plugin.search.preset;
    const presetId = String(preset?.id || '');
    if (!preset || !/^[a-z][a-z0-9_-]*$/.test(presetId)) return [];
    if (section && String(preset.section || '') !== section) return [];
    return [{ plugin, preset, presetId }];
  });
}

function searchPresetDefinition(presetId) {
  if (searchPresetDefinitions[presetId]) return searchPresetDefinitions[presetId];
  const entry = browserSearchPresets().find(item => item.presetId === presetId);
  if (!entry) return null;
  const videoFacet = Boolean(browserVideoFacetDefinition(entry.plugin));
  return {
    emptyMessage: entry.preset.emptyMessage,
    kind: videoFacet ? 'videos' : entry.plugin.id,
    pluginFilter: videoFacet ? entry.plugin.id : '',
    preserveQuery: entry.preset.preserveQuery === true,
    sort: entry.preset.sort || 'relevance',
  };
}

function searchPresetEmptyMessage(query) {
  const configured = searchPresetDefinition(activeSearchPreset)?.emptyMessage;
  if (typeof configured === 'function') return String(configured({ query }) || '');
  return String(configured || '');
}
let playlistVisibilityPlaylistId = '';
let playlistViewSort = playlistVideoSortOptions.has(sortPreferences.playlist)
  ? sortPreferences.playlist
  : 'playlist_order';
let playlistPageSearch = '';
let currentPage = 1;
let renderedPageInfo = { page: 1, pageCount: 1, total: 0 };
let pageBoundaryNavigationPending = false;
let pendingPageBoundaryLanding = '';
let pageBoundaryInputArmed = true;
let pageBoundaryInputTimer = null;
let pageBoundaryFallbackTimer = null;
let pageBoundaryTouchY = null;
let pageBoundaryTouchDistance = 0;
let pageBoundaryTouchDirection = 0;
let pageBoundaryTouchTargetAllowed = false;
let pageSize = String(pageConfig.pageSize || 100);
const adjacentPageCacheLimit = 6;
const historyActivityCacheLimit = 4;
const viewDataCacheLimit = 24;
let historyPageCache = new Map();
let historyActivityCache = new Map();
let historyActivityYearOffset = 0;
let historyActivitySyncEnabled = true;
let omniSearchCache = new Map();
let viewDataCache = new Map();
let adjacentPagePrefetchCancel = null;
let adjacentPagePrefetchGeneration = 0;
let videoMetaCountsCache = new Map();
let videoCompletionCountsCache = new Map();
let omniMetaCountsCache = new Map();
let omniVideoPluginFacetCountsCache = new Map();
let omniReactionCountsCache = new Map();
let omniCompletionCountsCache = new Map();
let omniPlaylistMembershipCountsCache = new Map();
let omniUploaderCategoryCountsCache = new Map();
let pendingHistoryDate = '';
let historyNavigationDate = '';
let channelHistoryCounts = new Map();
let channelDetailTab = 'playlists';
let renderGeneration = 0;
let renderedOmniSearchQuery = '';
let searchResultsRendered = false;
let retainedSearchHash = '#search';
let searchMetaProgressTimer = null;
let searchHeaderProgressTimer = null;
let searchHeaderProgressToken = 0;
let pendingSidebarProgressToken = null;
let pendingSearchMetaGroups = new Set();
let searchMetaProgressDots = '';
let searchInputTimer = null;
let playlistSearchTimer = null;
const search = document.getElementById('search');
const searchNav = document.getElementById('search-nav');
const historyNav = document.getElementById('history-nav');
const searchFilters = document.getElementById('search-filters');
const searchInFields = document.getElementById('search-in-fields');
const searchForFilters = document.getElementById('search-for-filters');
const refresh = document.getElementById('refresh');
const groupsEl = document.getElementById('groups');
const searchFields = [...document.querySelectorAll('.search-field')];
const grid = document.getElementById('grid');
const empty = document.getElementById('empty');
const title = document.getElementById('view-title');
const meta = document.getElementById('view-meta');
const bottomPager = document.getElementById('bottom-pager');
const resultsScroll = document.querySelector('.results-scroll');
const viewTop = document.getElementById('view-top');
const viewContext = document.getElementById('view-context');
const searchProgressStatus = document.getElementById('search-progress-status');

async function loadData({ preserveSearchContent = false } = {}) {
  cancelAdjacentPagePrefetch();
  refresh.disabled = true;
  refresh.textContent = 'Refreshing';
  const response = await fetch('/api/bootstrap', { cache: 'no-store' });
  if (!response.ok) throw new Error(`Data refresh failed: ${response.status}`);
  data = await response.json();
  await loadBrowserPlugins(data.plugins || []);
  historyPageCache = new Map();
  historyActivityCache = new Map();
  omniSearchCache = new Map();
  if (!preserveSearchContent) {
    renderedOmniSearchQuery = '';
    searchResultsRendered = false;
  }
  viewDataCache = new Map();
  videoMetaCountsCache = new Map();
  videoCompletionCountsCache = new Map();
  omniMetaCountsCache = new Map();
  omniVideoPluginFacetCountsCache = new Map();
  omniReactionCountsCache = new Map();
  omniCompletionCountsCache = new Map();
  omniPlaylistMembershipCountsCache = new Map();
  omniUploaderCategoryCountsCache = new Map();
  channelHistoryCounts = new Map();
  playlistMemberships = new Map();
  for (const item of data.memberships || []) {
    if (!playlistMemberships.has(item.group_key)) playlistMemberships.set(item.group_key, []);
    playlistMemberships.get(item.group_key).push(item.playlist_id);
  }
  playlistChildren = new Map();
  for (const group of data.groups || []) {
    const parent = group.parent_key || '';
    if (!playlistChildren.has(parent)) playlistChildren.set(parent, []);
    playlistChildren.get(parent).push(group);
  }
  channelMemberships = new Map();
  for (const item of data.channelMemberships || []) {
    if (!channelMemberships.has(item.group_key)) channelMemberships.set(item.group_key, []);
    channelMemberships.get(item.group_key).push(item.channel_id);
  }
  channelChildren = new Map();
  for (const group of data.channelGroups || []) {
    const parent = group.parent_key || '';
    if (!channelChildren.has(parent)) channelChildren.set(parent, []);
    channelChildren.get(parent).push(group);
  }
  selected = selectionFromHash();
  renderGroups();
  await render();
  refresh.disabled = false;
  refresh.textContent = 'Refresh';
}

function trimRequestCache(cache, maxEntries) {
  if (cache.size <= maxEntries) return;
  for (const [key, entry] of cache) {
    if (cache.size <= maxEntries) break;
    if (!entry?.promise) cache.delete(key);
  }
}

function cachedRequest(cache, key, load, maxEntries) {
  const cached = cache.get(key);
  if (cached?.data) {
    cache.delete(key);
    cache.set(key, cached);
    return Promise.resolve(cached.data);
  }
  if (cached?.promise) return cached.promise;
  const promise = Promise.resolve().then(load).then(payload => {
    const current = cache.get(key);
    if (!current || current.promise === promise) {
      cache.delete(key);
      cache.set(key, { data: payload, promise: null });
      trimRequestCache(cache, maxEntries);
    }
    return payload;
  }).catch(error => {
    if (cache.get(key)?.promise === promise) cache.delete(key);
    throw error;
  });
  cache.set(key, { data: null, promise });
  trimRequestCache(cache, maxEntries);
  return promise;
}

async function fetchViewData(path) {
  return cachedRequest(viewDataCache, path, async () => {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `View fetch failed: ${response.status}`);
    }
    return response.json();
  }, viewDataCacheLimit);
}

function remoteListPath(path, params = {}, page = currentPage) {
  const size = pageSizeNumber();
  const limit = Number.isFinite(size) ? size : 500;
  const requestedPage = Math.max(1, Number(page) || 1);
  const queryParams = new URLSearchParams({
    ...params,
    limit: String(limit),
    offset: String((requestedPage - 1) * limit),
  });
  return `${path}?${queryParams}`;
}

function playlistSelection(playlistId) {
  return `__playlist__:${playlistId}`;
}

function videoSelection(videoId) {
  return `__video__:${videoId}`;
}

function channelSelection(channelId) {
  return `__channel__:${channelId}`;
}

function paginationParams() {
  const params = new URLSearchParams();
  if (historyNavigationDate && historyDateNavigationIsActive()) {
    params.set('date', historyNavigationDate);
  } else if (currentPage > 1) {
    params.set('page', String(currentPage));
  }
  return params;
}

function appendHashParams(base, params) {
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

function hashParts(hash) {
  const queryIndex = hash.indexOf('?');
  return {
    base: queryIndex >= 0 ? hash.slice(0, queryIndex) : hash,
    params: new URLSearchParams(queryIndex >= 0 ? hash.slice(queryIndex + 1) : ''),
  };
}

function historyDateParam(value) {
  const date = String(value || '').trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : '';
}

function historyDateNavigationIsActive() {
  return selected === '__history__'
    || (selected.startsWith('__channel__:') && channelDetailTab === 'history');
}

function applyPaginationParams(params, allowHistoryDate = false) {
  historyNavigationDate = allowHistoryDate ? historyDateParam(params.get('date')) : '';
  if (historyNavigationDate) {
    currentPage = 1;
    pendingHistoryDate = historyNavigationDate;
    historyActivityYearOffset = historyActivityYearOffsetForDate(historyNavigationDate);
    return;
  }
  const page = Number(params.get('page') || 1);
  currentPage = Number.isFinite(page) && page > 0 ? page : 1;
}

function currentHashHasPaginationParams() {
  const { params } = hashParts(window.location.hash || '');
  return params.has('page') || params.has('size');
}

function localPlaylistHref(playlistId, includePagination = false) {
  const base = `#playlist=${encodeURIComponent(playlistId)}`;
  return includePagination ? appendHashParams(base, paginationParams()) : base;
}

function localVideoHref(videoId, includePagination = false) {
  const base = `#video=${encodeURIComponent(videoId)}`;
  return includePagination ? appendHashParams(base, paginationParams()) : base;
}

function encodeChannelReference(channelReference) {
  return encodeURIComponent(channelReference).replace(/%40/gi, '@');
}

function localChannelHref(channelId, includePagination = false) {
  const base = `#channel=${encodeChannelReference(channelId)}`;
  return includePagination ? appendHashParams(base, paginationParams()) : base;
}

const viewHashBySelection = new Map([
  ['__search__', 'search'],
  ['__history__', 'history'],
]);
const selectionByViewHash = new Map([...viewHashBySelection].map(([selection, hash]) => [hash, selection]));

function localViewHref(value, includePagination = false) {
  if (value === '__search__') return searchHash();
  const viewHash = viewHashBySelection.get(value);
  const base = viewHash ? `#view=${viewHash}` : '#search';
  return includePagination ? appendHashParams(base, paginationParams()) : base;
}

function searchFieldParamValue() {
  return [...activeSearchFields()].sort().join(',');
}

function metaFilterParamValue(visibility, excludedKeys = []) {
  const excluded = new Set(excludedKeys);
  const selected = Object.entries(visibility)
    .filter(([key, enabled]) => enabled && !excluded.has(key))
    .map(([key]) => key);
  return selected.length ? selected.join(',') : '__none__';
}

function boundedPartialMinimumPercent(value) {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) ? Math.max(1, Math.min(99, parsed)) : 1;
}

function setPartialCompletionMinimum(value) {
  const previousMinimum = partialCompletionMinimumPercent;
  const nextMinimum = boundedPartialMinimumPercent(value);
  partialCompletionMinimumPercent = nextMinimum;
  if (previousMinimum <= 1 || nextMinimum <= 1) {
    const belowMinimumEnabled = defaultPartialBelowMinimumEnabled();
    defaultSearchMetaVisibility.completion.partial_below_minimum = belowMinimumEnabled;
    searchMetaVisibility.completion.partial_below_minimum = belowMinimumEnabled;
    playlistCompletionVisibility.partial_below_minimum = belowMinimumEnabled;
  }
  return previousMinimum;
}

function allMetaFiltersEnabled(visibility, excludedKeys = []) {
  const excluded = new Set(excludedKeys);
  return Object.entries(visibility)
    .filter(([key]) => !excluded.has(key))
    .every(([, enabled]) => enabled);
}

function searchMetaPresetBaseline(groupName, preset = activeSearchPreset) {
  const visibility = searchMetaVisibility[groupName] || {};
  const baseline = Object.fromEntries(
    Object.keys(visibility).map(key => [key, false])
  );
  const definition = searchPresetDefinition(preset);
  if (!definition || searchKindFacetKeys(definition.kind).includes(groupName)) {
    for (const key of Object.keys(baseline)) {
      baseline[key] = defaultSearchMetaVisibility[groupName]?.[key] === true;
    }
  }
  const presetSelections = {
    'playlist-videos': { membership: ['member'] },
    'liked-videos': { reactions: ['liked'] },
    subscribed: { channelSubscription: ['subscribed'] },
    terminated: { channelStatus: ['terminated'] },
  };
  const selected = presetSelections[preset]?.[groupName];
  if (selected) {
    for (const key of Object.keys(baseline)) baseline[key] = selected.includes(key);
  }
  return baseline;
}

function metaFilterSelectionMatches(visibility, baseline, excludedKeys = []) {
  const excluded = new Set(excludedKeys);
  return Object.keys(visibility)
    .filter(key => !excluded.has(key))
    .every(key => visibility[key] === baseline[key]);
}

function browserVideoFacetPresetBaseline(plugin, preset = activeSearchPreset) {
  const definition = searchPresetDefinition(preset);
  const baseline = definition?.kind === 'videos' || !definition
    ? defaultBrowserVideoFacetVisibility(plugin)
    : { present: false, absent: false };
  return definition?.pluginFilter === plugin.id
    ? { present: true, absent: false }
    : baseline;
}

function searchOptInKeys(groupName) {
  return searchOptInMetaFilters
    .filter(filter => filter.groupName === groupName)
    .map(filter => filter.key);
}

function searchOptInFilter(groupName, key) {
  return searchOptInMetaFilters.find(
    filter => filter.groupName === groupName && filter.key === key
  ) || null;
}

function setLocalFilterPreference(preferenceKey, enabled) {
  if (enabled) {
    filterPreferences[preferenceKey] = true;
  } else {
    delete filterPreferences[preferenceKey];
  }
  const plugin = browserSearchPlugins().find(
    item => item.search.preferenceKey === preferenceKey
  );
  if (plugin) pluginSearchVisibility.set(plugin.id, enabled);
  for (const videoFacetPlugin of browserVideoFilterPlugins()) {
    const definition = browserVideoFacetDefinition(videoFacetPlugin);
    const state = browserVideoFacetState(videoFacetPlugin);
    if (definition.presentDisabledPreferenceKey === preferenceKey) {
      state.present = !enabled;
    }
    if (definition.absentDisabledPreferenceKey === preferenceKey) {
      state.absent = !enabled;
    }
  }
  const playlistFilter = playlistVideoOptInFilters.find(
    item => item.preferenceKey === preferenceKey
  );
  if (playlistFilter) playlistVisibility[playlistFilter.key] = enabled;
  const filter = searchOptInMetaFilters.find(
    item => item.preferenceKey === preferenceKey
  );
  if (!filter) return;
  const effectiveEnabled = (
    preferenceKey === filterPreferenceKeys.lowPartialCompletion
    && partialCompletionMinimumPercent <= 1
  ) || enabled;
  defaultSearchMetaVisibility[filter.groupName][filter.key] = effectiveEnabled;
  searchMetaVisibility[filter.groupName][filter.key] = effectiveEnabled;
  if (preferenceKey === filterPreferenceKeys.lowPartialCompletion) {
    playlistCompletionVisibility.partial_below_minimum = effectiveEnabled;
  }
}

function saveFilterPreference(preferenceKey, enabled) {
  const previousEnabled = filterPreferenceEnabled(preferenceKey);
  if (previousEnabled === enabled) return;
  setLocalFilterPreference(preferenceKey, enabled);
  const version = (filterPreferenceSaveVersions.get(preferenceKey) || 0) + 1;
  filterPreferenceSaveVersions.set(preferenceKey, version);
  void persistFilterPreference(preferenceKey, enabled).catch(error => {
    if (filterPreferenceSaveVersions.get(preferenceKey) !== version) return;
    setLocalFilterPreference(preferenceKey, previousEnabled);
    if (selected === '__search__') {
      syncSearchHashAndRender(true);
    } else {
      void render();
    }
    window.alert(error instanceof Error ? error.message : String(error));
  });
}

function saveSearchOptInPreferences(groupNames) {
  const groups = new Set(groupNames);
  for (const filter of searchOptInMetaFilters) {
    if (!groups.has(filter.groupName)) continue;
    saveFilterPreference(
      filter.preferenceKey,
      Boolean(searchMetaVisibility[filter.groupName][filter.key]),
    );
  }
}

function savePlaylistVideoOptInPreferences() {
  for (const filter of playlistVideoOptInFilters) {
    saveFilterPreference(filter.preferenceKey, Boolean(playlistVisibility[filter.key]));
  }
}

function resetSearchMetaVisibility() {
  for (const [groupName, defaults] of Object.entries(defaultSearchMetaVisibility)) {
    Object.assign(searchMetaVisibility[groupName], defaults);
  }
  for (const plugin of browserSearchPlugins()) {
    if (browserVideoFacetDefinition(plugin)) {
      Object.assign(
        browserVideoFacetState(plugin),
        defaultBrowserVideoFacetVisibility(plugin),
      );
    } else {
      pluginSearchVisibility.set(
        plugin.id,
        filterPreferenceEnabled(String(plugin.search.preferenceKey || '')),
      );
    }
  }
}

function clearSearchMetaVisibility() {
  for (const visibility of Object.values(searchMetaVisibility)) {
    for (const key of Object.keys(visibility)) visibility[key] = false;
  }
  for (const plugin of browserSearchPlugins()) {
    if (browserVideoFacetDefinition(plugin)) {
      Object.assign(browserVideoFacetState(plugin), { present: false, absent: false });
    } else {
      pluginSearchVisibility.set(plugin.id, false);
    }
  }
}

function enableDefaultSearchKind(kind) {
  const plugin = browserSearchPlugin(kind);
  if (plugin) {
    pluginSearchVisibility.set(plugin.id, true);
    return;
  }
  for (const facetKey of searchKindFacetKeys(kind)) {
    Object.assign(searchMetaVisibility[facetKey], defaultSearchMetaVisibility[facetKey]);
  }
  if (kind !== 'videos') return;
  for (const videoFilter of browserVideoFilterPlugins()) {
    const state = browserVideoFacetState(videoFilter);
    if (state.present || state.absent) continue;
    const defaults = defaultBrowserVideoFacetVisibility(videoFilter);
    Object.assign(
      state,
      defaults.present || defaults.absent
        ? defaults
        : { present: true, absent: true },
    );
  }
}

function setSearchFacetSelection(groupName, selectedKeys) {
  const selected = new Set(selectedKeys);
  for (const key of Object.keys(searchMetaVisibility[groupName])) {
    searchMetaVisibility[groupName][key] = selected.has(key);
  }
}

function applySearchPresetState(preset, groupKey = '') {
  uploaderCategorySelectionExplicit = false;
  const definition = searchPresetDefinition(preset);
  const groupPreset = preset === 'playlist-group' || preset === 'channel-group';
  if (!definition || (groupPreset && !groupKey)) {
    activeSearchPreset = '';
    searchPlaylistGroupKey = '';
    searchChannelGroupKey = '';
    resetSearchMetaVisibility();
    return;
  }
  activeSearchPreset = preset;
  searchPlaylistGroupKey = preset === 'playlist-group' ? groupKey : '';
  searchChannelGroupKey = preset === 'channel-group' ? groupKey : '';
  clearSearchMetaVisibility();
  enableDefaultSearchKind(definition.kind);
  if (definition.pluginFilter) {
    const plugin = browserSearchPlugin(definition.pluginFilter);
    if (browserVideoFacetDefinition(plugin)) {
      Object.assign(browserVideoFacetState(plugin), { present: true, absent: false });
    } else {
      pluginSearchVisibility.set(definition.pluginFilter, true);
    }
  }
  if (preset === 'playlist-videos') {
    setSearchFacetSelection('membership', ['member']);
  } else if (preset === 'liked-videos') {
    setSearchFacetSelection('reactions', ['liked']);
  } else if (preset === 'subscribed') {
    setSearchFacetSelection('channelSubscription', ['subscribed']);
  } else if (preset === 'terminated') {
    setSearchFacetSelection('channelStatus', ['terminated']);
  }
}

function defaultSearchResultsSort(
  query = search.value.trim(),
  preset = activeSearchPreset,
) {
  if (query) return 'relevance';
  return searchPresetDefinition(preset)?.sort || 'newest';
}

function browserPluginForcesRelevance(plugin, query = search.value.trim()) {
  const mode = plugin?.search?.forceRelevance;
  return mode === true || (mode === 'query' && Boolean(query));
}

function browserPluginSearchActive(plugin) {
  return browserVideoFacetDefinition(plugin)
    ? browserVideoFacetSearchActive(plugin) || browserPluginSearchFieldEnabled(plugin)
    : searchKindEnabled(plugin.id);
}

function searchSortPreferenceContext(preset = activeSearchPreset) {
  return preset || 'search';
}

function preferredSearchResultsSort(
  query = search.value.trim(),
  preset = activeSearchPreset,
) {
  const preference = sortPreferences[searchSortPreferenceContext(preset)];
  return searchSortOptions.has(preference)
    ? preference
    : defaultSearchResultsSort(query, preset);
}

function searchKindEnabled(kind) {
  const plugin = browserSearchPlugin(kind);
  if (plugin) {
    if (browserVideoFacetDefinition(plugin)) {
      const state = browserVideoFacetState(plugin);
      return state.present || state.absent;
    }
    return pluginSearchVisibility.get(plugin.id) === true;
  }
  const nativeFacetsEnabled = searchKindFacetKeys(kind).every(
    key => Object.values(searchMetaVisibility[key]).some(Boolean)
  );
  if (kind !== 'videos' || !nativeFacetsEnabled) return nativeFacetsEnabled;
  return browserVideoFilterPlugins().every(pluginItem => {
    const state = browserVideoFacetState(pluginItem);
    return state.present || state.absent;
  });
}

function selectedSearchKinds() {
  return [
    'videos',
    'playlists',
    'channels',
    ...browserResultSearchPlugins().map(plugin => plugin.id),
  ].filter(searchKindEnabled);
}

function selectedSearchResultKinds() {
  const resultKindByFilterKind = {
    videos: 'video',
    playlists: 'playlist',
    channels: 'channel',
  };
  return selectedSearchKinds().map(kind => resultKindByFilterKind[kind]).filter(Boolean);
}

function activeSearchSourceScopes() {
  return {
    video: activeSearchPreset === 'playlist-videos'
      ? 'playlist_member'
      : (activeSearchPreset === 'liked-videos' ? 'liked' : ''),
    channel: activeSearchPreset === 'subscribed'
      ? 'subscribed'
      : (activeSearchPreset === 'terminated' ? 'terminated' : ''),
  };
}

function presetDefiningFiltersMatch() {
  const pluginFilter = searchPresetDefinition(activeSearchPreset)?.pluginFilter;
  if (pluginFilter) {
    const plugin = browserSearchPlugin(pluginFilter);
    if (
      !plugin
      || (
        browserVideoFacetDefinition(plugin)
          ? !browserVideoFacetSearchActive(plugin)
          : !searchKindEnabled(pluginFilter)
      )
    ) return false;
  }
  if (activeSearchPreset === 'playlist-videos') {
    return searchMetaVisibility.membership.member && !searchMetaVisibility.membership.non_member;
  }
  if (activeSearchPreset === 'liked-videos') {
    return searchMetaVisibility.reactions.liked
      && !searchMetaVisibility.reactions.none
      && !searchMetaVisibility.reactions.disliked;
  }
  if (activeSearchPreset === 'subscribed') {
    return searchMetaVisibility.channelSubscription.subscribed
      && !searchMetaVisibility.channelSubscription.non_subscribed;
  }
  if (activeSearchPreset === 'terminated') {
    return searchMetaVisibility.channelStatus.terminated
      && !searchMetaVisibility.channelStatus.active;
  }
  if (activeSearchPreset === 'playlist-group') return Boolean(searchPlaylistGroupKey);
  if (activeSearchPreset === 'channel-group') return Boolean(searchChannelGroupKey);
  return true;
}

function reconcileSearchPreset() {
  const definition = searchPresetDefinition(activeSearchPreset);
  if (!definition) return;
  const kinds = selectedSearchKinds();
  if (kinds.length === 1 && kinds[0] === definition.kind && presetDefiningFiltersMatch()) return;
  activeSearchPreset = '';
  searchPlaylistGroupKey = '';
  searchChannelGroupKey = '';
}

function applyMetaFilterParam(visibility, value, excludedKeys = []) {
  if (value === null) return;
  const excluded = new Set(excludedKeys);
  const selectedValues = new Set(value === '__none__' ? [] : value.split(',').filter(Boolean));
  for (const key of Object.keys(visibility)) {
    if (excluded.has(key)) continue;
    visibility[key] = selectedValues.has(key);
  }
}

function searchHash() {
  const params = new URLSearchParams();
  const query = search.value.trim();
  if (query) params.set('q', query);
  if (activeSearchPreset) params.set('preset', activeSearchPreset);
  const groupKey = searchPlaylistGroupKey || searchChannelGroupKey;
  if (groupKey) params.set('group', groupKey);
  if (activeSearchFields().size !== searchFields.length) {
    params.set('in', searchFieldParamValue() || '__none__');
  }
  for (const [groupName, paramName] of Object.entries(searchMetaParamNames)) {
    const visibility = searchMetaVisibility[groupName];
    const optInKeys = searchOptInKeys(groupName);
    const baseline = searchMetaPresetBaseline(groupName);
    if (!metaFilterSelectionMatches(visibility, baseline, optInKeys)) {
      params.set(paramName, metaFilterParamValue(visibility, optInKeys));
    }
  }
  for (const { groupName, key, paramName } of searchOptInMetaFilters) {
    const baseline = searchMetaPresetBaseline(groupName);
    if (searchMetaVisibility[groupName][key] && !baseline[key]) {
      params.set(paramName, '1');
    }
  }
  for (const plugin of browserSearchPlugins()) {
    const videoFacet = browserVideoFacetDefinition(plugin);
    if (videoFacet) {
      const state = browserVideoFacetState(plugin);
      const baseline = browserVideoFacetPresetBaseline(plugin);
      if (state.present !== baseline.present) {
        params.set(
          videoFacet.presentHashParam || `plugin-${plugin.id}-present`,
          state.present ? '1' : '0',
        );
      }
      if (state.absent !== baseline.absent) {
        params.set(
          videoFacet.absentHashParam || `plugin-${plugin.id}-absent`,
          state.absent ? '1' : '0',
        );
      }
    } else if (searchKindEnabled(plugin.id)) {
      params.set(plugin.search.hashParam || `plugin-${plugin.id}`, '1');
    }
  }
  if (searchSortExplicit || searchResultsSort !== preferredSearchResultsSort(query)) {
    params.set('sort', searchResultsSort);
  }
  if (currentPage > 1) params.set('page', String(currentPage));
  return `#search${params.toString() ? `?${params.toString()}` : ''}`;
}

function applySearchHash(hash) {
  retainedSearchHash = hash;
  historyNavigationDate = '';
  pendingHistoryDate = '';
  const queryIndex = hash.indexOf('?');
  const params = new URLSearchParams(queryIndex >= 0 ? hash.slice(queryIndex + 1) : '');
  search.value = params.get('q') || '';
  const searchInParam = params.get('in');
  if (searchInParam !== null) {
    const active = new Set(
      searchInParam === '__none__' ? [] : searchInParam.split(',').filter(Boolean)
    );
    for (const input of searchFields) {
      input.checked = active.has(input.dataset.searchField);
    }
  } else {
    for (const input of searchFields) input.checked = true;
  }
  applySearchPresetState(params.get('preset') || '', params.get('group') || '');
  const videoMetaParam = params.get(searchMetaParamNames.videos);
  const compatibleVideoMetaParam = videoMetaParam === null
    ? null
    : videoMetaParam.split(',').map(value => value === 'videos' ? 'public' : value).join(',');
  const metaParamValues = {
    videos: compatibleVideoMetaParam,
    reactions: params.get(searchMetaParamNames.reactions),
    completion: params.get(searchMetaParamNames.completion),
    membership: params.get(searchMetaParamNames.membership),
    uploaderCategory: params.get(searchMetaParamNames.uploaderCategory),
    channelSubscription: params.get(searchMetaParamNames.channelSubscription),
    channelStatus: params.get(searchMetaParamNames.channelStatus),
    playlistVisibility: params.get(searchMetaParamNames.playlistVisibility),
    playlistOwnership: params.get(searchMetaParamNames.playlistOwnership),
    playlistStatus: params.get(searchMetaParamNames.playlistStatus),
  };
  const uploaderCategoryParam = metaParamValues.uploaderCategory;
  uploaderCategorySelectionExplicit = uploaderCategoryParam !== null;
  if (uploaderCategoryParam && uploaderCategoryParam !== '__none__') {
    for (const category of uploaderCategoryParam.split(',').filter(Boolean)) {
      defaultSearchMetaVisibility.uploaderCategory[category] = true;
      searchMetaVisibility.uploaderCategory[category] = false;
    }
  }
  for (const [groupName, value] of Object.entries(metaParamValues)) {
    applyMetaFilterParam(
      searchMetaVisibility[groupName],
      value,
      searchOptInKeys(groupName),
    );
  }
  for (const { groupName, key, paramName } of searchOptInMetaFilters) {
    const legacyValue = metaParamValues[groupName];
    const legacySelected = (
      legacyValue !== null
      && legacyValue !== '__none__'
      && legacyValue.split(',').includes(key)
    );
    if (params.get(paramName) === '1' || legacySelected) {
      searchMetaVisibility[groupName][key] = true;
    }
  }
  for (const plugin of browserSearchPlugins()) {
    const videoFacet = browserVideoFacetDefinition(plugin);
    if (videoFacet) {
      const state = browserVideoFacetState(plugin);
      const presentParam = params.get(
        videoFacet.presentHashParam || `plugin-${plugin.id}-present`
      );
      const absentParam = params.get(
        videoFacet.absentHashParam || `plugin-${plugin.id}-absent`
      );
      if (presentParam !== null) state.present = presentParam === '1';
      if (absentParam !== null) state.absent = absentParam === '1';
    } else {
      const hashParam = plugin.search.hashParam || `plugin-${plugin.id}`;
      if (params.get(hashParam) === '1') pluginSearchVisibility.set(plugin.id, true);
    }
  }
  const requestedSort = params.get('sort') || '';
  searchSortExplicit = searchSortOptions.has(requestedSort);
  searchResultsSort = searchSortExplicit
    ? requestedSort
    : preferredSearchResultsSort(search.value.trim());
  if (browserSearchPlugins().some(plugin => (
    browserPluginSearchActive(plugin) && browserPluginForcesRelevance(plugin)
  ))) {
    searchResultsSort = 'relevance';
    searchSortExplicit = false;
  }
  const page = Number(params.get('page') || 1);
  currentPage = Number.isFinite(page) && page > 0 ? page : 1;
}

function updateSearchHash(replace = false) {
  const href = searchHash();
  retainedSearchHash = href;
  if (window.location.hash === href) return false;
  if (replace) {
    history.replaceState(null, '', href);
  } else {
    window.location.hash = href;
  }
  return true;
}

function hrefForCurrentSelection(includePagination = false) {
  if (selected.startsWith('__playlist__:')) {
    return localPlaylistHref(selected.slice('__playlist__:'.length), includePagination);
  }
  if (selected.startsWith('__video__:')) {
    return localVideoHref(selected.slice('__video__:'.length), includePagination);
  }
  if (selected.startsWith('__channel__:')) {
    return localChannelHref(selected.slice('__channel__:'.length), includePagination);
  }
  return localViewHref(selected, includePagination);
}

function updateCurrentHash(replace = false) {
  if (selected === '__search__') return updateSearchHash(replace);
  const href = hrefForCurrentSelection(true);
  if (window.location.hash === href) return false;
  if (replace) {
    history.replaceState(null, '', href);
  } else {
    window.location.hash = href;
  }
  return true;
}

function syncSearchHashAndRender(replaceHash = true) {
  if (selected === '__search__') {
    const hashChanged = updateSearchHash(replaceHash);
    renderGroups();
    if (hashChanged && !replaceHash) return;
  }
  render();
}

function selectionFromHash() {
  const hash = window.location.hash || '';
  if (hash === '#search' || hash.startsWith('#search?')) {
    applySearchHash(hash);
    return '__search__';
  }
  const { base, params } = hashParts(hash);
  const historyLocation = base === '#view=history' || base.startsWith('#channel=');
  applyPaginationParams(params, historyLocation);
  if (base.startsWith('#playlist=')) {
    const playlistId = decodeURIComponent(base.slice('#playlist='.length));
    if (playlistId) return playlistSelection(playlistId);
  }
  if (base.startsWith('#video=')) {
    const videoId = decodeURIComponent(base.slice('#video='.length));
    if (videoId) return videoSelection(videoId);
  }
  if (base.startsWith('#channel=')) {
    const channelId = decodeURIComponent(base.slice('#channel='.length));
    if (channelId) {
      if (historyNavigationDate) channelDetailTab = 'history';
      return channelSelection(channelId);
    }
  }
  if (base.startsWith('#view=')) {
    const view = decodeURIComponent(base.slice('#view='.length));
    if (selectionByViewHash.has(view)) return selectionByViewHash.get(view);
  }
  return '__search__';
}

function activateSearchPreset(preset, groupKey = '') {
  const definition = searchPresetDefinition(preset);
  if (!definition) return;
  const progressToken = beginSidebarNavigationProgress();
  selected = '__search__';
  if (!definition.preserveQuery) search.value = '';
  for (const input of searchFields) input.checked = true;
  applySearchPresetState(preset, groupKey);
  searchResultsSort = preferredSearchResultsSort('', preset);
  searchSortExplicit = false;
  currentPage = 1;
  const href = searchHash();
  if (window.location.hash !== href) {
    window.location.hash = href;
    return;
  }
  renderGroups();
  void render().finally(() => finishSidebarNavigationProgress(progressToken));
}

function activateSearchFromHistory({ resetMetaVisibility = false } = {}) {
  if (selected !== '__history__') return false;
  selected = '__search__';
  activeSearchPreset = '';
  searchPlaylistGroupKey = '';
  searchChannelGroupKey = '';
  searchSortExplicit = false;
  currentPage = 1;
  searchFilters.classList.remove('view-inactive');
  if (resetMetaVisibility) {
    resetSearchMetaVisibility();
    renderSearchMetaFilters();
  }
  searchResultsSort = preferredSearchResultsSort(search.value.trim(), '');
  renderGroups();
  return true;
}

function activateUnscopedSearch() {
  const progressToken = beginSidebarNavigationProgress();
  selected = '__search__';
  activeSearchPreset = '';
  searchPlaylistGroupKey = '';
  searchChannelGroupKey = '';
  resetSearchMetaVisibility();
  searchResultsSort = preferredSearchResultsSort(search.value.trim(), '');
  searchSortExplicit = false;
  currentPage = 1;
  const href = searchHash();
  if (window.location.hash !== href) {
    window.location.hash = href;
    return;
  }
  renderGroups();
  void render().finally(() => finishSidebarNavigationProgress(progressToken));
}

function activateSearchNavigation() {
  if (selected === '__search__') {
    activateUnscopedSearch();
    return;
  }
  beginSidebarNavigationProgress();
  window.location.hash = retainedSearchHash;
}

function setSelected(value) {
  const progressToken = beginSidebarNavigationProgress();
  selected = value;
  if (value === '__history__') search.value = '';
  currentPage = 1;
  if (value.startsWith('__playlist__:')) {
    const playlistId = value.slice('__playlist__:'.length);
    resetPlaylistVisibilityFor(playlistId);
    if (window.location.hash !== localPlaylistHref(playlistId)) {
      window.location.hash = localPlaylistHref(playlistId);
      return;
    }
  } else if (value.startsWith('__video__:')) {
    const videoId = value.slice('__video__:'.length);
    if (window.location.hash !== localVideoHref(videoId)) {
      window.location.hash = localVideoHref(videoId);
      return;
    }
  } else if (value.startsWith('__channel__:')) {
    const channelId = value.slice('__channel__:'.length);
    if (window.location.hash !== localChannelHref(channelId)) {
      window.location.hash = localChannelHref(channelId);
      return;
    }
  } else {
    const href = localViewHref(value);
    if (window.location.hash !== href) {
      window.location.hash = href;
      return;
    }
  }
  renderGroups();
  void render().finally(() => finishSidebarNavigationProgress(progressToken));
}

function resetPlaylistVisibilityFor(playlistId) {
  if (playlistVisibilityPlaylistId === playlistId) return;
  playlistVisibilityPlaylistId = playlistId;
  playlistVisibility = defaultPlaylistVideoVisibility();
  playlistCompletionVisibility = {
    complete: true,
    partial: true,
    partial_below_minimum: defaultPartialBelowMinimumEnabled(),
    unknown: true,
    never_watched: true,
  };
  playlistDuplicatesOnly = false;
  playlistPageSearch = '';
}

function groupCount(groupKey, membershipMap, childMap) {
  const identifiers = new Set();
  const pending = [groupKey];
  const visited = new Set();
  while (pending.length) {
    const current = pending.pop();
    if (!current || visited.has(current)) continue;
    visited.add(current);
    for (const identifier of membershipMap.get(current) || []) identifiers.add(identifier);
    for (const child of childMap.get(current) || []) pending.push(child.group_key);
  }
  return identifiers.size;
}

function activeSearchFields() {
  return new Set(
    searchFields
      .filter(input => input.checked)
      .map(input => input.dataset.searchField)
  );
}

function syncFilterGroup(parent, childFilters, dimChildrenWhenUnchecked = true) {
  if (!parent || !childFilters.length) return;
  const checkedCount = childFilters.filter(input => input.checked).length;
  parent.checked = checkedCount > 0;
  parent.indeterminate = checkedCount > 0 && checkedCount < childFilters.length;
  setFilterDimmed(childFilters, dimChildrenWhenUnchecked && !parent.checked);
}

function metaFilterGroupVisibility(groupName) {
  if (groupName.startsWith('search-plugin-')) {
    const pluginId = groupName.slice('search-plugin-'.length);
    const plugin = browserVideoFilterPlugins().find(item => item.id === pluginId);
    return plugin ? browserVideoFacetState(plugin) : null;
  }
  const groups = {
    'playlist-videos': playlistVisibility,
    'playlist-completion': playlistCompletionVisibility,
    'search-videos': searchMetaVisibility.videos,
    'search-reactions': searchMetaVisibility.reactions,
    'search-completion': searchMetaVisibility.completion,
    'search-membership': searchMetaVisibility.membership,
    'search-uploaderCategory': searchMetaVisibility.uploaderCategory,
    'search-channelSubscription': searchMetaVisibility.channelSubscription,
    'search-channelStatus': searchMetaVisibility.channelStatus,
    'search-playlistVisibility': searchMetaVisibility.playlistVisibility,
    'search-playlistOwnership': searchMetaVisibility.playlistOwnership,
    'search-playlistStatus': searchMetaVisibility.playlistStatus,
  };
  return groups[groupName] || null;
}

function metaFilterGroupExcludedKeys(groupName) {
  return groupName === 'playlist-videos' ? new Set(['removed']) : new Set();
}

function setMetaFilterGroup(groupName, checked) {
  const group = metaFilterGroupVisibility(groupName);
  if (!group) return false;
  const root = groupName.startsWith('search-') ? searchForFilters : meta;
  const excludedKeys = metaFilterGroupExcludedKeys(groupName);
  for (const key of Object.keys(group)) {
    if (!excludedKeys.has(key)) group[key] = checked;
  }
  for (const input of root.querySelectorAll(`[data-meta-child-filter="${groupName}"]`)) {
    input.checked = checked;
  }
  return true;
}

function allMetaFilterChildrenChecked(groupName) {
  const group = metaFilterGroupVisibility(groupName);
  if (!group) return false;
  const excludedKeys = metaFilterGroupExcludedKeys(groupName);
  const children = Object.entries(group).filter(([key]) => !excludedKeys.has(key));
  return children.length > 0 && children.every(([, checked]) => checked);
}

function syncMetaFilterGroup(groupName) {
  const root = groupName.startsWith('search-') ? searchForFilters : meta;
  syncFilterGroup(
    root.querySelector(`[data-meta-all-filter="${groupName}"]`),
    [...root.querySelectorAll(`[data-meta-child-filter="${groupName}"]`)],
    false,
  );
}

const searchVideoFacetKeys = [
  'videos',
  'reactions',
  'completion',
  'membership',
  'uploaderCategory',
];
const searchPlaylistFacetKeys = ['playlistVisibility', 'playlistOwnership', 'playlistStatus'];
const searchChannelFacetKeys = ['channelSubscription', 'channelStatus'];

function searchKindFacetKeys(kind) {
  if (kind === 'videos') return searchVideoFacetKeys;
  if (kind === 'playlists') return searchPlaylistFacetKeys;
  if (kind === 'channels') return searchChannelFacetKeys;
  return [];
}

function setSearchKindFilter(kind, checked) {
  const plugin = browserSearchPlugin(kind);
  if (plugin) {
    if (browserVideoFacetDefinition(plugin)) {
      Object.assign(browserVideoFacetState(plugin), { present: checked, absent: checked });
    } else {
      pluginSearchVisibility.set(plugin.id, checked);
    }
    return true;
  }
  const facetKeys = searchKindFacetKeys(kind);
  if (!facetKeys.every(key => searchMetaVisibility[key])) return false;
  for (const facetKey of facetKeys) {
    for (const key of Object.keys(searchMetaVisibility[facetKey])) {
      searchMetaVisibility[facetKey][key] = checked;
    }
    for (const input of searchForFilters.querySelectorAll(`[data-search-meta-filter^="${facetKey}:"]`)) {
      input.checked = checked;
    }
    syncMetaFilterGroup(`search-${facetKey}`);
  }
  if (kind === 'videos') {
    for (const videoFilter of browserVideoFilterPlugins()) {
      Object.assign(
        browserVideoFacetState(videoFilter),
        { present: checked, absent: checked },
      );
      for (const input of searchForFilters.querySelectorAll(
        `[data-meta-child-filter="search-plugin-${videoFilter.id}"]`
      )) {
        input.checked = checked;
      }
      syncMetaFilterGroup(`search-plugin-${videoFilter.id}`);
    }
  }
  return true;
}

function syncSearchKindFilter(kind, applyDisabledStyles = true) {
  const parent = searchForFilters.querySelector(`[data-search-kind-filter="${kind}"]`);
  if (!(parent instanceof HTMLInputElement)) return;
  if (browserSearchPlugin(kind)) {
    parent.checked = searchKindEnabled(kind);
    parent.indeterminate = false;
    if (applyDisabledStyles) {
      parent.closest('.search-meta-kind')?.classList.toggle('kind-disabled', !parent.checked);
    }
    return;
  }
  const facetKeys = searchKindFacetKeys(kind);
  if (facetKeys.length > 1) {
    const facetSelections = facetKeys.map(
      key => Object.values(searchMetaVisibility[key])
    );
    if (kind === 'videos') {
      facetSelections.push(
        ...browserVideoFilterPlugins().map(plugin => (
          Object.values(browserVideoFacetState(plugin))
        )),
      );
    }
    const everyFacetHasSelection = facetSelections.every(values => values.some(Boolean));
    const allChildrenSelected = facetSelections.every(values => values.every(Boolean));
    parent.checked = everyFacetHasSelection;
    parent.indeterminate = everyFacetHasSelection && !allChildrenSelected;
    if (applyDisabledStyles) {
      for (const row of searchForFilters.querySelectorAll(`[data-search-kind-facet="${kind}"]`)) {
        row.classList.toggle('dimmed', !everyFacetHasSelection);
      }
      parent.closest('.search-meta-kind')?.classList.toggle('kind-disabled', !everyFacetHasSelection);
    }
    return;
  }
  syncFilterGroup(
    parent,
    [...searchForFilters.querySelectorAll(`[data-meta-child-filter="search-${kind}"]`)],
  );
  if (applyDisabledStyles) {
    parent.closest('.search-meta-kind')?.classList.toggle('kind-disabled', !parent.checked);
  }
}

function searchKindForFacet(facetKey) {
  if (searchVideoFacetKeys.includes(facetKey)) return 'videos';
  if (searchPlaylistFacetKeys.includes(facetKey)) return 'playlists';
  if (searchChannelFacetKeys.includes(facetKey)) return 'channels';
  return facetKey;
}

function refreshSearchAfterFilterChange(groupName, activatedFromHistory) {
  currentPage = 1;
  syncSearchKindFilter(searchKindForFacet(groupName), false);
  reconcileSearchPreset();
  showSearchMetaProgress(groupName);
  syncSearchHashAndRender(!activatedFromHistory);
}

function restoreEmptySearchKindFacets(facetKey) {
  const kind = searchKindForFacet(facetKey);
  const facetKeys = searchKindFacetKeys(kind);
  if (facetKeys.length < 2) return;
  for (const siblingKey of facetKeys) {
    if (siblingKey === facetKey) continue;
    if (Object.values(searchMetaVisibility[siblingKey]).some(Boolean)) continue;
    const defaults = defaultSearchMetaVisibility[siblingKey];
    Object.assign(searchMetaVisibility[siblingKey], defaults);
    for (const input of searchForFilters.querySelectorAll(`[data-search-meta-filter^="${siblingKey}:"]`)) {
      const filterName = input.dataset.searchMetaFilter.split(':')[1];
      input.checked = Boolean(defaults[filterName]);
    }
    syncMetaFilterGroup(`search-${siblingKey}`);
  }
  if (kind !== 'videos') return;
  for (const plugin of browserVideoFilterPlugins()) {
    const state = browserVideoFacetState(plugin);
    if (Object.values(state).some(Boolean)) continue;
    Object.assign(state, { present: true, absent: true });
    for (const input of searchForFilters.querySelectorAll(
      `[data-search-plugin-facet-filter^="${plugin.id}:"]`
    )) {
      const filterName = input.dataset.searchPluginFacetFilter.split(':')[1];
      input.checked = Boolean(state[filterName]);
    }
    syncMetaFilterGroup(`search-plugin-${plugin.id}`);
  }
}

function setFilterDimmed(inputs, dimmed) {
  for (const input of inputs) {
    const label = input?.closest?.('.filter, .meta-filter');
    if (label) label.classList.toggle('dimmed', dimmed);
  }
}

function isSubscribedChannel(channel) {
  return Number(channel.subscribed || 0) === 1;
}

const channelNotificationOptions = {
  all: {
    label: 'All',
    title: 'All notifications',
    svg: '<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24" focusable="false" aria-hidden="true"><path d="M19.395 1.196a1 1 0 00-.199 1.4A9 9 0 0121 8a1 1 0 002 0 11 11 0 00-2.205-6.605 1 1 0 00-1.4-.199Zm-16.192.2A11 11 0 001 8a1 1 0 002 0 9 9 0 011.803-5.404 1 1 0 00-1.6-1.2ZM12 1a7 7 0 00-7 7v4.446a1 1 0 01-.144.515L3.05 15.972C2.25 17.305 3.21 19 4.766 19H8a4 4 0 108 0h3.233c1.555 0 2.515-1.695 1.715-3.029l-1.805-3.01a1 1 0 01-.143-.515V8a7 7 0 00-7-7Zm0 2a5 5 0 015 5v4.445a3 3 0 00.428 1.545L19.233 17H4.766l1.806-3.01c.28-.466.428-1 .428-1.544V8a5 5 0 015-5Zm-2 16h4a2 2 0 01-4 0Z"></path></svg>',
  },
  personalized: {
    label: 'Personalized',
    title: 'Personalized notifications',
    svg: '<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24" focusable="false" aria-hidden="true"><path d="M16 19a4 4 0 11-8 0H4.765C3.21 19 2.25 17.304 3.05 15.97l1.806-3.01A1 1 0 005 12.446V8a7 7 0 0114 0v4.446c0 .181.05.36.142.515l1.807 3.01c.8 1.333-.161 3.029-1.716 3.029H16ZM12 3a5 5 0 00-5 5v4.446a3 3 0 01-.428 1.543L4.765 17h14.468l-1.805-3.01A3 3 0 0117 12.445V8a5 5 0 00-5-5Zm-2 16a2 2 0 104 0h-4Z"></path></svg>',
  },
  none: {
    label: 'None',
    title: 'No notifications',
    svg: '<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24" focusable="false" aria-hidden="true"><path d="M12 1a7 7 0 00-6.213 3.774l1.719 1.032A5 5 0 0117 8v3.502l2 1.199V8a7 7 0 00-7-7ZM1.141 5.485a1 1 0 00.343 1.372l3.514 2.109v3.48a1 1 0 01-.143.514L3.05 15.97c-.8 1.334.16 3.03 1.716 3.03H8a4 4 0 108 0l6-.001a1 1 0 00.515-1.856l-20-12a1 1 0 00-1.373.342ZM7 12.446v-2.28L18.39 17H4.766l1.806-3.011A3 3 0 007 12.446ZM10 19h4a2 2 0 01-4 0Z"></path></svg>',
  },
};

function channelNotificationHtml(channel) {
  if (!isSubscribedChannel(channel)) return '';
  const notificationLevel = String(channel.notification_level || '').trim().toLowerCase();
  const option = channelNotificationOptions[notificationLevel];
  if (!option) return '';
  return `<span class="channel-notification" title="${option.title}">${option.svg}<span>${option.label}</span></span>`;
}

function playlistVisibilityCategory(playlist) {
  const value = String(playlist.visibility || '').trim().toLowerCase();
  if (['private', 'public', 'unlisted'].includes(value)) return value;
  return 'unknown';
}

function displayPlaylistVisibility(playlist) {
  const value = playlistVisibilityCategory(playlist);
  return value === 'unknown' ? '' : value[0].toUpperCase() + value.slice(1);
}

function playlistVisibilityLabelHtml(playlist) {
  const value = playlistVisibilityCategory(playlist);
  return visibilityLabelHtml(value, displayPlaylistVisibility(playlist));
}

function visibilityFilterLabelHtml(value, count) {
  const countText = filterCountText(count);
  if (!['private', 'public', 'unlisted'].includes(value)) {
    return `<span>${escapeHtml(value)} <span class="meta-filter-count">${countText}</span></span>`;
  }
  return `<span class="visibility-label">${visibilityIconSvg(value)}<span>${escapeHtml(value)} <span class="meta-filter-count">${countText}</span></span></span>`;
}

function visibilityLabelHtml(value, label) {
  if (!label || !['private', 'public', 'unlisted'].includes(value)) return label ? escapeHtml(label) : '';
  return `<span class="visibility-label">${visibilityIconSvg(value)}<span>${escapeHtml(label)}</span></span>`;
}

function visibilityIconSvg(value) {
  if (value === 'public') {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><path d="M2 12h20"></path><path d="M12 2a15.3 15.3 0 0 1 0 20"></path><path d="M12 2a15.3 15.3 0 0 0 0 20"></path></svg>';
  }
  if (value === 'unlisted') {
    return '<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24" fill="currentColor" focusable="false" aria-hidden="true"><path d="M9 18c.226 0 .448-.012.667-.037A8.001 8.001 0 018.07 16H7a4 4 0 110-8h2a4 4 0 014 4 2 2 0 001.668 1.973A5.999 5.999 0 009 6H7a6 6 0 100 12h2Zm8 0a6 6 0 100-12h-2c-.225 0-.448.012-.667.036A8 8 0 0115.93 8H17a4 4 0 110 8h-2a4 4 0 01-4-4 2 2 0 00-1.668-1.973A6 6 0 0015 18h2Z"></path></svg>';
  }
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 8 0v4"></path></svg>';
}

function usefulMetadataTitle(video) {
  const title = String(video.metadata_title || '').trim();
  if (!title || title === '- YouTube' || title === 'YouTube') return '';
  return title;
}

function displayVideoTitle(video) {
  return usefulMetadataTitle(video) || video.title || '';
}

function displayVideoChannel(video) {
  return video.metadata_channel || video.channel || '';
}

function displayVideoChannelUrl(video) {
  return video.metadata_channel_url || '';
}

function displayVideoChannelLocalUrl(video) {
  const channelId = video.metadata_channel_id || video.channel_id || video.recovered_channel_id || '';
  const channelReference = video.metadata_channel_reference || channelId;
  return channelReference ? localChannelHref(channelReference) : displayVideoChannelUrl(video);
}

function displayVideoDuration(video) {
  return video.metadata_duration || video.duration_text || '';
}

function displayVideoUploadDate(video) {
  const value = video.metadata_upload_date || '';
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const timeZone = window.YTLibraryTime.timeZone || window.YTLibraryTime.detected();
  const date = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeZone }).format(parsed);
  const time = new Intl.DateTimeFormat(undefined, { timeStyle: 'short', timeZone }).format(parsed);
  return `${date} ${time}`;
}

function latestWatchedAtLabel(video) {
  const value = String(video.latest_watch_at || '').trim();
  if (!value) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return window.YTLibraryTime.formatDate(value);
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const timeZone = window.YTLibraryTime.timeZone || window.YTLibraryTime.detected();
  const date = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeZone }).format(parsed);
  const time = new Intl.DateTimeFormat(undefined, { timeStyle: 'short', timeZone }).format(parsed);
  return `${date} ${time}`;
}

function latestWatchDateHtml(video) {
  const watchedAt = latestWatchedAtLabel(video);
  return watchedAt
    ? `<div class="details"><span>Last watched ${escapeHtml(watchedAt)}</span></div>`
    : '';
}

function youtubeWatchUrl(video) {
  if (!video.video_id) return '';
  const list = video.playlist_id ? `&list=${encodeURIComponent(video.playlist_id)}` : '';
  return `https://www.youtube.com/watch?v=${encodeURIComponent(video.video_id)}${list}`;
}

function matchTypeLabel(video) {
  if (video.source_type) return '';
  if (video.match_type === 'ambiguous_hidden_candidate') return '';
  const label = video.match_label || '';
  if (/^matched by\b/i.test(label)) return '';
  return label;
}

function wasRemovedFromPlaylist(video) {
  return video.source_quality === 'takeout' && video.match_type === 'ambiguous_hidden_candidate';
}

function isUnavailablePlaylistVideo(video) {
  return !Number(video.is_playable || 0) || Boolean(unavailableLabel(video));
}

function wasRemovedByMeFromPlaylist(video) {
  if (video.collection_category) return video.collection_category === 'removed';
  return wasRemovedFromPlaylist(video) && !isUnavailablePlaylistVideo(video);
}

function pageSizeNumber() {
  if (pageSize === 'all') return Infinity;
  const value = Number(pageSize || 100);
  return Number.isFinite(value) && value > 0 ? value : 100;
}

function pageSlice(rows) {
  const size = pageSizeNumber();
  if (!Number.isFinite(size)) {
    currentPage = 1;
    return { pageRows: rows, page: 1, pageCount: 1, start: rows.length ? 1 : 0, end: rows.length, total: rows.length };
  }
  const pageCount = Math.max(1, Math.ceil(rows.length / size));
  currentPage = Math.min(Math.max(1, currentPage), pageCount);
  const startIndex = (currentPage - 1) * size;
  const endIndex = Math.min(rows.length, startIndex + size);
  return {
    pageRows: rows.slice(startIndex, endIndex),
    page: currentPage,
    pageCount,
    start: rows.length ? startIndex + 1 : 0,
    end: endIndex,
    total: rows.length,
  };
}

function pageJumpItems(pageInfo) {
  if (pageInfo.pageCount <= 1) return [];
  if (pageInfo.pageCount <= 7) {
    const items = Array.from({ length: pageInfo.pageCount }, (_, index) => index + 1);
    while (items.length < 7) items.push('placeholder');
    return items;
  }
  const page = pageInfo.page;
  const last = pageInfo.pageCount;
  if (page <= 3) {
    const leading = Array.from({ length: 4 - page }, () => 'placeholder');
    const pages = Array.from({ length: Math.min(page + 1, last) }, (_, index) => index + 1);
    const items = [...leading, ...pages, 'ellipsis', last];
    while (items.length < 7) items.push('placeholder');
    return items.slice(0, 7);
  }
  if (page >= last - 2) {
    const firstPage = Math.max(1, page - 1);
    const pages = Array.from({ length: last - firstPage + 1 }, (_, index) => firstPage + index);
    const trailing = Array.from({ length: Math.max(0, 7 - 2 - pages.length) }, () => 'placeholder');
    const items = [1, 'ellipsis', ...pages, ...trailing];
    while (items.length < 7) items.push('placeholder');
    return items.slice(0, 7);
  }
  return [1, 'ellipsis', page - 1, page, page + 1, 'ellipsis', last];
}

function pageJumpHtml(pageInfo) {
  const items = pageJumpItems(pageInfo);
  if (!items.length) return '';
  return `
    <span class="page-jumps">
      ${items.map(item => {
        if (item === 'ellipsis') return '<span class="ellipsis">...</span>';
        if (item === 'placeholder') return '<span class="page-placeholder">&nbsp;</span>';
        return `<button type="button" class="${item === pageInfo.page ? 'current' : ''}" data-page="${item}" ${item === pageInfo.page ? 'disabled' : ''}>${item}</button>`;
      }).join('')}
    </span>
  `;
}

function pageGotoHtml(pageInfo) {
  if (pageInfo.pageCount <= 1) return '';
  return `
    <form class="page-goto" data-page-goto>
      <label>Go to
        <input type="number" data-page-goto-input min="1" max="${pageInfo.pageCount}" value="${pageInfo.page}" inputmode="numeric">
      </label>
      <button type="submit">Go</button>
    </form>
  `;
}

function pagerHtml(pageInfo, includePageSize = false) {
  if (!pageInfo.total) return '';
  const pageSizes = ['50', '100', '250', '500'];
  const sizeHtml = includePageSize ? `
    <label>Page size
      <select data-page-size>
        ${pageSizes.map(value => `<option value="${value}" ${pageSize === value ? 'selected' : ''}>${value === 'all' ? 'All' : value}</option>`).join('')}
      </select>
    </label>
  ` : '';
  return `
    ${sizeHtml}
    <span>Showing ${pageInfo.start}-${pageInfo.end} of ${pageInfo.total}</span>
    ${pageJumpHtml(pageInfo)}
    ${pageGotoHtml(pageInfo)}
  `;
}

function usesDocumentPageScrolling() {
  return window.matchMedia('(max-width: 760px)').matches;
}

function pageScrollBoundaryState() {
  if (usesDocumentPageScrolling()) {
    const scrolling = document.scrollingElement || document.documentElement;
    const top = Number(scrolling.scrollTop || window.scrollY || 0);
    const maximum = Math.max(0, scrolling.scrollHeight - window.innerHeight);
    return { atTop: top <= 1, atBottom: top >= maximum - 1 };
  }
  if (!(resultsScroll instanceof HTMLElement)) return { atTop: false, atBottom: false };
  const maximum = Math.max(0, resultsScroll.scrollHeight - resultsScroll.clientHeight);
  return {
    atTop: resultsScroll.scrollTop <= 1,
    atBottom: resultsScroll.scrollTop >= maximum - 1,
  };
}

function pageBoundaryTargetAllowed(target) {
  if (!(target instanceof Element)) return false;
  if (target.closest('input, select, textarea')) return false;
  return Boolean(resultsScroll?.contains(target) || bottomPager.contains(target));
}

function armPageBoundaryInputAfterIdle(delay = 260) {
  if (pageBoundaryInputTimer !== null) window.clearTimeout(pageBoundaryInputTimer);
  pageBoundaryInputTimer = window.setTimeout(() => {
    pageBoundaryInputTimer = null;
    pageBoundaryInputArmed = true;
  }, delay);
}

function finishPageBoundaryLanding() {
  const landing = pendingPageBoundaryLanding;
  if (!landing) return;
  pendingPageBoundaryLanding = '';
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      if (usesDocumentPageScrolling()) {
        if (landing === 'top') {
          const top = window.scrollY + grid.getBoundingClientRect().top;
          window.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
        } else {
          const scrolling = document.scrollingElement || document.documentElement;
          window.scrollTo({
            top: Math.max(0, scrolling.scrollHeight - window.innerHeight),
            behavior: 'auto',
          });
        }
      } else if (resultsScroll instanceof HTMLElement) {
        resultsScroll.scrollTop = landing === 'top'
          ? 0
          : Math.max(0, resultsScroll.scrollHeight - resultsScroll.clientHeight);
      }
      pageBoundaryNavigationPending = false;
      pageBoundaryInputArmed = false;
      armPageBoundaryInputAfterIdle(350);
      if (pageBoundaryFallbackTimer !== null) {
        window.clearTimeout(pageBoundaryFallbackTimer);
        pageBoundaryFallbackTimer = null;
      }
    });
  });
}

function navigateAcrossPageBoundary(direction) {
  if (pageBoundaryNavigationPending || !pageBoundaryInputArmed) return false;
  const nextPage = Number(renderedPageInfo.page || currentPage) + direction;
  const pageCount = Number(renderedPageInfo.pageCount || 1);
  if (nextPage < 1 || nextPage > pageCount) return false;
  pageBoundaryNavigationPending = true;
  pageBoundaryInputArmed = false;
  pendingPageBoundaryLanding = direction > 0 ? 'top' : 'bottom';
  currentPage = nextPage;
  historyNavigationDate = '';
  pendingHistoryDate = '';
  if (pageBoundaryFallbackTimer !== null) window.clearTimeout(pageBoundaryFallbackTimer);
  pageBoundaryFallbackTimer = window.setTimeout(() => {
    pageBoundaryFallbackTimer = null;
    pageBoundaryNavigationPending = false;
    pendingPageBoundaryLanding = '';
    pageBoundaryInputArmed = false;
    armPageBoundaryInputAfterIdle();
  }, 10000);
  if (!updateCurrentHash(false)) void render();
  return true;
}

function renderPager(pageInfo) {
  renderedPageInfo = {
    page: Number(pageInfo.page || 1),
    pageCount: Number(pageInfo.pageCount || 1),
    total: Number(pageInfo.total || 0),
  };
  bottomPager.hidden = !pageInfo.total;
  bottomPager.innerHTML = pagerHtml(pageInfo, true);
  if (currentHashHasPaginationParams()) updateCurrentHash(true);
  finishPageBoundaryLanding();
}

function hidePager() {
  renderedPageInfo = { page: 1, pageCount: 1, total: 0 };
  bottomPager.hidden = true;
  bottomPager.replaceChildren();
}

function pagedRows(rows) {
  const pageInfo = pageSlice(rows);
  renderPager(pageInfo);
  return pageInfo;
}

function stopSearchProgress() {
  grid.removeAttribute('aria-busy');
  meta.removeAttribute('aria-busy');
}

function animateProgressDots(update) {
  let dotCount = 1;
  update('.');
  return setInterval(() => {
    dotCount = dotCount === 3 ? 1 : dotCount + 1;
    update('.'.repeat(dotCount));
  }, 280);
}

function progressMessageAnimation(container, labelText) {
  const label = document.createElement('span');
  label.textContent = labelText;
  const dots = document.createElement('span');
  dots.className = 'searching-dots';
  container.replaceChildren(label, dots);
  return animateProgressDots(value => {
    dots.textContent = value;
  });
}

function updateSearchMetaProgress(dotsText = searchMetaProgressDots) {
  searchMetaProgressDots = dotsText;
  for (const dots of searchForFilters.querySelectorAll('[data-search-meta-progress]')) {
    const active = pendingSearchMetaGroups.has(dots.dataset.searchMetaProgress);
    dots.classList.toggle('active', active);
    dots.textContent = active ? dotsText : '';
    const row = dots.closest('.search-meta-kind');
    if (!row) continue;
    if (active) {
      row.setAttribute('aria-busy', 'true');
    } else {
      row.removeAttribute('aria-busy');
    }
  }
}

function stopSearchMetaProgress() {
  if (searchMetaProgressTimer !== null) {
    clearInterval(searchMetaProgressTimer);
    searchMetaProgressTimer = null;
  }
  pendingSearchMetaGroups.clear();
  updateSearchMetaProgress('');
}

function showSearchMetaProgress(groupName) {
  const progressGroup = searchKindForFacet(groupName);
  pendingSearchMetaGroups.add(progressGroup);
  showSearchHeaderProgress();
  if (searchMetaProgressTimer === null) {
    searchMetaProgressTimer = animateProgressDots(updateSearchMetaProgress);
  } else {
    updateSearchMetaProgress();
  }
}

function stopSearchHeaderProgress(progressToken = null) {
  if (progressToken !== null && progressToken !== searchHeaderProgressToken) return;
  if (searchHeaderProgressTimer !== null) {
    clearInterval(searchHeaderProgressTimer);
    searchHeaderProgressTimer = null;
  }
  searchProgressStatus.hidden = true;
  searchProgressStatus.removeAttribute('aria-busy');
  searchProgressStatus.replaceChildren();
}

function showSearchHeaderProgress() {
  stopSearchHeaderProgress();
  const progressToken = ++searchHeaderProgressToken;
  searchProgressStatus.hidden = false;
  searchProgressStatus.setAttribute('aria-busy', 'true');
  searchHeaderProgressTimer = progressMessageAnimation(searchProgressStatus, 'Loading');
  return progressToken;
}

function beginSidebarNavigationProgress() {
  pendingSidebarProgressToken = showSearchHeaderProgress();
  return pendingSidebarProgressToken;
}

function finishSidebarNavigationProgress(progressToken) {
  if (pendingSidebarProgressToken === progressToken) pendingSidebarProgressToken = null;
  stopSearchHeaderProgress(progressToken);
}

function showSearchProgress({ preserveContent = false } = {}) {
  stopSearchProgress();
  grid.setAttribute('aria-busy', 'true');
  meta.setAttribute('aria-busy', 'true');
  if (preserveContent) return;
  grid.className = 'grid';
  grid.replaceChildren();
  empty.hidden = true;
}

function remotePageInfo(total, rowsLength, remoteLimit = 0) {
  const size = pageSizeNumber();
  const effectiveSize = remoteLimit || (Number.isFinite(size) ? size : 1000);
  const pageCount = Math.max(1, Math.ceil(total / effectiveSize));
  currentPage = Math.min(Math.max(1, currentPage), pageCount);
  const start = total ? (currentPage - 1) * effectiveSize + 1 : 0;
  const end = total ? Math.min(total, start + rowsLength - 1) : 0;
  return { pageRows: [], page: currentPage, pageCount, start, end, total };
}

function remotePayloadPageInfo(payload, rowsLength) {
  const limit = Number(payload.limit || pageSizeNumber() || 100);
  currentPage = Math.floor(Number(payload.offset || 0) / limit) + 1;
  return remotePageInfo(Number(payload.total || 0), rowsLength, limit);
}

function cancelAdjacentPagePrefetch() {
  adjacentPagePrefetchGeneration += 1;
  if (adjacentPagePrefetchCancel) adjacentPagePrefetchCancel();
  adjacentPagePrefetchCancel = null;
}

function scheduleAdjacentPagePrefetch(pageInfo, fetchPage, additionalRequests = []) {
  cancelAdjacentPagePrefetch();
  const page = Number(pageInfo.page || 1);
  const pageCount = Number(pageInfo.pageCount || 1);
  const pages = [page + 1, page - 1].filter(candidate => (
    candidate >= 1 && candidate <= pageCount
  ));
  const pageRequests = pages.map(candidate => () => fetchPage(candidate));
  if (!pageRequests.length && !additionalRequests.length) return;
  const generation = adjacentPagePrefetchGeneration;
  const runRequests = async requests => {
    for (const request of requests) {
      if (generation !== adjacentPagePrefetchGeneration) return;
      try {
        await request();
      } catch (_error) {
        // A speculative request must not affect normal page loading.
      }
    }
  };
  const run = async () => {
    adjacentPagePrefetchCancel = null;
    await Promise.all([
      runRequests(additionalRequests),
      runRequests(pageRequests),
    ]);
  };
  const handle = window.setTimeout(() => void run(), 150);
  adjacentPagePrefetchCancel = () => window.clearTimeout(handle);
}

async function fetchHistoryPage(channelId = '', page = currentPage) {
  const size = pageSizeNumber();
  const limit = Number.isFinite(size) ? size : 1000;
  const requestedPage = Math.max(1, Number(page) || 1);
  const offset = (requestedPage - 1) * limit;
  const key = `${channelId}:${limit}:${offset}`;
  return cachedRequest(historyPageCache, key, async () => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (channelId) params.set('channel_id', channelId);
    const response = await fetch(`/api/history/search?${params}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`History fetch failed: ${response.status}`);
    const payload = await response.json();
    if (channelId) {
      channelHistoryCounts.set(
        channelId,
        Number(payload.totals?.filtered_watch_rows ?? payload.totals?.watch_rows ?? 0),
      );
    }
    return payload;
  }, adjacentPageCacheLimit);
}

function cachedChannelHistoryCount(channelId) {
  const value = channelHistoryCounts.get(channelId);
  return typeof value === 'number' ? value : null;
}

async function fetchChannelHistoryCount(channelId) {
  const cached = channelHistoryCounts.get(channelId);
  if (typeof cached === 'number') return cached;
  if (cached) return cached;
  const request = fetch(`/api/history/search?${new URLSearchParams({ channel_id: channelId, limit: '1' })}`, { cache: 'no-store' })
    .then(response => {
      if (!response.ok) throw new Error(`Channel history fetch failed: ${response.status}`);
      return response.json();
    })
    .then(payload => {
      const total = Number(payload.totals?.filtered_watch_rows ?? payload.totals?.watch_rows ?? 0);
      channelHistoryCounts.set(channelId, total);
      return total;
    })
    .catch(error => {
      channelHistoryCounts.delete(channelId);
      throw error;
    });
  channelHistoryCounts.set(channelId, request);
  return request;
}

function localDateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function historyActivityRange(yearOffset = historyActivityYearOffset) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const targetYear = today.getFullYear() - yearOffset;
  const lastDayOfTargetMonth = new Date(targetYear, today.getMonth() + 1, 0).getDate();
  const displayEnd = new Date(targetYear, today.getMonth(), Math.min(today.getDate(), lastDayOfTargetMonth));
  const start = new Date(displayEnd);
  start.setDate(start.getDate() - start.getDay() - (52 * 7));
  const end = new Date(start);
  end.setDate(end.getDate() + (53 * 7) - 1);
  return { start, end, displayEnd, startKey: localDateKey(start), endKey: localDateKey(end) };
}

function historyRowDateKey(row) {
  for (const value of [row?.watched_at, row?.watch_date]) {
    const dateKey = window.YTLibraryTime.dateKey(value);
    if (dateKey) return dateKey;
  }
  return '';
}

function historyActivityYearOffsetForDate(dateKey) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateKey)) return historyActivityYearOffset;
  const currentRange = historyActivityRange(historyActivityYearOffset);
  if (dateKey >= currentRange.startKey && dateKey <= currentRange.endKey) {
    return historyActivityYearOffset;
  }
  const today = new Date();
  const maximumOffset = Math.max(0, today.getFullYear() - Number(dateKey.slice(0, 4)) + 2);
  for (let offset = 0; offset <= maximumOffset; offset += 1) {
    const range = historyActivityRange(offset);
    if (dateKey >= range.startKey && dateKey <= range.endKey) return offset;
  }
  return maximumOffset;
}

function syncHistoryActivityYearWithRows(rows, preferredDate = '') {
  const dateKey = preferredDate || (rows || []).map(historyRowDateKey).find(Boolean) || '';
  if (!dateKey) return false;
  const nextOffset = historyActivityYearOffsetForDate(dateKey);
  if (nextOffset === historyActivityYearOffset) return false;
  historyActivityYearOffset = nextOffset;
  return true;
}

function displayedHistoryAnchorDate() {
  const row = grid.querySelector('[data-watch-date]');
  return row instanceof HTMLElement ? row.dataset.watchDate || '' : '';
}

function shiftedHistoryDateKey(dateKey, yearDelta) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateKey)) return '';
  const year = Number(dateKey.slice(0, 4)) - yearDelta;
  const monthIndex = Number(dateKey.slice(5, 7)) - 1;
  const day = Number(dateKey.slice(8, 10));
  const lastDay = new Date(year, monthIndex + 1, 0).getDate();
  return localDateKey(new Date(year, monthIndex, Math.min(day, lastDay)));
}

function historyActivityDayNear(payload, dateKey) {
  const days = (payload.activity || []).filter(day => /^\d{4}-\d{2}-\d{2}$/.test(day.watch_date || ''));
  return days.find(day => day.watch_date === dateKey)
    || days.find(day => day.watch_date < dateKey)
    || days.at(-1)
    || null;
}

async function fetchHistoryActivity(channelId = '', yearOffset = historyActivityYearOffset) {
  const range = historyActivityRange(yearOffset);
  const key = `${channelId}:${range.startKey}:${range.endKey}`;
  return cachedRequest(historyActivityCache, key, async () => {
    const params = new URLSearchParams({ start: range.startKey, end: range.endKey });
    if (channelId) params.set('channel_id', channelId);
    const response = await fetch(`/api/history/activity?${params}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`History activity fetch failed: ${response.status}`);
    return response.json();
  }, historyActivityCacheLimit);
}

function historyPageNumberForOffset(offset) {
  const size = pageSizeNumber();
  const effectiveSize = Number.isFinite(size) ? size : 1000;
  return Math.floor(Math.max(0, offset) / effectiveSize) + 1;
}

function historyYearPagePrefetches(channelId, rows) {
  const anchorDate = (rows || []).map(historyRowDateKey).find(Boolean)
    || localDateKey(historyActivityRange().displayEnd);
  const shifts = historyActivityYearOffset > 0 ? [1, -1] : [1];
  return shifts.map(delta => {
    const yearOffset = historyActivityYearOffset + delta;
    return async () => {
      const activity = await fetchHistoryActivity(channelId, yearOffset);
      if (!historyActivitySyncEnabled) return;
      const targetDate = shiftedHistoryDateKey(anchorDate, delta)
        || localDateKey(historyActivityRange(yearOffset).displayEnd);
      const targetDay = historyActivityDayNear(activity, targetDate);
      if (!targetDay) return;
      const page = historyPageNumberForOffset(Number(targetDay.offset || 0));
      await fetchHistoryPage(channelId, page);
    };
  });
}

function heatmapLevel(count, sortedCounts) {
  if (!count || !sortedCounts.length) return 0;
  const threshold = fraction => sortedCounts[Math.floor((sortedCounts.length - 1) * fraction)];
  if (count <= threshold(.25)) return 1;
  if (count <= threshold(.5)) return 2;
  if (count <= threshold(.75)) return 3;
  return 4;
}

function restoreHistoryNavigationButtons(container) {
  for (const button of container.querySelectorAll('.history-heatmap-nav button')) {
    button.disabled = (
      (button.dataset.historyYearShift === '-1' && historyActivityYearOffset === 0)
      || (
        button.dataset.historyCurrent !== undefined
        && historyActivityYearOffset === 0
        && currentPage === 1
      )
    );
  }
}

function historyHeatmapFor(payload) {
  const range = historyActivityRange();
  const activity = new Map((payload.activity || []).map(day => [day.watch_date, day]));
  const sortedCounts = [...activity.values()]
    .map(day => Number(day.watch_count || 0))
    .filter(Boolean)
    .sort((left, right) => left - right);
  const heatmap = document.createElement('section');
  heatmap.className = 'history-heatmap';
  heatmap.dataset.historyChannelId = payload.channel_id || '';
  heatmap.setAttribute('aria-label', 'Views by day');
  const header = document.createElement('div');
  header.className = 'history-heatmap-header';
  const heading = document.createElement('div');
  heading.className = 'history-heatmap-title';
  heading.textContent = 'Views';
  const nav = document.createElement('div');
  nav.className = 'history-heatmap-nav';
  const syncLabel = document.createElement('label');
  syncLabel.className = 'history-heatmap-sync';
  const syncToggle = document.createElement('input');
  syncToggle.type = 'checkbox';
  syncToggle.dataset.historySync = '';
  syncToggle.checked = historyActivitySyncEnabled;
  syncLabel.append(syncToggle, document.createTextNode('Sync'));
  const previous = document.createElement('button');
  previous.type = 'button';
  previous.dataset.historyYearShift = '1';
  previous.title = 'Previous year';
  previous.setAttribute('aria-label', 'Previous year');
  previous.textContent = '<';
  const rangeLabel = document.createElement('span');
  rangeLabel.className = 'history-heatmap-range';
  const rangeDateLabel = date => date.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
  rangeLabel.textContent = `${rangeDateLabel(range.start)} - ${rangeDateLabel(range.displayEnd)}`;
  const next = document.createElement('button');
  next.type = 'button';
  next.dataset.historyYearShift = '-1';
  next.title = 'Next year';
  next.setAttribute('aria-label', 'Next year');
  next.textContent = '>';
  next.disabled = historyActivityYearOffset === 0;
  const current = document.createElement('button');
  current.type = 'button';
  current.dataset.historyCurrent = '';
  current.title = 'Current year/day';
  current.setAttribute('aria-label', 'Current year/day');
  current.textContent = '>|';
  current.disabled = historyActivityYearOffset === 0 && currentPage === 1;
  nav.append(syncLabel, previous, rangeLabel, next, current);
  header.append(heading, nav);
  const scroll = document.createElement('div');
  scroll.className = 'history-heatmap-scroll';
  const months = document.createElement('div');
  months.className = 'history-heatmap-months';
  const weeks = document.createElement('div');
  weeks.className = 'history-heatmap-weeks';
  const seenMonths = new Set();
  for (let weekIndex = 0; weekIndex < 53; weekIndex += 1) {
    const weekStart = new Date(range.start);
    weekStart.setDate(weekStart.getDate() + (weekIndex * 7));
    const monthDate = new Date(weekStart);
    for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
      const candidate = new Date(weekStart);
      candidate.setDate(candidate.getDate() + dayIndex);
      if (candidate.getDate() === 1) {
        monthDate.setTime(candidate.getTime());
        break;
      }
    }
    const monthKey = `${monthDate.getFullYear()}-${monthDate.getMonth()}`;
    if (!seenMonths.has(monthKey)) {
      seenMonths.add(monthKey);
      const label = document.createElement('span');
      label.className = 'history-heatmap-month';
      label.style.gridColumn = String(weekIndex + 1);
      label.textContent = monthDate.toLocaleDateString(undefined, { month: 'short' });
      months.append(label);
    }
    const week = document.createElement('div');
    week.className = 'history-heatmap-week';
    for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
      const date = new Date(weekStart);
      date.setDate(date.getDate() + dayIndex);
      const dateKey = localDateKey(date);
      const day = activity.get(dateKey);
      const count = Number(day?.watch_count || 0);
      const label = `${date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })}: ${count} ${count === 1 ? 'view' : 'views'}`;
      const cell = document.createElement(count && date <= range.end ? 'button' : 'span');
      cell.className = `history-heatmap-day level-${heatmapLevel(count, sortedCounts)}`;
      cell.title = label;
      cell.setAttribute('aria-label', label);
      if (cell instanceof HTMLButtonElement) {
        cell.type = 'button';
        cell.dataset.historyDate = dateKey;
        cell.dataset.historyOffset = String(Number(day.offset || 0));
      }
      week.append(cell);
    }
    weeks.append(week);
  }
  scroll.append(months, weeks);
  heatmap.append(header, scroll);
  return heatmap;
}

async function shiftHistoryActivityYear(delta) {
  const nextOffset = Math.max(0, historyActivityYearOffset + delta);
  if (nextOffset === historyActivityYearOffset) return;
  const heatmap = viewContext.querySelector('.history-heatmap');
  if (!(heatmap instanceof HTMLElement)) {
    historyActivityYearOffset = nextOffset;
    await render();
    return;
  }
  const previousOffset = historyActivityYearOffset;
  const previousPage = currentPage;
  const previousPendingDate = pendingHistoryDate;
  const channelId = heatmap.dataset.historyChannelId || '';
  const currentAnchorDate = displayedHistoryAnchorDate();
  historyActivityYearOffset = nextOffset;
  heatmap.setAttribute('aria-busy', 'true');
  for (const button of heatmap.querySelectorAll('.history-heatmap-nav button')) {
    button.disabled = true;
  }
  try {
    const activity = await fetchHistoryActivity(channelId);
    const channelHeatmapActive = Boolean(
      channelId
      && selected.startsWith('__channel__:')
      && channelDetailTab === 'history'
    );
    if ((selected === '__history__' || channelHeatmapActive) && heatmap.isConnected) {
      if (!historyActivitySyncEnabled) {
        heatmap.replaceWith(historyHeatmapFor(activity));
        return;
      }
      const targetDate = shiftedHistoryDateKey(currentAnchorDate, delta)
        || localDateKey(historyActivityRange().displayEnd);
      const targetDay = historyActivityDayNear(activity, targetDate);
      if (targetDay) {
        setHistoryPageFromOffset(targetDay.watch_date, Number(targetDay.offset || 0));
        if (updateCurrentHash(false)) return;
        await render();
      } else {
        heatmap.replaceWith(historyHeatmapFor(activity));
      }
    }
  } catch (error) {
    historyActivityYearOffset = previousOffset;
    currentPage = previousPage;
    pendingHistoryDate = previousPendingDate;
    heatmap.removeAttribute('aria-busy');
    restoreHistoryNavigationButtons(heatmap);
    throw error;
  }
}

async function jumpToCurrentHistoryActivity() {
  const heatmap = viewContext.querySelector('.history-heatmap');
  if (!(heatmap instanceof HTMLElement)) return;
  const previousOffset = historyActivityYearOffset;
  const previousPage = currentPage;
  const previousPendingDate = pendingHistoryDate;
  const channelId = heatmap.dataset.historyChannelId || '';
  historyActivityYearOffset = 0;
  heatmap.setAttribute('aria-busy', 'true');
  for (const button of heatmap.querySelectorAll('.history-heatmap-nav button')) {
    button.disabled = true;
  }
  try {
    const activity = await fetchHistoryActivity(channelId, 0);
    if (!heatmap.isConnected) return;
    if (!historyActivitySyncEnabled) {
      heatmap.replaceWith(historyHeatmapFor(activity));
      return;
    }
    const targetDay = historyActivityDayNear(activity, localDateKey(new Date()));
    if (targetDay) {
      setHistoryPageFromOffset(targetDay.watch_date, Number(targetDay.offset || 0));
      if (updateCurrentHash(false)) return;
      await render();
    } else {
      heatmap.replaceWith(historyHeatmapFor(activity));
    }
  } catch (error) {
    historyActivityYearOffset = previousOffset;
    currentPage = previousPage;
    pendingHistoryDate = previousPendingDate;
    heatmap.removeAttribute('aria-busy');
    restoreHistoryNavigationButtons(heatmap);
    throw error;
  }
}

async function setHistoryActivitySync(enabled) {
  historyActivitySyncEnabled = enabled;
  if (!enabled) return;
  const heatmap = viewContext.querySelector('.history-heatmap');
  if (!(heatmap instanceof HTMLElement)) return;
  const previousPage = currentPage;
  const previousPendingDate = pendingHistoryDate;
  const channelId = heatmap.dataset.historyChannelId || '';
  heatmap.setAttribute('aria-busy', 'true');
  for (const button of heatmap.querySelectorAll('.history-heatmap-nav button')) {
    button.disabled = true;
  }
  try {
    const activity = await fetchHistoryActivity(channelId);
    const targetDate = localDateKey(historyActivityRange().displayEnd);
    const targetDay = historyActivityDayNear(activity, targetDate);
    if (!targetDay || !historyActivitySyncEnabled || !heatmap.isConnected) return;
    setHistoryPageFromOffset(targetDay.watch_date, Number(targetDay.offset || 0));
    if (updateCurrentHash(false)) return;
    await render();
  } catch (error) {
    historyActivitySyncEnabled = false;
    currentPage = previousPage;
    pendingHistoryDate = previousPendingDate;
    const toggle = heatmap.querySelector('[data-history-sync]');
    if (toggle instanceof HTMLInputElement) toggle.checked = false;
    throw error;
  } finally {
    if (heatmap.isConnected) {
      heatmap.removeAttribute('aria-busy');
      restoreHistoryNavigationButtons(heatmap);
    }
  }
}

function scrollToPendingHistoryDate() {
  if (!pendingHistoryDate) return;
  const date = pendingHistoryDate;
  pendingHistoryDate = '';
  requestAnimationFrame(() => {
    const divider = grid.querySelector(`[data-history-date="${CSS.escape(date)}"]`);
    const row = grid.querySelector(`[data-watch-date="${CSS.escape(date)}"]`);
    const target = divider instanceof HTMLElement ? divider : row;
    if (!(target instanceof HTMLElement)) return;
    if (row instanceof HTMLElement) {
      row.classList.add('history-jump-target');
      window.setTimeout(() => row.classList.remove('history-jump-target'), 1800);
    }
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

function setHistoryPageFromOffset(date, offset) {
  currentPage = historyPageNumberForOffset(offset);
  pendingHistoryDate = date;
  historyNavigationDate = date;
}

async function jumpToHistoryDate(date, offset) {
  setHistoryPageFromOffset(date, offset);
  if (updateCurrentHash(false)) return;
  await render();
}

async function fetchHistoryLocation(channelId = '') {
  const activityRequest = fetchHistoryActivity(channelId);
  if (!historyNavigationDate) {
    return Promise.all([fetchHistoryPage(channelId), activityRequest]);
  }
  const activity = await activityRequest;
  const targetDay = (activity.activity || []).find(
    day => day.watch_date === historyNavigationDate
  );
  if (targetDay) {
    setHistoryPageFromOffset(targetDay.watch_date, Number(targetDay.offset || 0));
  }
  return [await fetchHistoryPage(channelId), activity];
}

async function renderHistoryView() {
  title.textContent = 'History';
  meta.textContent = 'Loading history...';
  applyHistoryCardLayout();
  empty.hidden = true;
  const [payload, initialActivity] = await fetchHistoryLocation();
  const rows = payload.watch || [];
  const activity = historyActivitySyncEnabled
    && syncHistoryActivityYearWithRows(rows, pendingHistoryDate)
    ? await fetchHistoryActivity()
    : initialActivity;
  const total = Number(payload.totals?.filtered_watch_rows ?? payload.totals?.watch_rows ?? rows.length);
  const pageInfo = remotePageInfo(total, rows.length);
  const historyTitleLocation = historyNavigationDate
    ? historyDayLabel({ watch_date: historyNavigationDate })
    : `page ${pageInfo.page}`;
  setDocumentTitle(`History ${historyTitleLocation}`);
  meta.innerHTML = rightPanelListMetaHtml(`${total} watches`, {
    showLayout: true,
    layout: historyCardLayout,
    layoutContext: 'history',
  });
  viewContext.hidden = false;
  viewContext.replaceChildren(historyHeatmapFor(activity));
  renderPager(pageInfo);
  grid.replaceChildren(...historyRowsWithDayDividers(rows, { layout: historyCardLayout }));
  empty.hidden = rows.length !== 0;
  empty.textContent = 'No history rows match.';
  scrollToPendingHistoryDate();
  scheduleAdjacentPagePrefetch(
    pageInfo,
    page => fetchHistoryPage('', page),
    historyYearPagePrefetches('', rows),
  );
}

function scrollResultsToTop() {
  if (resultsScroll) resultsScroll.scrollTop = 0;
}

function videoSortHtml(value, scope) {
  const options = [
    ['newest_added', 'Recently added'],
    ['title', 'Title A-Z'],
    ['oldest_added', 'Oldest added'],
    ['most_watched', 'Most watched'],
    ['playlist_order', 'Playlist order'],
  ];
  return `
    <label class="view-sort">Sort
      <select data-video-sort="${escapeHtml(scope)}">
        ${options.map(([optionValue, label]) => `<option value="${optionValue}" ${value === optionValue ? 'selected' : ''}>${label}</option>`).join('')}
      </select>
    </label>
  `;
}

const videoMetaFilterDefinitions = [
  { key: 'public', label: 'public', visibilityIcon: true },
  { key: 'unlisted', label: 'unlisted', visibilityIcon: true },
  { key: 'private', label: 'private', visibilityIcon: true },
  {
    key: 'members_only',
    label: 'members only',
    className: 'members-only-filter',
    decoratorHtml: membersOnlyIconHtml(),
  },
  { key: 'unavailable', label: 'unavailable', className: 'badge' },
  { key: 'unknown', label: 'unknown' },
  { key: 'removed', label: 'removed', className: 'badge' },
];
function visibleVideoMetaFilterDefinitions(counts, { includeRemoved = true } = {}) {
  return videoMetaFilterDefinitions.filter(({ key }) => (
    (includeRemoved || key !== 'removed')
    && (key !== 'private' || Number(counts?.private || 0) > 0)
  ));
}
const playlistVideoRemovedFilterDefinitions = videoMetaFilterDefinitions.filter(
  ({ key }) => key === 'removed'
);
const playlistDuplicateFilterDefinitions = [
  { key: 'duplicates', label: 'show duplicates', className: 'badge' },
];
const reactionMetaFilterDefinitions = [
  { key: 'none', label: 'no reaction' },
  { key: 'liked', label: 'liked', decoratorHtml: thumbIconHtml('like', false) },
  { key: 'disliked', label: 'disliked', decoratorHtml: thumbIconHtml('dislike', false) },
];
function completionMetaFilterDefinitions(minimumPercent, minimumAttribute) {
  const boundedMinimum = boundedPartialMinimumPercent(minimumPercent);
  const definitions = [
    { key: 'complete', label: 'complete' },
    {
      key: 'partial',
      label: 'partial',
      minimumPercent: boundedMinimum,
      minimumAttribute,
    },
    { key: 'unknown', label: 'unknown' },
    { key: 'never_watched', label: 'never watched' },
  ];
  if (boundedMinimum > 1) {
    definitions.splice(2, 0, {
      key: 'partial_below_minimum',
      label: `partial \u2264 ${boundedMinimum - 1}%`,
    });
  }
  return definitions;
}
const playlistMembershipMetaFilterDefinitions = [
  { key: 'member', label: 'member' },
  { key: 'non_member', label: 'non-member' },
];
function uploaderCategoryMetaFilterDefinitions(counts) {
  const categories = Object.keys(counts || {})
    .filter(key => key !== 'total' && key !== noUploaderCategoryFilter)
    .sort((left, right) => left.localeCompare(right));
  if (!categories.length) return [];
  return [
    { key: noUploaderCategoryFilter, label: 'No category' },
    ...categories.map(category => ({ key: category, label: category })),
  ];
}

function syncUploaderCategoryVisibility(counts) {
  const definitions = uploaderCategoryMetaFilterDefinitions(counts);
  const enableDetectedCategories = (
    !uploaderCategorySelectionExplicit
    && Object.values(searchMetaVisibility.uploaderCategory).some(Boolean)
  );
  for (const { key } of definitions) {
    if (!Object.prototype.hasOwnProperty.call(defaultSearchMetaVisibility.uploaderCategory, key)) {
      defaultSearchMetaVisibility.uploaderCategory[key] = true;
    }
    if (!Object.prototype.hasOwnProperty.call(searchMetaVisibility.uploaderCategory, key)) {
      searchMetaVisibility.uploaderCategory[key] = enableDetectedCategories;
    }
  }
  return definitions;
}
const channelSubscriptionMetaFilterDefinitions = [
  { key: 'subscribed', label: 'subscribed' },
  { key: 'non_subscribed', label: 'non-subscribed' },
];
const terminatedChannelMetaFilterDefinition = {
  key: 'terminated',
  label: 'terminated',
  className: 'badge',
};
const channelStatusMetaFilterDefinitions = [
  { key: 'active', label: 'active' },
  terminatedChannelMetaFilterDefinition,
];
const playlistVisibilityMetaFilterDefinitions = [
  { key: 'private', label: 'private', visibilityIcon: true },
  { key: 'public', label: 'public', visibilityIcon: true },
  { key: 'unlisted', label: 'unlisted', visibilityIcon: true },
  { key: 'unknown', label: 'unknown' },
];
const playlistOwnershipMetaFilterDefinitions = [
  { key: 'mine', label: 'mine' },
  { key: 'others', label: 'others' },
  { key: 'ownership_unknown', label: 'unknown' },
];
const playlistStatusMetaFilterDefinitions = [
  { key: 'active', label: 'active' },
  { key: 'removed', label: 'removed', className: 'status' },
];

function metaFilterChildrenHtml({
  groupName,
  filterAttribute,
  visibility,
  counts,
  definitions,
  filterValuePrefix = '',
}) {
  const applicableDefinitions = definitions.filter(({ key }) =>
    Object.prototype.hasOwnProperty.call(visibility, key)
  );
  return applicableDefinitions.map(({ key, label, className = '', visibilityIcon = false, decoratorHtml = '', minimumPercent = null, minimumAttribute = '' }) => minimumPercent === null ? `
        <label class="meta-filter meta-filter-child">
          <input type="checkbox" data-meta-child-filter="${escapeHtml(groupName)}" data-${escapeHtml(filterAttribute)}="${escapeHtml(`${filterValuePrefix}${key}`)}" ${visibility[key] ? 'checked' : ''}>
          ${visibilityIcon
            ? visibilityFilterLabelHtml(key, metaFilterCount(counts, key))
            : `<span${className || decoratorHtml ? ` class="${[className, decoratorHtml ? 'meta-filter-decorated' : ''].filter(Boolean).join(' ')}"` : ''}>${decoratorHtml}<span>${escapeHtml(label)} <span class="meta-filter-count">${filterCountText(metaFilterCount(counts, key))}</span></span></span>`}
        </label>
      ` : `
        <div class="meta-filter meta-filter-child">
          <label class="completion-partial-toggle">
            <input type="checkbox" data-meta-child-filter="${escapeHtml(groupName)}" data-${escapeHtml(filterAttribute)}="${escapeHtml(`${filterValuePrefix}${key}`)}" ${visibility[key] ? 'checked' : ''}>
            <span>${escapeHtml(label)}</span>
          </label>
          <span class="completion-minimum-control">
            <span>&ge;</span>
            <input class="completion-minimum-input" type="number" min="1" max="99" step="1" value="${minimumPercent}" data-${escapeHtml(minimumAttribute)} aria-label="Minimum partial completion percentage">
            <span>% <span class="meta-filter-count">${filterCountText(metaFilterCount(counts, key))}</span></span>
          </span>
        </div>
  `).join('');
}

function parentFilterCheckboxHtml(dataAttribute, value) {
  return `
    <span class="filter-parent-checkbox">
      <input type="checkbox" ${dataAttribute}="${escapeHtml(value)}">
      <span class="filter-parent-checkbox-indicator" aria-hidden="true">
        <svg viewBox="0 0 13 13"><path d="M2.75 6.5h7.5M6.5 2.75v7.5"></path></svg>
      </span>
    </span>
  `;
}

function metaFilterControlsHtml({
  groupName,
  filterAttribute,
  visibility,
  counts,
  definitions,
  filterValuePrefix = '',
  allLabel = 'All',
  showAll = true,
}) {
  return `
    ${showAll ? `<label class="meta-filter meta-filter-parent">${parentFilterCheckboxHtml('data-meta-all-filter', groupName)} <span>${escapeHtml(allLabel)}</span></label>` : ''}
    ${metaFilterChildrenHtml({
      groupName,
      filterAttribute,
      visibility,
      counts,
      definitions,
      filterValuePrefix,
    })}
  `;
}

function metaFilterCount(counts, key) {
  if (!counts || !Object.prototype.hasOwnProperty.call(counts, key)) return null;
  return Number(counts[key] || 0);
}

function filterCountText(count) {
  return count === null || count === undefined ? '' : String(Number(count || 0));
}

function videoStatusFiltersHtml({
  groupName,
  filterAttribute,
  visibility,
  counts,
  definitions = videoMetaFilterDefinitions,
}) {
  return metaFilterControlsHtml({
    groupName,
    filterAttribute,
    visibility,
    counts,
    definitions,
  });
}

function playlistVideoFiltersHtml(
  counts,
  completionCounts,
  duplicateCount,
  searchHtml = '',
) {
  return `
    <span class="video-filter-groups${searchHtml ? ' has-search' : ''}">
      ${searchHtml ? `<span class="video-filter-search">${searchHtml}</span>` : ''}
      <span class="video-filter-stack">
        <span class="video-filter-facet video-filter-availability">
          ${videoStatusFiltersHtml({
            groupName: 'playlist-videos',
            filterAttribute: 'playlist-filter',
            visibility: playlistVisibility,
            counts,
            definitions: visibleVideoMetaFilterDefinitions(counts, { includeRemoved: false }),
          })}
          <span class="video-removed-filter">
            <span class="video-filter-separator" aria-hidden="true">|</span>
            ${metaFilterControlsHtml({
              groupName: 'playlist-removed',
              filterAttribute: 'playlist-filter',
              visibility: playlistVisibility,
              counts,
              definitions: playlistVideoRemovedFilterDefinitions,
              showAll: false,
            })}
            ${Number(duplicateCount || 0) > 0 ? `
              <span class="video-duplicates-filter">
                <span class="video-filter-separator" aria-hidden="true">|</span>
                ${metaFilterControlsHtml({
                  groupName: 'playlist-duplicates',
                  filterAttribute: 'playlist-duplicates-filter',
                  visibility: { duplicates: playlistDuplicatesOnly },
                  counts: { duplicates: Number(duplicateCount || 0) },
                  definitions: playlistDuplicateFilterDefinitions,
                  showAll: false,
                })}
              </span>
            ` : ''}
          </span>
        </span>
        <span class="video-filter-facet video-filter-completion">
          ${metaFilterControlsHtml({
            groupName: 'playlist-completion',
            filterAttribute: 'playlist-completion-filter',
            visibility: playlistCompletionVisibility,
            counts: completionCounts,
            definitions: completionMetaFilterDefinitions(
              partialCompletionMinimumPercent,
              'playlist-completion-minimum',
            ),
            allLabel: 'Completion',
          })}
        </span>
      </span>
    </span>
  `;
}

function searchFilterTreeChildrenId(nodeId) {
  return `search-filter-tree-${nodeId.replace(/[^A-Za-z0-9_-]/g, '-')}`;
}

function searchFilterTreeToggleHtml(nodeId, label) {
  const expanded = searchFilterTreeExpanded.has(nodeId);
  return `
    <button
      class="search-tree-toggle"
      type="button"
      data-search-tree-toggle="${escapeHtml(nodeId)}"
      aria-controls="${escapeHtml(searchFilterTreeChildrenId(nodeId))}"
      aria-expanded="${expanded ? 'true' : 'false'}"
      aria-label="${expanded ? 'Collapse' : 'Expand'} ${escapeHtml(label)}"
      title="${expanded ? 'Collapse' : 'Expand'} ${escapeHtml(label)}"
    >
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"></path></svg>
    </button>
  `;
}

function searchMetaFiltersHtml(
  metaCounts,
  reactionCounts,
  completionCounts,
  playlistMembershipCounts,
  uploaderCategoryCounts,
  resultCounts,
) {
  const facetHtml = ({
    key,
    visibility,
    definitions,
    counts,
    allLabel = 'All',
    kind,
    groupName = `search-${key}`,
    filterAttribute = 'search-meta-filter',
    filterValuePrefix = `${key}:`,
  }) => {
    const nodeId = `facet:${key}`;
    const expanded = searchFilterTreeExpanded.has(nodeId);
    return `
      <div class="search-meta-facet" data-search-kind-facet="${escapeHtml(kind)}" data-search-tree-node="${escapeHtml(nodeId)}">
        ${searchFilterTreeToggleHtml(nodeId, allLabel)}
        <label class="meta-filter meta-filter-parent">
          ${parentFilterCheckboxHtml('data-meta-all-filter', groupName)}
          <span>${escapeHtml(allLabel)}</span>
        </label>
        <div
          id="${escapeHtml(searchFilterTreeChildrenId(nodeId))}"
          class="search-meta-facet-children"
          data-search-tree-children
          ${expanded ? '' : 'hidden'}
        >
          <span class="search-tree-toggle-spacer" aria-hidden="true"></span>
          <div class="search-meta-controls">
            ${metaFilterChildrenHtml({
              groupName,
              filterAttribute,
              filterValuePrefix,
              visibility,
              counts: searchKindEnabled(kind) ? counts : null,
              definitions,
            })}
          </div>
        </div>
      </div>
    `;
  };
  const pluginVideoFacetHtml = plugin => {
    const definition = browserVideoFacetDefinition(plugin);
    return facetHtml({
      key: `plugin-${plugin.id}`,
      groupName: `search-plugin-${plugin.id}`,
      filterAttribute: 'search-plugin-facet-filter',
      filterValuePrefix: `${plugin.id}:`,
      visibility: browserVideoFacetState(plugin),
      counts: metaCounts?.videoPlugins?.[plugin.id] || null,
      definitions: [
        { key: 'absent', label: definition.absentLabel || `no ${plugin.search.label || plugin.id}` },
        { key: 'present', label: definition.presentLabel || plugin.search.label || plugin.id },
      ],
      allLabel: plugin.search.label || plugin.id,
      kind: 'videos',
    });
  };
  const kindHtml = (titleText, kind, count, facetsHtml) => {
    const kindEnabled = searchKindEnabled(kind);
    const hasChildren = Boolean(facetsHtml.trim());
    const nodeId = `kind:${kind}`;
    const expanded = hasChildren && searchFilterTreeExpanded.has(nodeId);
    const escapedTitle = escapeHtml(titleText);
    return `
    <div class="search-meta-kind${kindEnabled ? '' : ' kind-disabled'}" data-search-tree-node="${escapeHtml(nodeId)}">
      ${hasChildren
        ? searchFilterTreeToggleHtml(nodeId, `${titleText} filters`)
        : '<span class="search-tree-toggle-spacer" aria-hidden="true"></span>'}
      <label class="search-meta-row-title">
        ${parentFilterCheckboxHtml('data-search-kind-filter', kind)}
        <span>${escapedTitle}</span>
        <span class="count">${kindEnabled ? filterCountText(count) : ''}</span>
        <span class="search-meta-progress" data-search-meta-progress="${kind}" aria-hidden="true"></span>
      </label>
      ${hasChildren ? `
        <div
          id="${escapeHtml(searchFilterTreeChildrenId(nodeId))}"
          class="search-meta-kind-children"
          data-search-tree-children
          ${expanded ? '' : 'hidden'}
        >${facetsHtml}</div>
      ` : ''}
    </div>
  `;
  };
  const uploaderCategoryDefinitions = uploaderCategoryMetaFilterDefinitions(
    uploaderCategoryCounts,
  );
  return [
    kindHtml('Videos', 'videos', metaCounts?.videos?.total, [
      facetHtml({ key: 'videos', visibility: searchMetaVisibility.videos, definitions: visibleVideoMetaFilterDefinitions(metaCounts?.videos), counts: metaCounts?.videos, allLabel: 'Availability', kind: 'videos' }),
      facetHtml({ key: 'reactions', visibility: searchMetaVisibility.reactions, definitions: reactionMetaFilterDefinitions, counts: reactionCounts, allLabel: 'Reactions', kind: 'videos' }),
      facetHtml({ key: 'completion', visibility: searchMetaVisibility.completion, definitions: completionMetaFilterDefinitions(partialCompletionMinimumPercent, 'search-completion-minimum'), counts: completionCounts, allLabel: 'Completion', kind: 'videos' }),
      facetHtml({ key: 'membership', visibility: searchMetaVisibility.membership, definitions: playlistMembershipMetaFilterDefinitions, counts: playlistMembershipCounts, allLabel: 'Playlist membership', kind: 'videos' }),
      ...(uploaderCategoryDefinitions.length ? [
        facetHtml({ key: 'uploaderCategory', visibility: searchMetaVisibility.uploaderCategory, definitions: uploaderCategoryDefinitions, counts: uploaderCategoryCounts, allLabel: 'Uploader category', kind: 'videos' }),
      ] : []),
      ...browserVideoFilterPlugins().map(pluginVideoFacetHtml),
    ].join('')),
    kindHtml('Playlists', 'playlists', metaCounts?.playlists?.total, [
      facetHtml({ key: 'playlistVisibility', visibility: searchMetaVisibility.playlistVisibility, definitions: playlistVisibilityMetaFilterDefinitions, counts: metaCounts?.playlists, allLabel: 'Visibility', kind: 'playlists' }),
      facetHtml({ key: 'playlistOwnership', visibility: searchMetaVisibility.playlistOwnership, definitions: playlistOwnershipMetaFilterDefinitions, counts: metaCounts?.playlists, allLabel: 'Ownership', kind: 'playlists' }),
      facetHtml({ key: 'playlistStatus', visibility: searchMetaVisibility.playlistStatus, definitions: playlistStatusMetaFilterDefinitions, counts: metaCounts?.playlists, allLabel: 'Status', kind: 'playlists' }),
    ].join('')),
    kindHtml('Channels', 'channels', metaCounts?.channels?.total, [
      facetHtml({ key: 'channelSubscription', visibility: searchMetaVisibility.channelSubscription, definitions: channelSubscriptionMetaFilterDefinitions, counts: metaCounts?.channels, allLabel: 'Subscription', kind: 'channels' }),
      facetHtml({ key: 'channelStatus', visibility: searchMetaVisibility.channelStatus, definitions: channelStatusMetaFilterDefinitions, counts: metaCounts?.channels, allLabel: 'Status', kind: 'channels' }),
    ].join('')),
    ...browserResultSearchPlugins().map(plugin => kindHtml(
      plugin.search.label || plugin.id,
      plugin.id,
      resultCounts?.plugins?.[plugin.id]
        ?? Number(plugin.search.catalogCount?.(browserPluginStatus(plugin.id)) || 0),
      '',
    )),
  ].filter(Boolean).join('');
}

function renderSearchMetaFilters({
  metaCounts = null,
  reactionCounts = null,
  completionCounts = null,
  playlistMembershipCounts = null,
  uploaderCategoryCounts = null,
  counts = null,
} = {}) {
  syncUploaderCategoryVisibility(uploaderCategoryCounts);
  searchForFilters.innerHTML = searchMetaFiltersHtml(
    metaCounts,
    reactionCounts,
    completionCounts,
    playlistMembershipCounts,
    uploaderCategoryCounts,
    counts,
  );
  for (const key of ['videos', 'reactions', 'completion', 'membership', 'uploaderCategory', 'playlistVisibility', 'playlistOwnership', 'playlistStatus', 'channelSubscription', 'channelStatus']) {
    syncMetaFilterGroup(`search-${key}`);
  }
  for (const plugin of browserVideoFilterPlugins()) {
    syncMetaFilterGroup(`search-plugin-${plugin.id}`);
  }
  for (const kind of [
    'videos',
    'playlists',
    'channels',
    ...browserResultSearchPlugins().map(plugin => plugin.id),
  ]) {
    syncSearchKindFilter(kind);
  }
  updateSearchMetaProgress();
}

function applySearchFilterTreeNodeState(nodeId) {
  const button = [...searchForFilters.querySelectorAll('[data-search-tree-toggle]')]
    .find(candidate => candidate.dataset.searchTreeToggle === nodeId);
  if (!(button instanceof HTMLButtonElement)) return;
  const expanded = searchFilterTreeExpanded.has(nodeId);
  const controlsId = button.getAttribute('aria-controls') || '';
  const children = controlsId ? document.getElementById(controlsId) : null;
  button.setAttribute('aria-expanded', String(expanded));
  const label = button.getAttribute('aria-label')?.replace(/^(?:Collapse|Expand)\s+/, '') || 'filters';
  button.setAttribute('aria-label', `${expanded ? 'Collapse' : 'Expand'} ${label}`);
  button.title = `${expanded ? 'Collapse' : 'Expand'} ${label}`;
  if (children instanceof HTMLElement) children.hidden = !expanded;
}

function toggleSearchFilterTreeNode(nodeId) {
  const wasExpanded = searchFilterTreeExpanded.has(nodeId);
  if (wasExpanded) searchFilterTreeExpanded.delete(nodeId);
  else searchFilterTreeExpanded.add(nodeId);
  applySearchFilterTreeNodeState(nodeId);
  saveSearchFilterTreePreference(nodeId, wasExpanded);
}

function syncSearchFiltersForSelection() {
  const historySelected = selected === '__history__';
  const alreadyInactive = searchFilters.classList.contains('view-inactive');
  searchFilters.classList.toggle('view-inactive', historySelected);
  if (!historySelected || alreadyInactive) return;
  clearSearchMetaVisibility();
  renderSearchMetaFilters();
}

function searchResultsSortHtml() {
  const forceRelevance = browserSearchPlugins().some(plugin => (
    browserPluginSearchActive(plugin) && browserPluginForcesRelevance(plugin)
  ));
  const options = forceRelevance ? [
    ['relevance', 'Relevance'],
  ] : [
    ['relevance', 'Relevance'],
    ['title', 'Title A-Z'],
    ['newest', 'Newest'],
    ['oldest', 'Oldest'],
    ['most_watched', 'Most watched'],
    ['type', 'Type'],
  ];
  return `
    <label class="view-sort">Sort
      <select data-search-sort>
        ${options.map(([optionValue, label]) => `<option value="${optionValue}" ${searchResultsSort === optionValue ? 'selected' : ''}>${label}</option>`).join('')}
      </select>
    </label>
  `;
}

function cardLayoutIconSvg(layout) {
  if (layout === 'detailed') {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2" y="4" width="16" height="12" rx="1"></rect><path d="M8 4v12M10.5 7h5M10.5 10h5M10.5 13h3.5"></path></svg>';
  }
  if (layout === 'compact') {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2" y="3" width="16" height="4" rx="1"></rect><rect x="2" y="8" width="16" height="4" rx="1"></rect><rect x="2" y="13" width="16" height="4" rx="1"></rect></svg>';
  }
  return '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2" y="2" width="7" height="7" rx="1"></rect><rect x="11" y="2" width="7" height="7" rx="1"></rect><rect x="2" y="11" width="7" height="7" rx="1"></rect><rect x="11" y="11" width="7" height="7" rx="1"></rect></svg>';
}

function cardLayoutHtml(activeLayout, context) {
  const options = [
    ['grid', 'Grid'],
    ['detailed', 'Detailed list'],
    ['compact', 'Compact list'],
  ];
  return `
    <div class="card-layout-control" role="group" aria-label="Card layout">
      ${options.map(([value, label]) => `
        <button class="card-layout-button${activeLayout === value ? ' active' : ''}" type="button" data-card-layout="${value}" data-card-layout-context="${context}" title="${label}" aria-label="${label}" aria-pressed="${activeLayout === value}">
          ${cardLayoutIconSvg(value)}
        </button>
      `).join('')}
    </div>
  `;
}

function rightPanelListMetaHtml(
  summary,
  { showLayout = false, layout = searchCardLayout, layoutContext = 'search', sortHtml = '' } = {},
) {
  return `
    <div class="search-result-meta">
      <div class="search-result-summary">
        <span>${escapeHtml(summary)}</span>
        ${(showLayout || sortHtml) ? `
          <span class="result-view-controls">
            ${showLayout ? cardLayoutHtml(layout, layoutContext) : ''}
            ${sortHtml}
          </span>
        ` : ''}
      </div>
    </div>
  `;
}

function applySearchCardLayout() {
  grid.className = `grid search-grid layout-${searchCardLayout}`;
}

function applyPlaylistCardLayout() {
  grid.className = `grid search-grid layout-${playlistCardLayout}`;
}

function applyHistoryCardLayout() {
  grid.className = `grid search-grid history-list layout-${historyCardLayout}`;
  for (const card of grid.querySelectorAll('.history-card')) {
    card.classList.toggle('history-row', historyCardLayout !== 'grid');
  }
}

function playlistSourceLinksHtml(video) {
  const links = Array.isArray(video.playlist_links) && video.playlist_links.length
    ? video.playlist_links
    : [{ playlist_id: video.playlist_id, title: video.playlist_title, removed: wasRemovedByMeFromPlaylist(video) }];
  const rows = links
    .filter(link => link && link.playlist_id && link.title && !link.removed)
    .map(link => {
      return `<div><a class="playlist-link" href="${localPlaylistHref(link.playlist_id)}">${escapeHtml(link.title)}</a></div>`;
    });
  return rows.length ? `<div class="details playlist-sources">${rows.join('')}</div>` : '';
}

function unavailableLabel(video) {
  if (Object.prototype.hasOwnProperty.call(video, 'is_playable') && !video.is_playable) {
    return availabilityLabel(video.availability);
  }
  const status = String(video.recovered_status || '');
  if (status === 'NOT_FOUND' || status.startsWith('DELETED_')) return 'Unavailable';
  const title = String(video.title || '').trim().toLowerCase().replace(/^[\[\(]+|[\]\)]+$/g, '');
  if (title === 'private video' || title === 'deleted video') return video.title || 'Unavailable';
  return '';
}

function availabilityLabel(value) {
  const availability = String(value || '').trim();
  if (availability.toLowerCase() === 'subscriber_only') return 'Members only';
  return availability;
}

function videoAvailabilityValue(video) {
  const availability = String(video.availability || '').trim().toLowerCase();
  if (availability === 'subscriber_only') return 'members_only';
  if (availability === 'private' && Number(video.is_playable) === 1) return 'private';
  if (['public', 'unlisted', 'unknown'].includes(availability)) return availability;
  if (['private', 'deleted', 'removed', 'unavailable', 'needs_auth', 'premium_only'].includes(availability)) {
    return 'unavailable';
  }
  const recoveredStatus = String(video.recovered_status || '').trim().toUpperCase();
  if (recoveredStatus === 'LIVE') return 'public';
  if (recoveredStatus === 'NOT_FOUND' || recoveredStatus.startsWith('DELETED_')) return 'unavailable';
  if (Object.prototype.hasOwnProperty.call(video, 'is_playable')) {
    if (video.is_playable === true || Number(video.is_playable) === 1) return 'public';
    if (video.is_playable === false || video.is_playable === 0) return 'unavailable';
  }
  return 'unknown';
}

function videoAvailabilityHtml(video) {
  const availability = videoAvailabilityValue(video);
  if (availability === 'members_only') {
    return badgeRowsHtml([{ label: 'Members only' }]);
  }
  if (availability === 'public' || availability === 'unlisted' || availability === 'private') {
    const label = availability.charAt(0).toUpperCase() + availability.slice(1);
    return `<div class="video-availability">${visibilityLabelHtml(availability, label)}</div>`;
  }
  if (availability === 'unavailable') {
    return '<div class="video-availability unavailable">Unavailable</div>';
  }
  return '<div class="video-availability">Unknown</div>';
}

function archivarixStatusLabel(video) {
  const status = String(video.recovered_status || '');
  if (status === 'NOT_FOUND') return 'Archivarix: No results found';
  if (status.startsWith('DELETED_')) return `Archivarix: ${status}`;
  return '';
}

function archivarixStatusHtml(video) {
  const label = archivarixStatusLabel(video);
  return label ? `<div class="details"><span class="badge">${escapeHtml(label)}</span></div>` : '';
}

function archivarixVideoUrl(video) {
  return video.video_id ? `https://tube.archivarix.net/?q=${encodeURIComponent(video.video_id)}` : '';
}

function shouldShowArchivarixLink(video) {
  return Boolean(video.video_id && (unavailableLabel(video) || archivarixStatusLabel(video)));
}

function archivarixLinkHtml(video) {
  const url = archivarixVideoUrl(video);
  if (!url || !shouldShowArchivarixLink(video)) return '';
  return `<a class="playlist-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">Archivarix</a>`;
}

function browserPluginRequestUrl(pluginId, path, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    for (const item of (Array.isArray(value) ? value : [value])) {
      if (item !== null && item !== undefined) query.append(key, String(item));
    }
  }
  const suffix = query.toString();
  return `/api/plugins/${encodeURIComponent(pluginId)}/${path}${suffix ? `?${suffix}` : ''}`;
}

async function libraryVideos(videoIds) {
  const ids = [...new Set((videoIds || []).map(String).filter(Boolean))];
  const videos = new Map();
  for (let start = 0; start < ids.length; start += 100) {
    const query = new URLSearchParams();
    for (const videoId of ids.slice(start, start + 100)) query.append('id', videoId);
    const response = await fetch(`/api/videos/batch?${query}`, { cache: 'no-store' });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `Library video request failed (${response.status})`);
    }
    for (const video of payload.videos || []) {
      if (video?.video_id) videos.set(video.video_id, video);
    }
  }
  return videos;
}

function browserPluginHost(pluginId) {
  return {
    pluginId,
    status: browserPluginStatus(pluginId),
    supports: capability => browserPluginSupports(pluginId, capability),
    libraryVideos,
    requestJson: async (path, params = {}) => {
      const response = await fetch(
        browserPluginRequestUrl(pluginId, path, params),
        { cache: 'no-store' },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || `Plugin request failed (${response.status})`);
      }
      return payload;
    },
    ui: {
      createSearchVideoCard: searchVideoCardFor,
      createVideoCard: videoCardFor,
      escapeHtml,
      localVideoHref,
      searchHighlight,
    },
  };
}

async function fetchBrowserPluginSearches(query, limit, offset) {
  const counts = {};
  const errors = [];
  const results = [];
  let total = 0;
  let totalIsExact = true;
  let remaining = limit;
  let localOffset = offset;
  for (const plugin of browserResultSearchPlugins().filter(item => searchKindEnabled(item.id))) {
    let payload = { total: 0, totalIsExact: true, results: [] };
    if (query || plugin.search.fetchEmptyQuery === true) {
      try {
        payload = await plugin.search.fetch(
          { query, limit: Math.max(1, remaining), offset: localOffset },
          browserPluginHost(plugin.id),
        );
      } catch (error) {
        errors.push({
          id: plugin.id,
          label: plugin.search.label || plugin.id,
          message: error instanceof Error ? error.message : String(error),
        });
      }
    }
    const pluginTotal = Math.max(0, Number(payload.total || 0));
    const rows = Array.isArray(payload.results) ? payload.results : [];
    counts[plugin.id] = pluginTotal;
    total += pluginTotal;
    totalIsExact = totalIsExact && payload.totalIsExact !== false;
    if (remaining > 0 && localOffset < pluginTotal) {
      const visibleRows = rows.slice(0, remaining);
      results.push(...visibleRows.map(item => ({
        kind: 'plugin',
        pluginId: plugin.id,
        item,
      })));
      remaining -= visibleRows.length;
    }
    localOffset = Math.max(0, localOffset - pluginTotal);
  }
  return {
    counts,
    coreOffset: localOffset,
    errors,
    remaining,
    results,
    total,
    totalIsExact,
  };
}

async function decorateCoreSearchResults(results, errors, query) {
  for (const plugin of browserSearchPlugins().filter(item => searchKindEnabled(item.id))) {
    if (typeof plugin.search.decorateCoreResults !== 'function') continue;
    try {
      await plugin.search.decorateCoreResults(
        results,
        browserPluginHost(plugin.id),
        { query },
      );
    } catch (error) {
      errors.push({
        id: plugin.id,
        label: plugin.search.label || plugin.id,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }
}

async function fetchOmniSearch(query, page = currentPage) {
  const size = pageSizeNumber();
  const limit = Number.isFinite(size) ? size : 5000;
  const requestedPage = Math.max(1, Number(page) || 1);
  const offset = (requestedPage - 1) * limit;
  const searchFieldsValue = searchFieldParamValue() || '__none__';
  const kindsValue = selectedSearchResultKinds().join(',') || '__none__';
  const sourceScopes = activeSearchSourceScopes();
  const metaCountsKey = JSON.stringify([
    query,
    searchFieldsValue,
    kindsValue,
    searchPlaylistGroupKey,
    searchChannelGroupKey,
    sourceScopes.video,
    sourceScopes.channel,
    partialCompletionMinimumPercent,
    browserSearchPlugins()
      .map(browserPluginStateKey)
      .filter(Boolean),
  ]);
  const metaCountsCache = omniMetaCountsCache;
  const videoPluginFacetCountsCache = omniVideoPluginFacetCountsCache;
  const reactionCountsCache = omniReactionCountsCache;
  const completionCountsCache = omniCompletionCountsCache;
  const playlistMembershipCountsCache = omniPlaylistMembershipCountsCache;
  const uploaderCategoryCountsCache = omniUploaderCategoryCountsCache;
  const coreParams = new URLSearchParams({
    q: query,
    search_fields: searchFieldsValue,
    kinds: kindsValue,
    playlist_group_key: searchPlaylistGroupKey,
    channel_group_key: searchChannelGroupKey,
    video_source: sourceScopes.video,
    channel_source: sourceScopes.channel,
    video_meta: metaFilterParamValue(searchMetaVisibility.videos),
    video_reaction: metaFilterParamValue(searchMetaVisibility.reactions),
    video_completion: metaFilterParamValue(searchMetaVisibility.completion),
    video_completion_min_percent: String(partialCompletionMinimumPercent),
    video_playlist_membership: metaFilterParamValue(searchMetaVisibility.membership),
    channel_subscription: metaFilterParamValue(searchMetaVisibility.channelSubscription),
    channel_status: metaFilterParamValue(searchMetaVisibility.channelStatus),
    playlist_meta: metaFilterParamValue(searchMetaVisibility.playlistVisibility),
    playlist_ownership: metaFilterParamValue(searchMetaVisibility.playlistOwnership),
    playlist_status: metaFilterParamValue(searchMetaVisibility.playlistStatus),
    sort: searchResultsSort,
  });
  if (!allMetaFiltersEnabled(searchMetaVisibility.uploaderCategory)) {
    coreParams.set(
      'video_uploader_category',
      metaFilterParamValue(searchMetaVisibility.uploaderCategory),
    );
  }
  const pluginKey = browserSearchPlugins()
    .map(browserPluginStateKey)
    .filter(Boolean)
    .join(',');
  const videoPluginFacetCountsKey = JSON.stringify([
    query,
    searchFieldsValue,
    kindsValue,
    searchPlaylistGroupKey,
    searchChannelGroupKey,
    sourceScopes.video,
    sourceScopes.channel,
    metaFilterParamValue(searchMetaVisibility.videos),
    metaFilterParamValue(searchMetaVisibility.reactions),
    metaFilterParamValue(searchMetaVisibility.completion),
    String(partialCompletionMinimumPercent),
    metaFilterParamValue(searchMetaVisibility.membership),
    metaFilterParamValue(searchMetaVisibility.uploaderCategory),
    query ? browserVideoFilterPlugins().map(browserVideoFacetSearchActive) : [],
  ]);
  const key = `${coreParams.toString()}&plugins=${encodeURIComponent(pluginKey)}&limit=${limit}&offset=${offset}`;
  return cachedRequest(omniSearchCache, key, async () => {
    const pluginPayload = await fetchBrowserPluginSearches(query, limit, offset);
    const requestParams = new URLSearchParams(coreParams);
    for (const plugin of browserVideoFilterPlugins()) {
      const state = browserVideoFacetState(plugin);
      requestParams.append('video_facet_plugin', plugin.id);
      if (state.present && !state.absent) {
        requestParams.append('video_filter_plugin', plugin.id);
      } else if (state.absent && !state.present) {
        requestParams.append('video_exclude_filter_plugin', plugin.id);
      }
    }
    const videoSearchFieldPlugins = browserVideoSearchFieldPlugins();
    if (videoSearchFieldPlugins.length) {
      for (const plugin of videoSearchFieldPlugins) {
        requestParams.append('video_search_plugin', plugin.id);
      }
    } else {
      requestParams.append('video_search_plugin', '__none__');
    }
    requestParams.set('limit', String(Math.max(1, pluginPayload.remaining)));
    requestParams.set('offset', String(pluginPayload.coreOffset));
    const response = await fetch(`/api/search?${requestParams}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Search failed: ${response.status}`);
    const corePayload = await response.json();
    const coreRows = pluginPayload.remaining
      ? (corePayload.results || []).slice(0, pluginPayload.remaining)
      : [];
    await decorateCoreSearchResults(coreRows, pluginPayload.errors, query);
    const payload = {
      ...corePayload,
      query,
      limit,
      offset,
      total: pluginPayload.total + Number(corePayload.total || 0),
      totalIsExact: pluginPayload.totalIsExact && corePayload.totalIsExact !== false,
      counts: {
        ...(corePayload.counts || {}),
        plugins: pluginPayload.counts,
      },
      pluginErrors: pluginPayload.errors,
      results: [...pluginPayload.results, ...coreRows],
    };
    if (!metaCountsCache.has(metaCountsKey)) {
      const stableMetaCounts = Object.fromEntries(
        Object.entries(payload.metaCounts || {}).map(([group, counts]) => [group, { ...counts }])
      );
      metaCountsCache.set(metaCountsKey, stableMetaCounts);
    }
    if (!videoPluginFacetCountsCache.has(videoPluginFacetCountsKey)) {
      videoPluginFacetCountsCache.set(
        videoPluginFacetCountsKey,
        Object.fromEntries(
          Object.entries(payload.metaCounts?.videoPlugins || {}).map(
            ([pluginId, counts]) => [pluginId, { ...counts }]
          )
        ),
      );
    }
    if (!reactionCountsCache.has(metaCountsKey)) {
      reactionCountsCache.set(metaCountsKey, { ...(payload.reactionCounts || {}) });
    }
    if (!completionCountsCache.has(metaCountsKey)) {
      completionCountsCache.set(metaCountsKey, { ...(payload.completionCounts || {}) });
    }
    if (!playlistMembershipCountsCache.has(metaCountsKey)) {
      playlistMembershipCountsCache.set(
        metaCountsKey,
        { ...(payload.playlistMembershipCounts || {}) },
      );
    }
    if (!uploaderCategoryCountsCache.has(metaCountsKey)) {
      uploaderCategoryCountsCache.set(
        metaCountsKey,
        { ...(payload.uploaderCategoryCounts || {}) },
      );
    }
    const stableMetaCounts = {
      ...metaCountsCache.get(metaCountsKey),
      videoPlugins: videoPluginFacetCountsCache.get(videoPluginFacetCountsKey),
    };
    const stablePayload = {
      ...payload,
      metaCounts: stableMetaCounts,
      reactionCounts: reactionCountsCache.get(metaCountsKey),
      completionCounts: completionCountsCache.get(metaCountsKey),
      playlistMembershipCounts: playlistMembershipCountsCache.get(metaCountsKey),
      uploaderCategoryCounts: uploaderCategoryCountsCache.get(metaCountsKey),
    };
    return stablePayload;
  }, adjacentPageCacheLimit);
}

async function fetchVideoCollection({
  scope = 'playlist',
  playlistId = '',
  channelId = '',
  sort = 'newest_added',
  query = '',
  visibility = playlistVisibility,
  completion = null,
  partialMinimumPercent = 1,
  duplicatesOnly = false,
  page = currentPage,
} = {}) {
  const base = playlistId
    ? `/api/playlists/${encodeURIComponent(playlistId)}/videos`
    : '/api/videos';
  partialMinimumPercent = boundedPartialMinimumPercent(partialMinimumPercent);
  const metaCountsKey = JSON.stringify([
    scope,
    playlistId,
    channelId,
    query,
    partialMinimumPercent,
  ]);
  const metaCountsCache = videoMetaCountsCache;
  const params = {
    scope,
    channel_id: channelId,
    q: query,
    public: visibility.public === false ? '0' : '1',
    unlisted: visibility.unlisted === false ? '0' : '1',
    private: visibility.private === false ? '0' : '1',
    members_only: visibility.members_only === false ? '0' : '1',
    unavailable: visibility.unavailable === false ? '0' : '1',
    unknown: visibility.unknown === false ? '0' : '1',
    removed: visibility.removed === false ? '0' : '1',
    duplicates: duplicatesOnly ? '1' : '0',
    sort,
  };
  if (completion) params.completion = metaFilterParamValue(completion);
  if (completion) params.completion_min_percent = String(partialMinimumPercent);
  const path = remoteListPath(base, params, page);
  const payload = await fetchViewData(path);
  if (!metaCountsCache.has(metaCountsKey)) {
    metaCountsCache.set(metaCountsKey, { ...(payload.counts || {}) });
  }
  if (!videoCompletionCountsCache.has(metaCountsKey)) {
    videoCompletionCountsCache.set(metaCountsKey, { ...(payload.completionCounts || {}) });
  }
  return {
    ...payload,
    counts: metaCountsCache.get(metaCountsKey),
    completionCounts: videoCompletionCountsCache.get(metaCountsKey),
  };
}

function buttonFor(group, preset, membershipMap, childMap) {
  const button = document.createElement('button');
  button.className = 'group group-tree-action';
  button.dataset.preset = preset;
  button.dataset.groupKey = group.group_key;
  button.innerHTML = `<span>${escapeHtml(group.name)}</span><span class="count">${groupCount(group.group_key, membershipMap, childMap)}</span>`;
  button.addEventListener('click', () => activateSearchPreset(preset, group.group_key));
  return button;
}

function navigationGroupTreeNodeId(preset, groupKey) {
  return `${preset}:${groupKey}`;
}

function applyNavigationGroupTreeNodeState(nodeId, toggle, childContainer, label) {
  const expanded = !navigationGroupTreeCollapsed.has(nodeId);
  toggle.setAttribute('aria-expanded', String(expanded));
  toggle.setAttribute('aria-label', `${expanded ? 'Collapse' : 'Expand'} ${label}`);
  toggle.title = `${expanded ? 'Collapse' : 'Expand'} ${label}`;
  childContainer.hidden = !expanded;
}

function toggleNavigationGroupTreeNode(nodeId, toggle, childContainer, label) {
  const wasCollapsed = navigationGroupTreeCollapsed.has(nodeId);
  if (wasCollapsed) navigationGroupTreeCollapsed.delete(nodeId);
  else navigationGroupTreeCollapsed.add(nodeId);
  applyNavigationGroupTreeNodeState(nodeId, toggle, childContainer, label);
  saveNavigationGroupTreePreference(nodeId, wasCollapsed);
}

function appendGroupTree(section, group, preset, membershipMap, childMap, depth = 0) {
  const childGroups = childMap.get(group.group_key) || [];
  const row = document.createElement('div');
  row.className = `group-tree-row ${depth > 0 ? 'child' : ''}`;
  row.style.setProperty('--group-depth', String(depth));
  if (childGroups.length) {
    const nodeId = navigationGroupTreeNodeId(preset, group.group_key);
    const childContainer = document.createElement('div');
    const childrenId = `navigation-group-tree-${++navigationGroupTreeDomId}`;
    childContainer.className = 'group-tree-children';
    childContainer.id = childrenId;
    const toggle = document.createElement('button');
    toggle.className = 'search-tree-toggle group-tree-toggle';
    toggle.type = 'button';
    toggle.dataset.groupTreeToggle = nodeId;
    toggle.setAttribute('aria-controls', childrenId);
    toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"></path></svg>';
    applyNavigationGroupTreeNodeState(nodeId, toggle, childContainer, group.name);
    toggle.addEventListener('click', () => {
      toggleNavigationGroupTreeNode(nodeId, toggle, childContainer, group.name);
    });
    row.appendChild(toggle);
    row.appendChild(buttonFor(group, preset, membershipMap, childMap));
    section.appendChild(row);
    for (const child of childGroups) {
      appendGroupTree(
        childContainer,
        child,
        preset,
        membershipMap,
        childMap,
        depth + 1,
      );
    }
    section.appendChild(childContainer);
  } else {
    const spacer = document.createElement('span');
    spacer.className = 'group-tree-toggle-spacer';
    spacer.setAttribute('aria-hidden', 'true');
    row.appendChild(spacer);
    row.appendChild(buttonFor(group, preset, membershipMap, childMap));
    section.appendChild(row);
  }
}

function sectionFor(label) {
  const section = document.createElement('section');
  section.className = 'group-section';
  const title = document.createElement('div');
  title.className = 'group-section-title';
  title.textContent = label;
  section.appendChild(title);
  groupsEl.appendChild(section);
  return section;
}

function presetButton(preset, label, count) {
  const button = document.createElement('button');
  button.className = 'group';
  button.dataset.preset = preset;
  button.innerHTML = `<span>${escapeHtml(label)}</span><span class="count">${count}</span>`;
  button.addEventListener('click', () => activateSearchPreset(preset));
  return button;
}

function syncSidebarSelection() {
  const groupPresets = new Set(['playlist-group', 'channel-group']);
  for (const button of groupsEl.querySelectorAll('.group')) {
    const activeGroupKey = activeSearchPreset === 'channel-group'
      ? searchChannelGroupKey
      : searchPlaylistGroupKey;
    const activeGroupPreset = (
      selected === '__search__'
      && groupPresets.has(activeSearchPreset)
      && button.dataset.preset === activeSearchPreset
      && button.dataset.groupKey === activeGroupKey
    );
    const activeNamedPreset = (
      selected === '__search__'
      && button.dataset.preset
      && !groupPresets.has(button.dataset.preset)
      && button.dataset.preset === activeSearchPreset
    );
    button.classList.toggle(
      'active',
      button.dataset.key === selected || activeGroupPreset || activeNamedPreset,
    );
  }
  searchNav?.classList.toggle('active', selected === '__search__' && !activeSearchPreset);
  historyNav?.classList.toggle('active', selected === '__history__');
}

function renderGroups() {
  if (!data) return;
  groupsEl.replaceChildren();
  navigationGroupTreeDomId = 0;
  const counts = data.counts || {};
  const historyCount = historyNav?.querySelector('.count');
  if (historyCount) historyCount.textContent = counts.history || 0;
  const videoSection = sectionFor('Videos');
  videoSection.appendChild(presetButton('videos', 'Videos', counts.videos || 0));
  videoSection.appendChild(presetButton('playlist-videos', 'Playlist videos', counts.playlist_videos || 0));
  videoSection.appendChild(presetButton('liked-videos', 'Liked videos', counts.liked_videos || 0));
  for (const { plugin, preset, presetId } of browserSearchPresets('videos')) {
    const status = browserPluginStatus(plugin.id);
    const count = Number(
      preset.count?.(status)
      ?? plugin.search.catalogCount?.(status)
      ?? 0
    );
    videoSection.appendChild(presetButton(presetId, preset.label || plugin.search.label || plugin.id, count));
  }

  const playlistSection = sectionFor('Playlists');
  playlistSection.appendChild(presetButton('all-playlists', 'Playlists', counts.playlists || 0));
  for (const group of playlistChildren.get('') || []) {
    appendGroupTree(
      playlistSection,
      group,
      'playlist-group',
      playlistMemberships,
      playlistChildren,
    );
  }

  const channelSection = sectionFor('Channels');
  channelSection.appendChild(presetButton('channels', 'Channels', counts.channels || 0));
  channelSection.appendChild(presetButton('subscribed', 'Subscribed', counts.subscribed_channels || 0));
  channelSection.appendChild(presetButton('terminated', 'Terminated', counts.terminated_channels || 0));
  for (const group of channelChildren.get('') || []) {
    appendGroupTree(
      channelSection,
      group,
      'channel-group',
      channelMemberships,
      channelChildren,
    );
  }
  syncSidebarSelection();
}

async function renderBrowserPluginVideoPanels(videoId) {
  const panels = [];
  for (const plugin of browserPlugins.values()) {
    const extension = plugin.videoDetail;
    if (!extension || !browserPluginSupports(plugin.id, extension.capability)) continue;
    try {
      const panel = await extension.render(videoId, browserPluginHost(plugin.id));
      if (panel instanceof HTMLElement) panels.push(panel);
    } catch (_error) {
      // Optional plugin failures must not prevent the core video detail from rendering.
    }
  }
  return panels;
}

function videoDetailCardFor(video) {
  const article = document.createElement('article');
  article.className = 'card video-detail';
  const titleText = displayVideoTitle(video);
  const watchUrl = youtubeWatchUrl(video);
  const channelName = displayVideoChannel(video);
  const channelUrl = displayVideoChannelLocalUrl(video);
  const thumbnail = video.metadata_thumbnail_path
    ? thumbnailWithProgress(video.metadata_thumbnail_path, video)
    : document.createElement('div');
  const body = document.createElement('div');
  body.className = 'body';
  body.innerHTML = `
    <div class="detail-layout">
      <div class="detail-thumb"></div>
      <div>
        ${channelName ? `<div class="details video-card-channel">${creatorHtml(video.metadata_channel_thumbnail_path, channelName, channelUrl)}</div>` : ''}
        <div class="title-row">
          <div class="video-title">${escapeHtml(titleText)}</div>
          ${watchUrl ? `<a class="external-link" href="${escapeHtml(watchUrl)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(titleText)} on YouTube">${externalLinkSvg()}</a>` : ''}
        </div>
        ${badgeRowsHtml([
          { label: video.virtual_video ? 'Not in library' : '' },
          { label: wasRemovedByMeFromPlaylist(video) ? 'Removed' : '' },
          { label: matchTypeLabel(video), title: video.match_note },
        ])}
        ${detailRowHtml([
          displayVideoDuration(video) ? `<span>${escapeHtml(displayVideoDuration(video))}</span>` : '',
          displayVideoUploadDate(video) ? `<span>${escapeHtml(displayVideoUploadDate(video))}</span>` : '',
          video.video_id ? `<span>${escapeHtml(video.video_id)}</span>` : '',
          archivarixLinkHtml(video),
        ])}
        ${archivarixStatusHtml(video)}
        ${videoAvailabilityHtml(video)}
        ${latestWatchDateHtml(video)}
        ${watchedLineHtml(video)}
        ${watchSparklineHtml(video, true)}
        ${reactionIconsHtml(video)}
        ${uploaderCategoryHtml(video.uploader_category)}
        ${playlistSourceLinksHtml(video)}
        ${video.metadata_description ? `<div class="description">${escapeHtml(video.metadata_description)}</div>` : '<div class="empty">No description captured for this video.</div>'}
      </div>
    </div>
  `;
  body.querySelector('.detail-thumb')?.append(thumbnail);
  article.append(body);
  return article;
}

function channelDetailCardFor(channel) {
  const article = document.createElement('article');
  article.className = 'card channel-detail';
  const youtubeUrl = channel.url || (channel.channel_id ? `https://www.youtube.com/channel/${encodeURIComponent(channel.channel_id)}` : '');
  const archivarixUrl = channel.channel_id ? `https://tube.archivarix.net/?q=${encodeURIComponent(channel.channel_id)}` : '';
  const status = String(channel.status || '').toLowerCase();
  const subscribedLabel = isSubscribedChannel(channel) ? 'Subscribed' : 'Non-subscribed';
  const titleText = channel.title || channel.channel_id;
  const avatar = channel.thumbnail_path
    ? `<img class="channel-avatar-large" src="/${escapeHtml(channel.thumbnail_path)}" alt="">`
    : '<div class="channel-avatar-large"></div>';
  const body = document.createElement('div');
  body.className = 'body';
  body.innerHTML = `
    <div class="detail-layout">
      <div>${avatar}</div>
      <div>
        <div class="title-row">
          <div class="video-title">${escapeHtml(titleText)}</div>
          <div class="channel-external-links">
            ${youtubeUrl ? `<a class="playlist-link" href="${escapeHtml(youtubeUrl)}" target="_blank" rel="noreferrer">YouTube</a><a class="external-link" href="${escapeHtml(youtubeUrl)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(titleText)} on YouTube">${externalLinkSvg()}</a>` : ''}
            ${archivarixUrl ? `<a class="playlist-link" href="${escapeHtml(archivarixUrl)}" target="_blank" rel="noreferrer">Archivarix</a><a class="external-link" href="${escapeHtml(archivarixUrl)}" target="_blank" rel="noreferrer" title="Open on Archivarix" aria-label="Open ${escapeHtml(titleText)} on Archivarix">${externalLinkSvg()}</a>` : ''}
          </div>
        </div>
        <div class="details">
          <span>${subscribedLabel}</span>
          ${channelNotificationHtml(channel)}
          ${status ? `<span class="badge">${escapeHtml(status)}</span>` : ''}
          ${channel.channel_id ? `<span>${escapeHtml(channel.channel_id)}</span>` : ''}
          ${channel.archivarix_channel_id ? `<span>Archivarix ${escapeHtml(channel.archivarix_channel_id)}</span>` : ''}
        </div>
        ${channelDatesHtml(channel)}
        ${channel.status_reason ? `<div class="status">${escapeHtml(channel.status_reason)}</div>` : ''}
        ${channel.aliases ? `<div class="details"><span>${escapeHtml(channel.aliases)}</span></div>` : ''}
        ${channel.description ? `<div class="description">${escapeHtml(channel.description)}</div>` : '<div class="empty">No channel description captured.</div>'}
      </div>
    </div>
  `;
  article.append(body);
  return article;
}

function channelDatesHtml(channel) {
  const subscribedDate = channel.subscribed_at
    ? window.YTLibraryTime.formatDate(channel.subscribed_at)
    : '';
  const firstSeenDate = channel.first_seen_at
    ? window.YTLibraryTime.formatDate(channel.first_seen_at)
    : '';
  const values = [];
  if (subscribedDate) values.push(`<span>Subscribed ${escapeHtml(subscribedDate)}</span>`);
  if (firstSeenDate && firstSeenDate !== subscribedDate) {
    values.push(`<span>First seen ${escapeHtml(firstSeenDate)}</span>`);
  }
  return values.length
    ? `<div class="details channel-first-seen">${values.join('')}</div>`
    : '';
}

function playlistCreatedHtml(playlist) {
  if (!playlist.created_at) return '';
  const date = window.YTLibraryTime.formatDate(playlist.created_at);
  return date ? `<span>Created ${escapeHtml(date)}</span>` : '';
}

function channelTabsFor(activeTab, playlistCount, historyCount) {
  const tabs = document.createElement('div');
  tabs.className = 'channel-tabs';
  tabs.setAttribute('role', 'tablist');
  for (const [key, label, count] of [
    ['playlists', 'Playlists', playlistCount],
    ['history', 'History', historyCount],
  ]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `channel-tab${key === activeTab ? ' active' : ''}`;
    button.dataset.channelTab = key;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(key === activeTab));
    button.textContent = `${label} (${count === null ? '...' : Number(count || 0).toLocaleString()})`;
    tabs.append(button);
  }
  return tabs;
}

async function render() {
  const generation = ++renderGeneration;
  cancelAdjacentPagePrefetch();
  setDocumentTitle();
  if (selected !== '__search__') {
    searchResultsRendered = false;
    stopSearchMetaProgress();
  }
  if (!data) {
    title.textContent = '';
    meta.textContent = '';
    return;
  }
  syncSearchFiltersForSelection();
  const query = search.value.trim().toLowerCase();
  viewTop.classList.toggle('history-top', selected === '__history__');
  viewTop.classList.toggle(
    'video-collection-top',
    selected.startsWith('__playlist__:'),
  );
  stopSearchProgress();
  empty.textContent = 'No playlists match.';
  const preserveHistoryChrome = Boolean(viewContext.querySelector('.history-heatmap')) && (
    selected === '__history__'
    || (selected.startsWith('__channel__:') && channelDetailTab === 'history')
  );
  const preserveRemotePager = !bottomPager.hidden && (
    (selected === '__search__' && omniSearchCache.size > 0)
    || (selected !== '__search__' && viewDataCache.size > 0)
  );
  if (!preserveHistoryChrome && !preserveRemotePager) {
    hidePager();
  }
  if (!preserveHistoryChrome) {
    viewContext.replaceChildren();
    viewContext.hidden = true;
  }
  syncSidebarSelection();
  if (selected.startsWith('__video__:')) {
    const videoId = selected.slice('__video__:'.length);
    title.textContent = 'Video';
    meta.textContent = '';
    let video;
    let pluginPanels = [];
    try {
      [video, pluginPanels] = await Promise.all([
        fetchViewData(`/api/videos/${encodeURIComponent(videoId)}`),
        renderBrowserPluginVideoPanels(videoId),
      ]);
    } catch (error) {
      if (generation !== renderGeneration) return;
      title.textContent = 'Video not found';
      meta.textContent = videoId;
      grid.replaceChildren();
      empty.hidden = false;
      empty.textContent = error.message;
      return;
    }
    if (generation !== renderGeneration) return;
    setDocumentTitle(displayVideoTitle(video) || videoId);
    hidePager();
    grid.className = 'grid';
    grid.replaceChildren(
      videoDetailCardFor(video),
      ...pluginPanels,
    );
    empty.hidden = true;
    return;
  }
  if (selected.startsWith('__channel__:')) {
    const channelReference = selected.slice('__channel__:'.length);
    title.textContent = 'Channel';
    let channel;
    let playlistSummary;
    try {
      [channel, playlistSummary] = await Promise.all([
        fetchViewData(`/api/channels/${encodeChannelReference(channelReference)}`),
        fetchViewData(`/api/channels/${encodeChannelReference(channelReference)}/videos?limit=1&offset=0&sort=title`),
      ]);
    } catch (error) {
      if (generation !== renderGeneration) return;
      title.textContent = 'Channel not found';
      meta.textContent = channelReference;
      grid.replaceChildren();
      empty.hidden = false;
      empty.textContent = error.message;
      return;
    }
    if (generation !== renderGeneration) return;
    const channelId = channel.channel_id || channelReference;
    setDocumentTitle(channel.title || channelReference);
    const playlistCount = Number(playlistSummary.total || 0);
    const historyCount = cachedChannelHistoryCount(channelId);
    const currentHeatmap = channelDetailTab === 'history'
      ? viewContext.querySelector(`.history-heatmap[data-history-channel-id="${CSS.escape(channelId)}"]`)
      : null;
    viewContext.hidden = false;
    viewContext.replaceChildren(
      channelDetailCardFor(channel),
      channelTabsFor(channelDetailTab, playlistCount, historyCount),
      ...(currentHeatmap ? [currentHeatmap] : []),
    );
    grid.className = channelDetailTab === 'history' ? 'history-list' : 'grid';
    if (channelDetailTab === 'history') {
      meta.textContent = 'Loading channel history...';
      grid.replaceChildren();
      empty.hidden = true;
      const [payload, initialActivity] = await fetchHistoryLocation(channelId);
      const rows = payload.watch || [];
      const activity = historyActivitySyncEnabled
        && syncHistoryActivityYearWithRows(rows, pendingHistoryDate)
        ? await fetchHistoryActivity(channelId)
        : initialActivity;
      const total = Number(payload.totals?.filtered_watch_rows ?? payload.totals?.watch_rows ?? rows.length);
      if (generation !== renderGeneration) return;
      viewContext.replaceChildren(
        channelDetailCardFor(channel),
        channelTabsFor('history', playlistCount, total),
        historyHeatmapFor(activity),
      );
      const pageInfo = remotePageInfo(total, rows.length);
      meta.textContent = '';
      renderPager(pageInfo);
      grid.replaceChildren(...historyRowsWithDayDividers(rows));
      empty.hidden = rows.length !== 0;
      empty.textContent = 'No history rows match this channel.';
      scrollToPendingHistoryDate();
      scheduleAdjacentPagePrefetch(
        pageInfo,
        page => fetchHistoryPage(channelId, page),
        historyYearPagePrefetches(channelId, rows),
      );
    } else {
      const payload = await fetchVideoCollection({ channelId, sort: 'title' });
      if (generation !== renderGeneration) return;
      const rows = payload.results || [];
      const pageInfo = remotePageInfo(Number(payload.total || 0), rows.length, Number(payload.limit || 100));
      meta.textContent = '';
      renderPager(pageInfo);
      grid.replaceChildren(...rows.map(playlistVideoCardFor));
      empty.hidden = rows.length !== 0;
      empty.textContent = 'No playlist videos match this channel.';
      scheduleAdjacentPagePrefetch(pageInfo, page => (
        fetchVideoCollection({ channelId, sort: 'title', page })
      ));
      if (historyCount === null) {
        void fetchChannelHistoryCount(channelId).then(() => {
          if (selected === channelSelection(channelReference) && channelDetailTab === 'playlists') render();
        }).catch(() => {});
      }
    }
    return;
  }
  if (selected === '__search__') {
    const preserveSearchContent = (
      renderedOmniSearchQuery === query
      && searchResultsRendered
    );
    if (preserveSearchContent) {
      showSearchProgress({ preserveContent: true });
    } else {
      title.textContent = '';
      meta.textContent = '';
      renderSearchMetaFilters();
      showSearchHeaderProgress();
      showSearchProgress();
      await new Promise(resolve => requestAnimationFrame(resolve));
      if (generation !== renderGeneration) return;
    }
    let payload;
    try {
      payload = await fetchOmniSearch(query);
    } catch (error) {
      if (generation !== renderGeneration) return;
      stopSearchMetaProgress();
      stopSearchHeaderProgress();
      stopSearchProgress();
      searchResultsRendered = false;
      title.textContent = 'Search unavailable';
      empty.hidden = false;
      empty.textContent = error.message;
      return;
    }
    if (generation !== renderGeneration) return;
    stopSearchMetaProgress();
    stopSearchHeaderProgress();
    stopSearchProgress();
    const rows = payload.results || [];
    const total = Number(payload.total || 0);
    const remoteLimit = Number(payload.limit || pageSizeNumber() || 100);
    currentPage = Math.floor(Number(payload.offset || 0) / remoteLimit) + 1;
    renderedOmniSearchQuery = query;
    searchResultsRendered = true;
    title.textContent = '';
    const totalLabel = `${total.toLocaleString()}${payload.totalIsExact === false ? '+' : ''} results`;
    meta.innerHTML = rightPanelListMetaHtml(totalLabel, {
      showLayout: true,
      sortHtml: searchResultsSortHtml(),
    });
    for (const pluginError of payload.pluginErrors || []) {
      const warning = document.createElement('div');
      warning.className = 'status plugin-search-warning';
      warning.textContent = `${pluginError.label} unavailable: ${pluginError.message}`;
      meta.append(warning);
    }
    renderSearchMetaFilters(payload);
    const pageInfo = remotePageInfo(total, rows.length, remoteLimit);
    renderPager(pageInfo);
    applySearchCardLayout();
    grid.replaceChildren(...rows.map(result => searchResultCardFor(result, {
      query,
      searchFields: payload.searchFields,
    })));
    empty.hidden = rows.length !== 0;
    empty.textContent = searchPresetEmptyMessage(query) || 'No results match.';
    scheduleAdjacentPagePrefetch(pageInfo, page => fetchOmniSearch(query, page));
    return;
  }
  if (selected.startsWith('__playlist__:')) {
    const playlistId = selected.slice('__playlist__:'.length);
    resetPlaylistVisibilityFor(playlistId);
    let playlist;
    let payload;
    try {
      [playlist, payload] = await Promise.all([
        fetchViewData(`/api/playlists/${encodeURIComponent(playlistId)}`),
        fetchVideoCollection({
          playlistId,
          sort: playlistViewSort,
          query: playlistPageSearch.trim(),
          completion: playlistCompletionVisibility,
          partialMinimumPercent: partialCompletionMinimumPercent,
          duplicatesOnly: playlistDuplicatesOnly,
        }),
      ]);
    } catch (error) {
      if (generation !== renderGeneration) return;
      title.textContent = 'Playlist not found';
      meta.textContent = playlistId;
      grid.replaceChildren();
      empty.hidden = false;
      empty.textContent = error.message;
      return;
    }
    if (generation !== renderGeneration) return;
    if (playlistDuplicatesOnly && Number(payload.duplicateCount || 0) === 0) {
      playlistDuplicatesOnly = false;
      payload = await fetchVideoCollection({
        playlistId,
        sort: playlistViewSort,
        query: playlistPageSearch.trim(),
        completion: playlistCompletionVisibility,
        partialMinimumPercent: partialCompletionMinimumPercent,
        duplicatesOnly: false,
      });
      if (generation !== renderGeneration) return;
    }
    setDocumentTitle(playlist.title || playlistId);
    const rows = payload.results || [];
    const playlistCount = playlistVideoCountLabel(playlist);
    const playlistHeadingMeta = [
      playlistCount ? `<span>${escapeHtml(playlistCount)}</span>` : '',
      playlistVisibilityLabelHtml(playlist),
      playlistStatusLabelHtml(playlist),
    ].filter(Boolean).join('');
    title.innerHTML = `
      <span class="playlist-page-heading">
        <span class="title-with-action">
          <span>${escapeHtml(playlist.title)}</span>
          ${playlist.url ? `<a class="external-link" href="${escapeHtml(playlist.url)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(playlist.title)} on YouTube">${externalLinkSvg()}</a>` : ''}
        </span>
        ${playlistHeadingMeta ? `<span class="details playlist-page-meta">${playlistHeadingMeta}</span>` : ''}
      </span>
    `;
    meta.innerHTML = `
      ${playlistVideoFiltersHtml(
        payload.counts,
        payload.completionCounts,
        payload.duplicateCount,
        `<input class="playlist-search" type="search" data-playlist-page-search placeholder="Search this playlist" autocomplete="off" value="${escapeHtml(playlistPageSearch)}">`,
      )}
      <span class="result-view-controls video-collection-view-controls">
        ${cardLayoutHtml(playlistCardLayout, 'playlist')}
        ${videoSortHtml(playlistViewSort, 'playlist')}
      </span>
    `;
    syncMetaFilterGroup('playlist-videos');
    syncMetaFilterGroup('playlist-completion');
    const pageInfo = remotePayloadPageInfo(payload, rows.length);
    renderPager(pageInfo);
    applyPlaylistCardLayout();
    grid.replaceChildren(...rows.map(video => playlistVideoCardFor(video, { showPosition: true })));
    empty.hidden = rows.length !== 0;
    empty.textContent = playlist.scanned_at ? 'No videos match.' : 'This playlist has not been scanned yet.';
    scheduleAdjacentPagePrefetch(pageInfo, page => fetchVideoCollection({
      playlistId,
      sort: playlistViewSort,
      query: playlistPageSearch.trim(),
      completion: playlistCompletionVisibility,
      partialMinimumPercent: partialCompletionMinimumPercent,
      duplicatesOnly: playlistDuplicatesOnly,
      page,
    }));
    return;
  }
  if (selected === '__history__') {
    await renderHistoryView();
    return;
  }
}

function cardFor(playlist, options = {}) {
  const localHref = localPlaylistHref(playlist.playlist_id);
  const playlistCount = playlistVideoCountLabel(playlist);
  const owner = playlistOwnerHtml(playlist);
  return CollectionCard.create({
    resultKind: options.resultKind,
    thumbnailPath: playlist.thumbnail_path,
    thumbnailHref: localHref,
    placeholderThumbnail: true,
    headerHtml: owner ? `<div class="details video-card-channel">${owner}</div>` : '',
    titleHtml: `<a class="playlist-title" href="${localHref}">${escapeHtml(playlist.title)}</a>`,
    actionsHtml: `<a class="external-link" href="${escapeHtml(playlist.url)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(playlist.title)} on YouTube">${externalLinkSvg()}</a>`,
    bodyHtml: `
    <div class="details">
      ${playlistCount ? `<span>${escapeHtml(playlistCount)}</span>` : ''}
      ${playlist.unavailable_count ? `<span class="badge">${playlist.unavailable_count} unavailable</span>` : ''}
      ${playlistVisibilityLabelHtml(playlist)}
      ${playlistStatusLabelHtml(playlist)}
    </div>
    <div class="details">
      ${playlist.playlist_id ? `<span>${escapeHtml(playlist.playlist_id)}</span>` : ''}
      ${playlistCreatedHtml(playlist)}
    </div>
    `,
  });
}

function playlistStatusLabelHtml(playlist) {
  if (playlist.fetch_status === 'removed') return '<span class="status">Removed</span>';
  if (playlist.fetch_status === 'unavailable') return '<span class="status">Unavailable</span>';
  if (playlist.fetch_status === 'error') return '<span class="status">Fetch failed</span>';
  return '';
}

function playlistVideoCountLabel(playlist) {
  const reported = Number(playlist.video_count || 0);
  const scanned = Number(playlist.scanned_video_count || 0);
  const count = reported || scanned;
  if (!count) return '';
  const incomplete = Boolean(
    playlist.scan_status !== 'removed'
    && playlist.scan_status !== 'unavailable'
    && (
      playlist.scan_status === 'error'
      || (reported && scanned && reported !== scanned)
    )
  );
  return `${count} videos${incomplete ? ' (incomplete)' : ''}`;
}

function playlistOwnerHtml(playlist) {
  const owner = playlistOwnerForDisplay(playlist);
  const name = cleanPlaylistOwnerName(owner.title || '');
  if (!name) return '';
  const href = owner.reference
    ? localChannelHref(owner.reference)
    : (owner.url || '');
  return creatorHtml(owner.thumbnail_path || '', name, href);
}

function playlistOwnerForDisplay(playlist) {
  const title = playlist.owner_channel_title || '';
  if (title) {
    return {
      title,
      channel_id: playlist.owner_channel_id || '',
      reference: playlist.owner_channel_reference || playlist.owner_channel_id || '',
      url: playlist.owner_channel_url || '',
      thumbnail_path: playlist.owner_channel_thumbnail_path || '',
    };
  }
  return {};
}

function cleanPlaylistOwnerName(value) {
  const text = String(value || '').trim();
  return text.toLowerCase().startsWith('by ') ? text.slice(3).trim() : text;
}

function videoCardFor(options) {
  return VideoCard.create({ ...options, externalIconHtml: externalLinkSvg() });
}

function playlistVideoCardFor(video, options = {}) {
  const watchUrl = youtubeWatchUrl(video);
  const channelName = displayVideoChannel(video);
  const channelUrl = displayVideoChannelLocalUrl(video);
  return videoCardFor({
    thumbnailPath: video.metadata_thumbnail_path,
    progressVideo: video,
    resultKind: options.resultKind,
    position: options.showPosition ? video.position : '',
    title: displayVideoTitle(video),
    titleHtml: options.titleHtml,
    localUrl: video.video_id ? localVideoHref(video.video_id) : '',
    externalUrl: options.externalUrl === undefined ? watchUrl : options.externalUrl,
    badges: [
      { label: wasRemovedByMeFromPlaylist(video) ? 'Removed' : '' },
      { label: matchTypeLabel(video), title: video.match_note },
      ...(options.badges || []),
    ],
    details: [
      ...(options.details || []),
      displayVideoDuration(video) ? `<span>${escapeHtml(displayVideoDuration(video))}</span>` : '',
      video.video_id ? `<span>${escapeHtml(video.video_id)}</span>` : '',
      archivarixLinkHtml(video),
    ],
    recoveryHtml: archivarixStatusHtml(video),
    channelHtml: creatorHtml(video.metadata_channel_thumbnail_path, channelName, channelUrl),
    sources: options.sources || [],
    playlistSourcesHtml: options.playlistSourcesHtml === undefined ? playlistSourceLinksHtml(video) : options.playlistSourcesHtml,
    watchDateHtml: options.watchDateHtml || '',
    latestWatchDateHtml: options.latestWatchDateHtml || '',
    availabilityHtml: videoAvailabilityHtml(video),
    watchedHtml: watchedLineHtml(video),
    sparklineHtml: watchSparklineHtml(video),
    reactionHtml: reactionIconsHtml(video),
    uploaderCategory: video.uploader_category,
    description: options.description === undefined ? video.metadata_description : options.description,
    descriptionHtml: options.descriptionHtml,
  });
}

function searchVideoCardFor(video, options = {}) {
  return playlistVideoCardFor(video, {
    ...options,
    resultKind: options.resultKind || 'Video',
    latestWatchDateHtml: options.latestWatchDateHtml === undefined
      ? latestWatchDateHtml(video)
      : options.latestWatchDateHtml,
    badges: [
      ...(Array.isArray(video.plugin_badges) ? video.plugin_badges : []),
      ...(options.badges || []),
    ],
  });
}

function historyWatchedAtLabel(video) {
  if (video.time_quality === 'exact' && video.watched_at) {
    return window.YTLibraryTime.format(video.watched_at);
  }
  return video.watch_date || '';
}

function historyDayLabel(video) {
  const value = video.watched_at || video.watch_date || '';
  const dateLabel = window.YTLibraryTime.formatDate(value);
  if (!dateLabel) return '';
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const parsed = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(value);
  if (Number.isNaN(parsed.getTime())) return dateLabel;
  const options = { weekday: 'short' };
  if (!dateOnly) {
    options.timeZone = window.YTLibraryTime.timeZone || window.YTLibraryTime.detected();
  }
  const weekday = new Intl.DateTimeFormat(undefined, options).format(parsed);
  return `${weekday}, ${dateLabel}`;
}

function historyDayDividerFor(label, date) {
  const divider = document.createElement('div');
  divider.className = 'history-day-divider';
  divider.dataset.historyDate = date;
  divider.setAttribute('role', 'separator');
  divider.setAttribute('aria-label', label);
  const text = document.createElement('span');
  text.textContent = label;
  divider.append(text);
  return divider;
}

function historyRowsWithDayDividers(rows, options = {}) {
  const elements = [];
  let previousLabel = '';
  for (const row of rows) {
    const label = historyDayLabel(row);
    if (label && label !== previousLabel) {
      elements.push(historyDayDividerFor(label, historyRowDateKey(row)));
    }
    elements.push(historyRowCardFor(row, options));
    previousLabel = label;
  }
  return elements;
}

function historyRowCardFor(video, { layout = 'detailed' } = {}) {
  const watched = historyWatchedAtLabel(video);
  const article = playlistVideoCardFor(video, {
    playlistSourcesHtml: playlistSourceLinksHtml(video),
    watchDateHtml: watched
      ? `<div class="details"><span>Watched ${escapeHtml(watched)}</span></div>`
      : '',
  });
  article.classList.add('history-card');
  article.classList.toggle('history-row', layout !== 'grid');
  const watchDate = historyRowDateKey(video);
  if (watchDate) article.dataset.watchDate = watchDate;
  if (!article.querySelector('.thumb-wrap')) {
    const placeholder = document.createElement('div');
    placeholder.className = 'thumb-wrap';
    article.insertBefore(placeholder, article.firstChild);
  }
  return article;
}

function searchResultCardFor(result, options = {}) {
  if (result.kind === 'plugin') {
    const plugin = browserSearchPlugin(result.pluginId);
    const card = plugin?.search?.renderResult?.(
      result.item,
      browserPluginHost(result.pluginId),
    );
    if (card instanceof HTMLElement) return card;
  }
  if (result.kind === 'playlist') {
    return cardFor(result.item, { resultKind: 'Playlist' });
  }
  if (result.kind === 'channel') {
    return channelCardFor(result.item, { resultKind: 'Channel' });
  }
  const video = result.item;
  const query = String(options.query || '');
  const searchFields = new Set(options.searchFields || []);
  const titleText = displayVideoTitle(video);
  const descriptionText = String(video.metadata_description || '');
  const card = searchVideoCardFor(video, {
    titleHtml: query && searchFields.has('titles')
      ? searchHighlight.textHtml(titleText, query)
      : undefined,
    descriptionHtml: query && descriptionText && searchFields.has('descriptions')
      ? searchHighlight.excerptHtml(descriptionText, query)
      : undefined,
  });
  for (const plugin of browserSearchPlugins().filter(item => searchKindEnabled(item.id))) {
    if (typeof plugin.search.decorateCoreResultCard !== 'function') continue;
    try {
      plugin.search.decorateCoreResultCard(card, result, browserPluginHost(plugin.id));
    } catch (error) {
      console.error(`Plugin card decoration failed: ${plugin.id}`, error);
    }
  }
  return card;
}

function channelCardFor(channel, options = {}) {
  const youtubeUrl = channel.url || (channel.channel_id ? `https://www.youtube.com/channel/${encodeURIComponent(channel.channel_id)}` : '');
  const archivarixUrl = channel.channel_id ? `https://tube.archivarix.net/?q=${encodeURIComponent(channel.channel_id)}` : '';
  const status = String(channel.status || '').toLowerCase();
  const subscribedLabel = isSubscribedChannel(channel) ? 'Subscribed' : 'Non-subscribed';
  const titleText = channel.title || channel.channel_id;
  const channelReference = channel.preferred_reference || channel.channel_id || '';
  const titleHtml = creatorHtml(
    channel.thumbnail_path || '',
    titleText,
    channelReference ? localChannelHref(channelReference) : ''
  );
  return CollectionCard.create({
    resultKind: options.resultKind,
    thumbnailPath: channel.thumbnail_path,
    titleHtml: `<div class="playlist-title">${titleHtml || escapeHtml(titleText)}</div>`,
    actionsHtml: youtubeUrl ? `<a class="external-link" href="${escapeHtml(youtubeUrl)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(titleText)} on YouTube">${externalLinkSvg()}</a>` : '',
    bodyHtml: `
    <div class="details">
      <span>${subscribedLabel}</span>
      ${channelNotificationHtml(channel)}
      ${status ? `<span class="badge">${escapeHtml(status)}</span>` : ''}
      ${channel.channel_id ? `<span>${escapeHtml(channel.channel_id)}</span>` : ''}
      ${channel.archivarix_channel_id ? `<span>Archivarix ${escapeHtml(channel.archivarix_channel_id)}</span>` : ''}
    </div>
    ${channelDatesHtml(channel)}
    ${channel.status_reason ? `<div class="status">${escapeHtml(channel.status_reason)}</div>` : ''}
    ${channel.aliases ? `<div class="details"><span>${escapeHtml(channel.aliases)}</span></div>` : ''}
    ${channel.description ? `<div class="description">${escapeHtml(channel.description)}</div>` : ''}
    <div class="details">
      ${youtubeUrl ? `<a class="playlist-link" href="${escapeHtml(youtubeUrl)}" target="_blank" rel="noreferrer">YouTube</a>` : ''}
      ${archivarixUrl ? `<a class="playlist-link" href="${escapeHtml(archivarixUrl)}" target="_blank" rel="noreferrer">Archivarix</a>` : ''}
    </div>
    `,
  });
}

function externalLinkSvg() {
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7"></path><path d="M8 7h9v9"></path><path d="M7 7H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-2"></path></svg>';
}

search.addEventListener('input', () => {
  if (searchInputTimer !== null) {
    clearTimeout(searchInputTimer);
    searchInputTimer = null;
  }
  currentPage = 1;
  if (!searchSortExplicit) searchResultsSort = preferredSearchResultsSort();
  const wasSearchHash = window.location.hash === '#search' || window.location.hash.startsWith('#search?');
  if (!wasSearchHash) {
    activeSearchPreset = '';
    searchPlaylistGroupKey = '';
    searchChannelGroupKey = '';
    resetSearchMetaVisibility();
    if (!searchSortExplicit) searchResultsSort = preferredSearchResultsSort();
  }
  selected = '__search__';
  renderGroups();
  const changed = updateSearchHash(wasSearchHash);
  if (changed && !wasSearchHash) return;
  searchInputTimer = setTimeout(() => {
    searchInputTimer = null;
    render();
  }, 250);
});
historyNav?.addEventListener('click', () => setSelected('__history__'));
searchNav?.addEventListener('click', activateSearchNavigation);
viewContext.addEventListener('change', event => {
  const syncToggle = event.target.closest('[data-history-sync]');
  if (syncToggle instanceof HTMLInputElement) {
    void setHistoryActivitySync(syncToggle.checked);
  }
});
viewContext.addEventListener('click', event => {
  const currentHistory = event.target.closest('[data-history-current]');
  if (currentHistory instanceof HTMLButtonElement && !currentHistory.disabled) {
    void jumpToCurrentHistoryActivity();
    return;
  }
  const yearShift = event.target.closest('[data-history-year-shift]');
  if (yearShift instanceof HTMLButtonElement && !yearShift.disabled) {
    const delta = Number(yearShift.dataset.historyYearShift || 0);
    if (delta) void shiftHistoryActivityYear(delta);
    return;
  }
  const historyDay = event.target.closest('[data-history-date]');
  if (historyDay instanceof HTMLButtonElement) {
    const date = historyDay.dataset.historyDate || '';
    const offset = Number(historyDay.dataset.historyOffset || 0);
    if (date) void jumpToHistoryDate(date, offset);
    return;
  }
  const target = event.target.closest('[data-channel-tab]');
  if (!(target instanceof HTMLButtonElement)) return;
  channelDetailTab = target.dataset.channelTab || 'playlists';
  historyNavigationDate = '';
  pendingHistoryDate = '';
  currentPage = 1;
  updateCurrentHash(true);
  scrollResultsToTop();
  render();
});
function handleMetaChange(event) {
  const target = event.target;
  if (target instanceof HTMLSelectElement && target.dataset.videoSort !== undefined) {
    if (target.dataset.videoSort === 'playlist') {
      const nextSort = target.value || 'playlist_order';
      const previousSort = playlistViewSort;
      const previousPreference = sortPreferences.playlist;
      playlistViewSort = nextSort;
      saveSortPreference('playlist', nextSort, previousPreference, () => {
        if (!selected.startsWith('__playlist__:') || playlistViewSort !== nextSort) return;
        playlistViewSort = previousSort;
        currentPage = 1;
        void render();
      });
    }
    currentPage = 1;
    scrollResultsToTop();
    render();
    return;
  }
  if (target instanceof HTMLSelectElement && target.dataset.searchSort !== undefined) {
    const context = searchSortPreferenceContext();
    const nextSort = target.value || 'relevance';
    const previousSort = searchResultsSort;
    const previousExplicit = searchSortExplicit;
    const previousPreference = sortPreferences[context];
    searchResultsSort = nextSort;
    searchSortExplicit = true;
    saveSortPreference(context, nextSort, previousPreference, () => {
      if (searchSortPreferenceContext() !== context || searchResultsSort !== nextSort) return;
      searchResultsSort = previousSort;
      searchSortExplicit = previousExplicit;
      currentPage = 1;
      syncSearchHashAndRender();
    });
    currentPage = 1;
    scrollResultsToTop();
    syncSearchHashAndRender();
    return;
  }
  if (!(target instanceof HTMLInputElement)) return;
  const searchFilterInteraction = (
    target.dataset.searchKindFilter
    || target.dataset.searchPluginFacetFilter
    || target.dataset.searchMetaFilter
    || target.dataset.searchCompletionMinimum !== undefined
    || String(target.dataset.metaAllFilter || '').startsWith('search-')
  );
  const activatedFromHistory = searchFilterInteraction
    ? activateSearchFromHistory()
    : false;
  if (target.dataset.searchCompletionMinimum !== undefined) {
    const previousMinimum = setPartialCompletionMinimum(target.value);
    target.value = String(partialCompletionMinimumPercent);
    if (
      previousMinimum === partialCompletionMinimumPercent
      && searchMetaVisibility.completion.partial
    ) return;
    savePartialCompletionMinimum(
      partialCompletionMinimumPercent,
      previousMinimum,
    );
    searchMetaVisibility.completion.partial = true;
    const partialInput = searchForFilters.querySelector(
      '[data-search-meta-filter="completion:partial"]'
    );
    if (partialInput instanceof HTMLInputElement) partialInput.checked = true;
    syncMetaFilterGroup('search-completion');
    restoreEmptySearchKindFacets('completion');
    refreshSearchAfterFilterChange('completion', activatedFromHistory);
    return;
  }
  if (target.dataset.playlistCompletionMinimum !== undefined) {
    const previousMinimum = setPartialCompletionMinimum(target.value);
    target.value = String(partialCompletionMinimumPercent);
    if (
      previousMinimum === partialCompletionMinimumPercent
      && playlistCompletionVisibility.partial
    ) return;
    savePartialCompletionMinimum(
      partialCompletionMinimumPercent,
      previousMinimum,
    );
    playlistCompletionVisibility.partial = true;
    const partialInput = meta.querySelector(
      '[data-playlist-completion-filter="partial"]'
    );
    if (partialInput instanceof HTMLInputElement) partialInput.checked = true;
    currentPage = 1;
    syncMetaFilterGroup('playlist-completion');
    render();
    return;
  }
  const searchKindFilter = target.dataset.searchKindFilter;
  if (searchKindFilter && setSearchKindFilter(searchKindFilter, target.checked)) {
    const plugin = browserSearchPlugin(searchKindFilter);
    if (plugin) {
      if (plugin.search.preferenceKey) {
        saveFilterPreference(plugin.search.preferenceKey, target.checked);
      }
    } else {
      saveSearchOptInPreferences(searchKindFacetKeys(searchKindFilter));
    }
    if (
      plugin
      && browserPluginSearchActive(plugin)
      && browserPluginForcesRelevance(plugin)
    ) {
      searchResultsSort = 'relevance';
      searchSortExplicit = false;
    }
    refreshSearchAfterFilterChange(searchKindFilter, activatedFromHistory);
    return;
  }
  const searchPluginFacetFilter = target.dataset.searchPluginFacetFilter;
  if (searchPluginFacetFilter) {
    const [pluginId, filterName] = searchPluginFacetFilter.split(':');
    const plugin = browserVideoFilterPlugins().find(item => item.id === pluginId);
    if (!plugin) return;
    const state = browserVideoFacetState(plugin);
    if (!Object.prototype.hasOwnProperty.call(state, filterName)) return;
    state[filterName] = target.checked;
    saveBrowserVideoFacetPreferences(plugin);
    if (target.checked && !searchKindEnabled('videos')) {
      enableDefaultSearchKind('videos');
      renderSearchMetaFilters();
    }
    if (browserPluginSearchActive(plugin) && browserPluginForcesRelevance(plugin)) {
      searchResultsSort = 'relevance';
      searchSortExplicit = false;
    }
    syncMetaFilterGroup(`search-plugin-${plugin.id}`);
    refreshSearchAfterFilterChange('videos', activatedFromHistory);
    return;
  }
  const metaAllFilter = target.dataset.metaAllFilter;
  const selectAllMetaChildren = metaAllFilter
    ? !allMetaFilterChildrenChecked(metaAllFilter)
    : false;
  if (metaAllFilter && setMetaFilterGroup(metaAllFilter, selectAllMetaChildren)) {
    if (metaAllFilter.startsWith('search-plugin-')) {
      const pluginId = metaAllFilter.slice('search-plugin-'.length);
      const plugin = browserVideoFilterPlugins().find(item => item.id === pluginId);
      if (!plugin) return;
      saveBrowserVideoFacetPreferences(plugin);
      if (selectAllMetaChildren && !searchKindEnabled('videos')) {
        enableDefaultSearchKind('videos');
        renderSearchMetaFilters();
      }
      syncMetaFilterGroup(metaAllFilter);
      refreshSearchAfterFilterChange('videos', activatedFromHistory);
    } else if (metaAllFilter.startsWith('search-')) {
      const facetKey = metaAllFilter.slice('search-'.length);
      saveSearchOptInPreferences([facetKey]);
      syncMetaFilterGroup(metaAllFilter);
      if (target.checked) restoreEmptySearchKindFacets(facetKey);
      refreshSearchAfterFilterChange(facetKey, activatedFromHistory);
    } else {
      if (metaAllFilter === 'playlist-completion') {
        saveFilterPreference(
          filterPreferenceKeys.lowPartialCompletion,
          playlistCompletionVisibility.partial_below_minimum,
        );
      } else if (metaAllFilter === 'playlist-videos') {
        savePlaylistVideoOptInPreferences();
      }
      render();
    }
    return;
  }
  const searchMetaFilter = target.dataset.searchMetaFilter;
  if (searchMetaFilter) {
    const [groupName, filterName] = searchMetaFilter.split(':');
    const visibility = searchMetaVisibility[groupName];
    if (!visibility || !Object.prototype.hasOwnProperty.call(visibility, filterName)) return;
    visibility[filterName] = target.checked;
    const optInFilter = searchOptInFilter(groupName, filterName);
    if (optInFilter) {
      saveFilterPreference(optInFilter.preferenceKey, target.checked);
    }
    syncMetaFilterGroup(`search-${groupName}`);
    if (target.checked) restoreEmptySearchKindFacets(groupName);
    refreshSearchAfterFilterChange(groupName, activatedFromHistory);
    return;
  }
  const playlistCompletionFilter = target.dataset.playlistCompletionFilter;
  if (playlistCompletionFilter) {
    playlistCompletionVisibility[playlistCompletionFilter] = target.checked;
    if (playlistCompletionFilter === 'partial_below_minimum') {
      saveFilterPreference(filterPreferenceKeys.lowPartialCompletion, target.checked);
    }
    currentPage = 1;
    render();
    return;
  }
  if (target.dataset.playlistDuplicatesFilter) {
    playlistDuplicatesOnly = target.checked;
    currentPage = 1;
    render();
    return;
  }
  const filter = target.dataset.playlistFilter;
  if (!filter) return;
  const playlistFilter = playlistVideoOptInFilters.find(item => item.key === filter);
  if (playlistFilter) {
    saveFilterPreference(playlistFilter.preferenceKey, target.checked);
  } else {
    playlistVisibility[filter] = target.checked;
  }
  currentPage = 1;
  render();
}
meta.addEventListener('change', handleMetaChange);
searchForFilters.addEventListener('change', handleMetaChange);
searchForFilters.addEventListener('click', event => {
  if (!(event.target instanceof Element)) return;
  const button = event.target.closest('[data-search-tree-toggle]');
  if (!(button instanceof HTMLButtonElement)) return;
  const nodeId = button.dataset.searchTreeToggle || '';
  if (nodeId) toggleSearchFilterTreeNode(nodeId);
});
function scheduleCompletionMinimumInput(event) {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) return;
  if (
    target.dataset.searchCompletionMinimum === undefined
    && target.dataset.playlistCompletionMinimum === undefined
  ) return;
  if (completionMinimumInputTimer !== null) {
    clearTimeout(completionMinimumInputTimer);
  }
  completionMinimumInputTimer = setTimeout(() => {
    completionMinimumInputTimer = null;
    handleMetaChange({ target });
  }, 250);
}
meta.addEventListener('input', scheduleCompletionMinimumInput);
searchForFilters.addEventListener('input', scheduleCompletionMinimumInput);
function applyCardLayoutPreference(context, layout) {
  if (context === 'history') {
    historyCardLayout = layout;
    applyHistoryCardLayout();
  } else if (context === 'playlist') {
    playlistCardLayout = layout;
    applyPlaylistCardLayout();
  } else {
    searchCardLayout = layout;
    applySearchCardLayout();
  }
  for (const option of meta.querySelectorAll(`[data-card-layout-context="${context}"]`)) {
    const active = option.dataset.cardLayout === layout;
    option.classList.toggle('active', active);
    option.setAttribute('aria-pressed', String(active));
  }
}

function persistCardLayoutPreference(context, layout) {
  const save = async () => {
    const params = new URLSearchParams({ context, value: layout });
    const response = await fetch(`/api/settings/layout?${params.toString()}`, { method: 'POST' });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Layout save failed (${response.status})`);
    }
  };
  const request = cardLayoutSaveChains[context].catch(() => {}).then(save);
  cardLayoutSaveChains[context] = request;
  return request;
}

function persistSortPreference(context, value) {
  const save = async () => {
    const params = new URLSearchParams({ context, value });
    const response = await fetch(`/api/settings/sort?${params.toString()}`, { method: 'POST' });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Sort preference save failed (${response.status})`);
    }
  };
  const request = sortPreferenceSaveChain.catch(() => {}).then(save);
  sortPreferenceSaveChain = request;
  return request;
}

function saveSortPreference(context, value, previousPreference, onFailure) {
  const version = (sortPreferenceSaveVersions.get(context) || 0) + 1;
  sortPreferenceSaveVersions.set(context, version);
  sortPreferences[context] = value;
  void persistSortPreference(context, value).catch(error => {
    if (sortPreferenceSaveVersions.get(context) !== version) return;
    if (previousPreference === undefined) {
      delete sortPreferences[context];
    } else {
      sortPreferences[context] = previousPreference;
    }
    onFailure();
    window.alert(error instanceof Error ? error.message : String(error));
  });
}

function persistPageSizePreference(value) {
  const save = async () => {
    const params = new URLSearchParams({ value });
    const response = await fetch(`/api/settings/page-size?${params.toString()}`, { method: 'POST' });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Page size save failed (${response.status})`);
    }
  };
  const request = pageSizeSaveChain.catch(() => {}).then(save);
  pageSizeSaveChain = request;
  return request;
}

function persistPartialCompletionMinimum(value) {
  const save = async () => {
    const params = new URLSearchParams({ value });
    const response = await fetch(
      `/api/settings/partial-completion-minimum?${params.toString()}`,
      { method: 'POST' },
    );
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(
        result.error || `Partial completion minimum save failed (${response.status})`
      );
    }
  };
  const request = partialCompletionMinimumSaveChain.catch(() => {}).then(save);
  partialCompletionMinimumSaveChain = request;
  return request;
}

function persistFilterPreference(preferenceKey, enabled) {
  const save = async () => {
    const params = new URLSearchParams({
      key: preferenceKey,
      enabled: enabled ? '1' : '0',
    });
    const response = await fetch(
      `/api/settings/filter-preference?${params.toString()}`,
      { method: 'POST' },
    );
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Filter preference save failed (${response.status})`);
    }
  };
  const request = filterPreferenceSaveChain.catch(() => {}).then(save);
  filterPreferenceSaveChain = request;
  return request;
}

function persistSearchFilterTreePreference(expandedNodes) {
  const save = async () => {
    const params = new URLSearchParams({ expanded: expandedNodes.join(',') });
    const response = await fetch(
      `/api/settings/search-filter-tree?${params.toString()}`,
      { method: 'POST' },
    );
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Filter tree save failed (${response.status})`);
    }
  };
  const request = searchFilterTreeSaveChain.catch(() => {}).then(save);
  searchFilterTreeSaveChain = request;
  return request;
}

function saveSearchFilterTreePreference(nodeId, wasExpanded) {
  const version = ++searchFilterTreeSaveVersion;
  const expandedNodes = [...searchFilterTreeExpanded].sort();
  void persistSearchFilterTreePreference(expandedNodes).catch(error => {
    if (searchFilterTreeSaveVersion !== version) return;
    if (wasExpanded) searchFilterTreeExpanded.add(nodeId);
    else searchFilterTreeExpanded.delete(nodeId);
    applySearchFilterTreeNodeState(nodeId);
    window.alert(error instanceof Error ? error.message : String(error));
  });
}

function persistNavigationGroupTreePreference(collapsedNodes) {
  const save = async () => {
    const params = new URLSearchParams();
    for (const nodeId of collapsedNodes) params.append('collapsed', nodeId);
    const response = await fetch(
      `/api/settings/navigation-group-tree?${params.toString()}`,
      { method: 'POST' },
    );
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Navigation group tree save failed (${response.status})`);
    }
  };
  const request = navigationGroupTreeSaveChain.catch(() => {}).then(save);
  navigationGroupTreeSaveChain = request;
  return request;
}

function saveNavigationGroupTreePreference(nodeId, wasCollapsed) {
  const version = ++navigationGroupTreeSaveVersion;
  const collapsedNodes = [...navigationGroupTreeCollapsed].sort();
  void persistNavigationGroupTreePreference(collapsedNodes).catch(error => {
    if (navigationGroupTreeSaveVersion !== version) return;
    if (wasCollapsed) navigationGroupTreeCollapsed.add(nodeId);
    else navigationGroupTreeCollapsed.delete(nodeId);
    renderGroups();
    window.alert(error instanceof Error ? error.message : String(error));
  });
}

function savePartialCompletionMinimum(value, previousValue) {
  const version = ++partialCompletionMinimumSaveVersion;
  void persistPartialCompletionMinimum(value).catch(error => {
    if (partialCompletionMinimumSaveVersion !== version) return;
    partialCompletionMinimumPercent = previousValue;
    void render();
    window.alert(error instanceof Error ? error.message : String(error));
  });
}

meta.addEventListener('click', async event => {
  const button = event.target.closest('[data-card-layout]');
  if (!(button instanceof HTMLButtonElement)) return;
  const layout = button.dataset.cardLayout || '';
  const context = button.dataset.cardLayoutContext || 'search';
  const activeLayout = context === 'history'
    ? historyCardLayout
    : (context === 'playlist' ? playlistCardLayout : searchCardLayout);
  if (!cardLayouts.has(layout) || layout === activeLayout) return;
  const version = ++cardLayoutSaveVersions[context];
  applyCardLayoutPreference(context, layout);
  try {
    await persistCardLayoutPreference(context, layout);
  } catch (error) {
    if (cardLayoutSaveVersions[context] === version) {
      applyCardLayoutPreference(context, activeLayout);
      window.alert(error instanceof Error ? error.message : String(error));
    }
  }
});
meta.addEventListener('input', event => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement) || target.dataset.playlistPageSearch === undefined) return;
  playlistPageSearch = target.value;
  const cursor = target.selectionStart ?? target.value.length;
  currentPage = 1;
  if (playlistSearchTimer !== null) clearTimeout(playlistSearchTimer);
  playlistSearchTimer = setTimeout(() => {
    playlistSearchTimer = null;
    void render().then(() => {
      const replacement = meta.querySelector('[data-playlist-page-search]');
      if (replacement instanceof HTMLInputElement) {
        replacement.focus();
        replacement.setSelectionRange(cursor, cursor);
      }
    });
  }, 250);
});
function handlePagerClick(event) {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) return;
  const direction = target.dataset.page;
  if (!direction) return;
  const page = Number(direction);
  if (Number.isFinite(page) && page > 0) currentPage = page;
  historyNavigationDate = '';
  pendingHistoryDate = '';
  scrollResultsToTop();
  if (updateCurrentHash(false)) return;
  render();
}
async function handlePagerChange(event) {
  const target = event.target;
  if (!(target instanceof HTMLSelectElement)) return;
  if (target.dataset.pageSize === undefined) return;
  const previousPageSize = pageSize;
  const nextPageSize = target.value || '100';
  if (nextPageSize === previousPageSize) return;
  const version = ++pageSizeSaveVersion;
  pageSize = nextPageSize;
  currentPage = 1;
  scrollResultsToTop();
  if (!updateCurrentHash(false)) void render();
  try {
    await persistPageSizePreference(nextPageSize);
  } catch (error) {
    if (pageSizeSaveVersion === version) {
      pageSize = previousPageSize;
      currentPage = 1;
      if (!updateCurrentHash(false)) void render();
      window.alert(error instanceof Error ? error.message : String(error));
    }
  }
}
function handlePagerSubmit(event) {
  const target = event.target;
  if (!(target instanceof HTMLFormElement) || target.dataset.pageGoto === undefined) return;
  event.preventDefault();
  const input = target.querySelector('[data-page-goto-input]');
  if (!(input instanceof HTMLInputElement)) return;
  const page = Number(input.value);
  const maxPage = Number(input.max || 0);
  if (!Number.isFinite(page) || page < 1) return;
  currentPage = maxPage > 0 ? Math.min(page, maxPage) : page;
  historyNavigationDate = '';
  pendingHistoryDate = '';
  scrollResultsToTop();
  if (updateCurrentHash(false)) return;
  render();
}

function handlePageBoundaryWheel(event) {
  armPageBoundaryInputAfterIdle();
  if (!pageBoundaryTargetAllowed(event.target) || !pageBoundaryInputArmed) return;
  const direction = Math.sign(event.deltaY);
  if (!direction) return;
  const boundary = pageScrollBoundaryState();
  if ((direction > 0 && !boundary.atBottom) || (direction < 0 && !boundary.atTop)) return;
  if (navigateAcrossPageBoundary(direction)) event.preventDefault();
}

function handlePageBoundaryTouchStart(event) {
  const touch = event.touches?.[0];
  pageBoundaryTouchTargetAllowed = Boolean(touch && pageBoundaryTargetAllowed(event.target));
  pageBoundaryTouchY = touch ? touch.clientY : null;
  pageBoundaryTouchDistance = 0;
  pageBoundaryTouchDirection = 0;
}

function handlePageBoundaryTouchMove(event) {
  const touch = event.touches?.[0];
  if (!touch || pageBoundaryTouchY === null || !pageBoundaryTouchTargetAllowed) return;
  const distance = pageBoundaryTouchY - touch.clientY;
  pageBoundaryTouchY = touch.clientY;
  const direction = Math.sign(distance);
  if (!direction) return;
  const boundary = pageScrollBoundaryState();
  const beyondBoundary = direction > 0 ? boundary.atBottom : boundary.atTop;
  if (!beyondBoundary) {
    pageBoundaryTouchDistance = 0;
    pageBoundaryTouchDirection = 0;
    return;
  }
  if (pageBoundaryTouchDirection !== direction) pageBoundaryTouchDistance = 0;
  pageBoundaryTouchDirection = direction;
  pageBoundaryTouchDistance += Math.abs(distance);
}

function handlePageBoundaryTouchEnd() {
  if (pageBoundaryTouchTargetAllowed && pageBoundaryTouchDistance >= 48) {
    navigateAcrossPageBoundary(pageBoundaryTouchDirection);
  }
  pageBoundaryTouchY = null;
  pageBoundaryTouchDistance = 0;
  pageBoundaryTouchDirection = 0;
  pageBoundaryTouchTargetAllowed = false;
}

bottomPager.addEventListener('click', handlePagerClick);
bottomPager.addEventListener('change', handlePagerChange);
bottomPager.addEventListener('submit', handlePagerSubmit);
window.addEventListener('wheel', handlePageBoundaryWheel, { passive: false });
window.addEventListener('touchstart', handlePageBoundaryTouchStart, { passive: true });
window.addEventListener('touchmove', handlePageBoundaryTouchMove, { passive: true });
window.addEventListener('touchend', handlePageBoundaryTouchEnd, { passive: true });
function bindSearchField(input) {
  input.addEventListener('change', () => {
    const activatedFromHistory = activateSearchFromHistory({ resetMetaVisibility: true });
    currentPage = 1;
    syncSearchHashAndRender(!activatedFromHistory);
  });
}
for (const input of searchFields) bindSearchField(input);
window.addEventListener('hashchange', () => {
  const progressToken = pendingSidebarProgressToken;
  pendingSidebarProgressToken = null;
  const previousSelection = selected;
  selected = selectionFromHash();
  if (selected !== previousSelection || !selected.startsWith('__channel__:')) {
    channelDetailTab = selected.startsWith('__channel__:') && historyNavigationDate
      ? 'history'
      : 'playlists';
  }
  if (selected.startsWith('__playlist__:')) resetPlaylistVisibilityFor(selected.slice('__playlist__:'.length));
  if (selected === '__history__') search.value = '';
  renderGroups();
  void render().finally(() => {
    if (progressToken !== null) {
      finishSidebarNavigationProgress(progressToken);
    } else if (selected !== '__search__') {
      stopSearchHeaderProgress();
    }
  });
});
refresh.addEventListener('click', () => {
  const preserveSearchContent = (
    selected === '__search__'
    && searchResultsRendered
    && renderedOmniSearchQuery === search.value.trim().toLowerCase()
  );
  if (preserveSearchContent) showSearchHeaderProgress();
  loadData({ preserveSearchContent }).catch(error => {
    meta.textContent = error.message;
    refresh.disabled = false;
    refresh.textContent = 'Refresh';
  }).finally(stopSearchHeaderProgress);
});
renderSearchMetaFilters();
loadData().catch(error => {
  title.textContent = 'Unable to load data';
  meta.textContent = error.message;
  refresh.disabled = false;
  refresh.textContent = 'Refresh';
});
