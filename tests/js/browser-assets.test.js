const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const templateDirectory = path.join(process.cwd(), 'yt_library', 'templates');
const assetNames = [
  'theme.js',
  'timezone.js',
  'video-card.js',
  'collection-card.js',
  'entity-card-extensions.js',
  'history-workflow.js',
  'index.js',
  'admin-transport.js',
  'admin.js',
];

function source(name) {
  return fs.readFileSync(path.join(templateDirectory, name), 'utf8');
}

function timezoneHelpers(displayTimezone) {
  const context = {
    CustomEvent: class CustomEvent {},
    Date,
    Intl,
    URLSearchParams,
    console,
    window: {
      YT_LIBRARY_CONFIG: { displayTimezone },
      dispatchEvent() {},
    },
  };
  vm.runInNewContext(source('timezone.js'), context, { filename: 'timezone.js' });
  return context.window.YTLibraryTime;
}

function videoCardHelpers() {
  const context = { console, window: {} };
  vm.runInNewContext(source('video-card.js'), context, { filename: 'video-card.js' });
  return context.window.YTLibraryVideoCard;
}

test('all served browser assets have valid JavaScript syntax', () => {
  for (const name of assetNames) {
    assert.doesNotThrow(
      () => new vm.Script(source(name), { filename: name }),
      `${name} must parse`,
    );
  }
});

test('video cards render raw YouTube reaction statuses', () => {
  const helpers = videoCardHelpers();

  assert.equal(helpers.reactionLabel({ reaction: 'LIKE' }), 'Liked');
  assert.equal(helpers.reactionLabel({ reaction: 'DISLIKE' }), 'Disliked');
  assert.equal(helpers.reactionLabel({ reaction: 'INDIFFERENT' }), '');
  assert.match(helpers.reactionIconsHtml({ reaction: 'LIKE' }), /reaction-icon like active/);
  assert.match(helpers.reactionIconsHtml({ reaction: 'DISLIKE' }), /reaction-icon dislike active/);
  assert.doesNotMatch(helpers.reactionIconsHtml({ reaction: 'INDIFFERENT' }), / active/);
});

test('timezone reset persists the detected zone in one request', () => {
  const timezoneSource = source('timezone.js');

  assert.match(
    timezoneSource,
    /async reset\(\) \{\s*return persist\(detected\(\)\);\s*\}/,
  );
  assert.doesNotMatch(timezoneSource, /method: 'DELETE'/);
});

test('detail navigation retains the active search state', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /let retainedSearchUrl = loadRetainedSearchUrl\(\);/);
  assert.match(indexSource, /window\.sessionStorage\.getItem\('yt-library-retained-search-url'\)/);
  assert.match(indexSource, /window\.sessionStorage\.setItem\('yt-library-retained-search-url', href\)/);
  assert.match(indexSource, /rememberSearchUrl\(currentBrowserUrl\(\)\)/);
  assert.match(indexSource, /setBrowserUrl\(retainedSearchUrl \|\| '\/search'\)/);
  assert.match(
    indexSource,
    /searchNav\?\.addEventListener\('click', event => \{[\s\S]{0,120}handleSidebarLinkClick\(event, activateSearchNavigation\)/,
  );
  assert.doesNotMatch(
    indexSource,
    /if \(selected !== '__search__'\) search\.value = '';/,
  );
});

test('history hides search facets until the search box activates search', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexHtml, /\.filters\[hidden\] \{ display: none; \}/);
  assert.match(
    indexSource,
    /function syncSearchFiltersForSelection\(\)[\s\S]{0,180}searchFilters\.hidden = historySelected/,
  );
  assert.match(
    indexSource,
    /function activateSearchFromSelection[\s\S]{0,700}searchFilters\.hidden = false/,
  );
});

test('sidebar navigation uses real links without intercepting modified clicks', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexHtml, /<a id="history-nav" class="group" href="\/history"/);
  assert.match(indexHtml, /<a id="search-nav" class="group search-nav" href="\/search"/);
  assert.match(indexSource, /function searchPresetHref\(preset, groupKey = ''\)/);
  assert.match(indexSource, /function handleSidebarLinkClick\(event, navigate\)[\s\S]{0,240}event\.ctrlKey[\s\S]{0,120}event\.preventDefault\(\)/);
  assert.match(indexSource, /function groupLinkFor[\s\S]{0,160}document\.createElement\('a'\)/);
  assert.match(indexSource, /function presetLink[\s\S]{0,160}document\.createElement\('a'\)/);
  assert.match(indexSource, /link\.href = searchPresetHref\(preset/);
});

test('browser routes use path scope as the authoritative search context', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /const scopeByPath = \{[\s\S]*?'\/videos': 'videos'[\s\S]*?'\/clips': 'clips'[\s\S]*?'\/playlists': 'playlists'[\s\S]*?'\/channels': 'channels'/);
  assert.match(indexSource, /const contextKind = searchContextKind\(\)[\s\S]{0,120}selectedKinds\.filter\(kind => kind === contextKind\)/);
  assert.match(indexSource, /const base = scope \? `\/\$\{scope\}` : '\/search'/);
  assert.match(indexSource, /if \(contextKind && kind !== contextKind\) return '';/);
  assert.match(indexSource, /data-search-filter-section="\$\{escapeHtml\(kind\)\}"/);
  assert.match(indexSource, /window\.addEventListener\('popstate', handleBrowserLocationChange\)/);
  assert.doesNotMatch(indexSource, /window\.location\.hash|addEventListener\('hashchange'/);
});

test('entity details enter their scoped category context', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function selectedEntityCategory\(\)[\s\S]{0,320}'__video__:'[\s\S]*?'videos'[\s\S]*?'__clip__:'[\s\S]*?'clips'[\s\S]*?'__playlist__:'[\s\S]*?'playlists'[\s\S]*?'__channel__:'[\s\S]*?'channels'/);
  assert.match(indexSource, /function searchContextKind\(\)[\s\S]{0,180}selected === '__search__' \? activeSearchScope : selectedEntityCategory\(\)/);
  assert.match(indexSource, /function activeSidebarCategory\(\)[\s\S]{0,160}return selectedEntityCategory\(\)/);
  assert.match(indexSource, /function activateSearchFromSelection[\s\S]{0,240}const scope = searchContextKind\(\)[\s\S]{0,160}activeSearchScope = scope/);
  assert.match(indexSource, /search\.placeholder = selected\.startsWith\('__playlist__:'\)[\s\S]{0,120}placeholders\[contextKind\]/);
  assert.match(indexSource, /async function fetchEntitySearchFilters\(category, entityId\)[\s\S]{0,480}q: entityId[\s\S]{0,220}limit: '1'/);
  assert.match(indexSource, /function hydrateEntitySearchFilters\(category, entityId, generation\)[\s\S]{0,300}fetchEntitySearchFilters\(category, entityId\)[\s\S]{0,180}renderSearchMetaFilters\(payload\)/);
  assert.match(indexSource, /hydrateEntitySearchFilters\('videos', video\.video_id \|\| videoId, generation\)/);
  assert.match(indexSource, /hydrateEntitySearchFilters\('clips', clip\.clip_id \|\| clipId, generation\)/);
  assert.match(indexSource, /hydrateEntitySearchFilters\('channels', channelId, generation\)/);
});

test('playlist detail reuses the sidebar video search facets', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /search\.placeholder = selected\.startsWith\('__playlist__:'\)[\s\S]{0,100}'Search this playlist'/);
  assert.match(indexSource, /function searchContextKind\(\)[\s\S]{0,100}selected\.startsWith\('__playlist__:'\)[\s\S]{0,40}'videos'/);
  assert.match(indexSource, /fetchVideoCollection\(\{[\s\S]{0,500}useSearchFacets: true/);
  assert.match(indexSource, /reactionCounts: payload\.reactionCounts/);
  assert.match(indexSource, /uploaderCategoryCounts: payload\.uploaderCategoryCounts/);
  assert.doesNotMatch(indexSource, /function playlistVideoFiltersHtml/);
});

test('internal channel links prefer aliases while channel queries use canonical ids', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /video\.metadata_channel_reference \|\| channelId/);
  assert.match(indexSource, /playlist\.owner_channel_reference \|\| playlist\.owner_channel_id/);
  assert.match(indexSource, /channel\.preferred_reference \|\| channel\.channel_id/);
  assert.match(indexSource, /replace\(\/%40\/gi, '@'\)/);
  assert.match(indexSource, /fetchViewData\(`\/api\/channels\/\$\{encodeChannelReference\(channelReference\)\}`\)/);
  assert.match(indexSource, /const channelId = channel\.channel_id \|\| channelReference/);
});

test('playlist unavailable and removed filters are persisted opt-ins', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /unavailable: filterPreferenceEnabled\(filterPreferenceKeys\.unavailablePlaylistVideos\)/);
  assert.match(indexSource, /removed: filterPreferenceEnabled\(filterPreferenceKeys\.removedPlaylistVideos\)/);
  assert.match(indexSource, /function savePlaylistVideoOptInPreferences\(\)[\s\S]{0,180}playlistVideoOptInFilters/);
  assert.match(indexSource, /selected\.startsWith\('__playlist__:'\) && groupName === 'videos'[\s\S]{0,160}savePlaylistVideoOptInPreferences\(\)/);
});

test('playlist search groups unavailable under availability without a status facet', () => {
  const indexSource = source('index.js');
  const availabilityStart = indexSource.indexOf('const playlistAvailabilityMetaFilterDefinitions');
  const ownershipStart = indexSource.indexOf('const playlistOwnershipMetaFilterDefinitions');
  const availabilitySource = indexSource.slice(availabilityStart, ownershipStart);

  assert.ok(availabilityStart >= 0);
  assert.ok(availabilitySource.indexOf("key: 'unavailable'") < availabilitySource.indexOf("key: 'unknown'"));
  assert.match(indexSource, /key: 'playlistAvailability'[\s\S]{0,220}allLabel: 'Availability'/);
  assert.doesNotMatch(indexSource, /key: 'playlistStatus'/);
  assert.doesNotMatch(indexSource, /allLabel: 'Status', kind: 'playlists'/);
});

test('playlist cards show visibility before counts and pluralize video counts', () => {
  const indexSource = source('index.js');
  const cardSource = indexSource.slice(
    indexSource.indexOf('function cardFor('),
    indexSource.indexOf('function playlistStatusLabelHtml('),
  );

  assert.ok(
    cardSource.indexOf('playlistVisibilityLabelHtml(playlist)')
      < cardSource.indexOf('playlistCount ?'),
  );
  assert.match(indexSource, /count === 1 \? 'video' : 'videos'/);
});

test('compact playlist cards hide playlist ids without removing them from other layouts', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /class="playlist-id">\$\{escapeHtml\(playlist\.playlist_id\)\}/);
  assert.match(indexHtml, /\.search-grid\.layout-compact \.playlist-id,/);
});

test('compact channel cards retain only a right-aligned YouTube link', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');
  const channelCardSource = indexSource.slice(
    indexSource.indexOf('function channelCardFor('),
    indexSource.indexOf('function externalLinkSvg('),
  );

  assert.match(channelCardSource, /className: 'channel-card'/);
  assert.match(channelCardSource, /class="channel-id">\$\{escapeHtml\(channel\.channel_id\)\}/);
  assert.match(channelCardSource, /class="channel-archivarix-id">Archivarix/);
  assert.match(channelCardSource, /class="playlist-link channel-youtube-link"/);
  assert.match(channelCardSource, /class="playlist-link channel-archivarix-link"/);
  assert.match(indexHtml, /\.search-grid\.layout-compact \.channel-id,/);
  assert.match(indexHtml, /\.search-grid\.layout-compact \.channel-archivarix-link \{ display: none; \}/);
  assert.match(indexHtml, /\.search-grid\.layout-compact \.channel-card-links \{ justify-content: flex-end; \}/);
});

test('playlist cards render one owner separately from collaborators', () => {
  const indexSource = source('index.js');
  const videoCardSource = source('video-card.js');

  assert.match(indexSource, /const people = \[owner, \.\.\.collaborators\]/);
  assert.match(indexSource, /names \+= ` and \$\{playlistPersonNameHtml\(collaborators\[0\]\)\}`/);
  assert.match(indexSource, /names \+= ` and \$\{collaborators\.length\} others`/);
  assert.match(indexSource, /playlist-creator-avatars/);
  assert.match(
    videoCardSource,
    /value\.startsWith\('\/'\) \|\| value\.startsWith\('#'\)/,
  );
  assert.match(indexSource, /return linkTargetAttributes\(href\);/);
});

test('admin status polling clears stale running state on request failures', () => {
  const adminSource = source('admin.js');

  assert.match(adminSource, /const statusRequestTimeoutMs = 5000;/);
  assert.match(adminSource, /function renderServiceUnavailable\(error\)/);
  assert.match(adminSource, /fields\.serviceStatus\.textContent = 'Unavailable';/);
  assert.match(adminSource, /renderServiceUnavailable\(statusError\);/);
});

test('admin log level selector uses cumulative verbosity with error as the default', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.doesNotMatch(adminHtml, /All levels/);
  assert.match(adminHtml, /<option value="error" selected>Error<\/option>/);
  assert.match(adminSource, /logSeverityRanks = Object\.freeze\(\{ info: 0, warn: 1, error: 2, debug: 3 \}\)/);
  assert.match(adminSource, /return logRank <= selectedRank;/);
  assert.match(adminSource, /fields\.logLevelFilter\.value \|\| 'error'/);
});

test('admin renders generic placed plugin process inputs', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(adminHtml, /id="videoPluginProcesses"/);
  assert.match(adminSource, /process\.adminActions \|\| \[\]/);
  assert.match(adminSource, /action\.placement === 'videos'/);
  assert.match(adminSource, /data-plugin-param=/);
  assert.match(adminSource, /params\[input\.dataset\.pluginParam\] = value/);
  assert.match(adminSource, /videoPluginProcesses\.addEventListener\('submit', enqueuePluginProcess\)/);
});

test('advanced admin renders a generic persisted plugin enabled slider', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(adminHtml, /\.plugin-enabled-control/);
  assert.match(adminHtml, /<section id="pluginPanel" class="panel plugins-panel advanced-only">/);
  assert.match(adminHtml, /<div class="queue-title"><h2>Plugins<\/h2><\/div>/);
  assert.match(adminSource, /function pluginEnabledControlHtml\(plugin\)/);
  assert.match(adminSource, /<span>Enabled<\/span>/);
  assert.match(adminSource, /class="plugin-enabled-toggle" type="checkbox"/);
  assert.match(adminSource, /\$\{plugin\.enabled \? 'checked' : ''\}/);
  assert.match(adminSource, /\/api\/admin\/plugins\/\$\{encodeURIComponent\(pluginId\)\}\/enabled/);
  assert.match(adminSource, /pluginWorkstreams\.addEventListener\('change', savePluginEnabled\)/);
  assert.match(adminSource, /<div class="plugin-workstream-header">/);
  assert.match(adminSource, /fields\.pluginPanel\.classList\.toggle\('advanced-only', !hasBasicPlugin\)/);
});

test('history views render shared day dividers', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /function historyDayLabel\(video\)/);
  assert.match(indexSource, /const options = \{ weekday: 'short' \}/);
  assert.match(indexSource, /return `\$\{weekday\}, \$\{dateLabel\}`/);
  assert.match(indexSource, /function historyRowsWithDayDividers\(rows, options = \{\}\)/);
  assert.equal((indexSource.match(/historyRowsWithDayDividers\(rows/g) || []).length, 2);
  assert.match(indexSource, /async function renderHistoryResults\(options\)[\s\S]{0,1600}historyRowsWithDayDividers\(rows/);
  assert.match(indexSource, /async function renderHistoryView\(generation\)[\s\S]{0,500}renderHistoryResults\(\{/);
  assert.match(indexSource, /const layoutContext = 'channel-history'[\s\S]{0,500}renderHistoryResults\(\{/);
  assert.match(indexSource, /divider\.dataset\.historyDate = date/);
  assert.match(indexSource, /for \(const value of \[row\?\.watched_at, row\?\.watch_date\]\)/);
  assert.match(indexSource, /const watchDate = historyRowDateKey\(video\)/);
  assert.match(indexSource, /const target = divider instanceof HTMLElement \? divider : row/);
  assert.match(indexHtml, /\.history-day-divider/);
});

test('history day keys use the configured display timezone', () => {
  const time = timezoneHelpers('America/Los_Angeles');

  assert.equal(time.dateKey('2026-08-01T06:59:45.594617Z'), '2026-07-31');
  assert.equal(time.dateKey('2026-08-01'), '2026-08-01');
});

test('history heatmaps honor the configured week start and label the top row', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(indexSource, /const historyWeekStart = pageConfig\.weekStart === 'monday' \? 'monday' : 'sunday'/);
  assert.match(indexSource, /const daysSinceWeekStart = \(start\.getDay\(\) - historyWeekStartDay \+ 7\) % 7/);
  assert.match(indexSource, /weekStartLabel\.textContent = historyWeekStart === 'monday' \? 'Mon' : 'Sun'/);
  assert.match(indexHtml, /\.history-heatmap-week-start/);
  assert.match(adminHtml, /<legend>Week start<\/legend>/);
  assert.match(adminHtml, /name="weekStart" value="sunday" checked/);
  assert.match(adminHtml, /name="weekStart" value="monday"/);
  assert.match(adminSource, /week_start: fields\.weekStartMonday\.checked \? 'monday' : 'sunday'/);
});

test('zero-count filters use the config-backed shared visibility path', () => {
  const indexSource = source('index.js');
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(indexSource, /const hideEmptyFilters = pageConfig\.hideEmptyFilters !== false/);
  assert.match(indexSource, /function visibleMetaFilterDefinitions\(visibility, counts, definitions\)/);
  assert.match(indexSource, /!hideEmptyFilters \|\| counts === null \|\| counts === undefined \|\| metaFilterCount\(counts, key\) !== 0/);
  assert.match(adminHtml, /id="hideEmptyFilters" type="checkbox" checked>Hide empty filters/);
  assert.match(adminSource, /AdminTransport\.postJson\('\/api\/settings\/hide-empty-filters'/);
  assert.match(
    adminSource,
    /fields\.hideEmptyFilters\.addEventListener\('change',[\s\S]{0,180}saveHideEmptyFilters\(\)/,
  );
  assert.doesNotMatch(
    adminSource,
    /AdminTransport\.postJson\('\/api\/admin\/settings',[\s\S]{0,300}hide_empty_filters:/,
  );
});

test('search filter bootstrap defers leaves until facet counts arrive', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /const countsReady = counts !== null && counts !== undefined/);
  assert.match(indexSource, /\$\{countsReady \? metaFilterChildrenHtml\(\{/);
  assert.match(indexSource, /\$\{expanded && countsReady \? '' : 'hidden'\}/);
  assert.match(indexSource, /const countsPending = metaCounts === null \|\| metaCounts === undefined/);
  assert.match(indexSource, /syncMetaFilterGroup\(`search-\$\{key\}`, countsPending\)/);
  assert.match(indexSource, /syncSearchKindFilter\(kind, true, countsPending\)/);
  assert.match(indexSource, /'search-clipOwnership': searchMetaVisibility\.clipOwnership/);
});

test('admin parameter posts use the shared transport with caller-owned effects', () => {
  const adminSource = source('admin.js');

  assert.match(adminSource, /const AdminTransport = window\.YTLibraryAdminTransport;/);
  assert.doesNotMatch(adminSource, /async function requestJson\(/);
  assert.match(
    adminSource,
    /async function post\(path, params = \{\}\)[\s\S]{0,320}AdminTransport\.postJson\(path, params\)[\s\S]{0,180}loadStatus\(\{ force: true \}\)[\s\S]{0,100}scheduleActionPolls\(\)/,
  );
  assert.match(
    adminSource,
    /async function stopWorkersNow\(\)[\s\S]{0,400}AdminTransport\.postJson\('\/api\/admin\/queue\/stop'\)[\s\S]{0,100}scheduleActionPolls\(\)/,
  );
  assert.match(adminSource, /AdminTransport\.postJson\('\/api\/admin\/service\/restart'\)/);
  assert.equal((adminSource.match(/method: 'POST'/g) || []).length, 1);
});

test('history heatmaps track the first fully visible card', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /function firstVisibleHistoryCardDate\(\)[\s\S]*?bounds\.top >= viewport\.top && bounds\.bottom <= viewport\.bottom/);
  assert.match(indexSource, /function updateHistoryHeatmapCurrentDay\(\)[\s\S]*?cell\.setAttribute\('aria-current', 'date'\)/);
  assert.match(indexSource, /resultsScroll\?\.addEventListener\('scroll', scheduleHistoryHeatmapCurrentDay/);
  assert.match(indexSource, /window\.addEventListener\('scroll', scheduleHistoryHeatmapCurrentDay/);
  assert.match(indexHtml, /button\.history-heatmap-day\[aria-current="date"\]/);
});

test('history year navigation keeps the selected month and day', () => {
  const indexSource = source('index.js');

  assert.match(
    indexSource,
    /function displayedHistoryAnchorDate\(\)[\s\S]{0,420}history-heatmap-day\[aria-current="date"\]\[data-history-date\][\s\S]{0,220}selectedDay\.dataset\.historyDate/,
  );
  assert.match(
    indexSource,
    /function displayedHistoryAnchorDate\(\)[\s\S]{0,650}firstVisibleHistoryCardDate\(\)[\s\S]{0,180}historyNavigationDate/,
  );
  assert.match(
    indexSource,
    /async function shiftHistoryActivityYear\(delta\)[\s\S]{0,700}const currentAnchorDate = displayedHistoryAnchorDate\(\)[\s\S]{0,800}shiftedHistoryDateKey\(currentAnchorDate, delta\)/,
  );
});

test('history heatmap changes share one rollback-safe transition path', () => {
  const indexSource = source('index.js');
  const workflowSource = source('history-workflow.js');

  assert.equal(
    (indexSource.match(/runHistoryHeatmapTransition\(/g) || []).length,
    4,
  );
  assert.match(
    indexSource,
    /function historyTransitionState\(\)[\s\S]{0,400}navigationDate: historyNavigationDate/,
  );
  assert.match(
    indexSource,
    /function restoreHistoryTransitionState\(snapshot\)[\s\S]{0,300}historyNavigationDate = snapshot\.navigationDate/,
  );
  assert.match(workflowSource, /if \(!isCurrent\(\) \|\| !heatmap\.isConnected\) \{\s*restore\(\)/);
  assert.match(workflowSource, /catch \(error\) \{\s*restore\(\)/);
  assert.doesNotMatch(indexSource, /heatmap\.setAttribute\('aria-busy', 'true'\)/);
});

test('histogram navigation uses a restorable date URL', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /params\.set\('date', historyNavigationDate\)/);
  assert.match(indexSource, /async function fetchHistoryLocation\(channelId = ''\)/);
  assert.match(indexSource, /day => day\.watch_date === historyNavigationDate/);
  assert.match(indexSource, /const direction = target\.dataset\.page[\s\S]{0,200}historyNavigationDate = '';/);
});

test('numbered browser pages navigate across scroll boundaries', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /let renderedPageInfo = \{ page: 1, pageCount: 1, total: 0 \}/);
  assert.match(indexSource, /function navigateAcrossPageBoundary\(direction\)/);
  assert.match(indexSource, /pendingPageBoundaryLanding = direction > 0 \? 'top' : 'bottom'/);
  assert.match(indexSource, /window\.addEventListener\('wheel', handlePageBoundaryWheel, \{ passive: false \}\)/);
  assert.match(indexSource, /window\.addEventListener\('touchend', handlePageBoundaryTouchEnd, \{ passive: true \}\)/);
});

test('numbered browser pages cache and prefetch adjacent payloads', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function cachedRequest\(cache, key, load, maxEntries\)/);
  assert.match(indexSource, /function scheduleAdjacentPagePrefetch\(pageInfo, fetchPage, additionalRequests = \[\]\)/);
  assert.match(indexSource, /const pages = \[page \+ 1, page - 1\]/);
  assert.match(indexSource, /Promise\.all\(\[[\s\S]*runRequests\(additionalRequests\)[\s\S]*runRequests\(pageRequests\)/);
  assert.match(indexSource, /window\.setTimeout\(\(\) => void run\(\), 150\)/);
  assert.match(indexSource, /async function fetchHistoryPage\(channelId = '', page = currentPage\)/);
  assert.match(indexSource, /function historyYearPagePrefetches\(channelId, rows\)/);
  assert.match(indexSource, /const shifts = historyActivityYearOffset > 0 \? \[1, -1\] : \[1\]/);
  assert.match(indexSource, /async function fetchOmniSearch\(query, page = currentPage\)/);
  assert.match(indexSource, /fetchVideoCollection\(\{[\s\S]*page = currentPage,/);
});

test('search filters share deferred category dimming until refreshed results render', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function setSearchKindFilter\(kind, checked\)/);
  assert.match(indexSource, /function syncSearchKindFilter\(kind, applyDisabledStyles = true, assumeAllChecked = false\)/);
  assert.match(indexSource, /if \(applyDisabledStyles\) \{[\s\S]{0,300}row\.classList\.toggle\('dimmed'/);
  assert.match(indexSource, /function refreshSearchAfterFilterChange\(groupName, activatedFromSelection\)/);
  assert.match(indexSource, /refreshSearchAfterFilterChange[\s\S]{0,200}syncSearchKindFilter\(searchKindForFacet\(groupName\), false\)/);
  assert.match(indexSource, /setSearchKindFilter\(searchKindFilter, target\.checked\)[\s\S]{0,800}refreshSearchAfterFilterChange\(searchKindFilter, activatedFromSelection\)/);
  assert.match(indexSource, /syncMetaFilterGroup\(`search-\$\{groupName\}`\);[\s\S]{0,180}refreshSearchAfterFilterChange\(groupName, activatedFromSelection\)/);
  assert.match(indexSource, /function renderSearchMetaFilters[\s\S]*?for \(const kind of \[[\s\S]*?syncSearchKindFilter\(kind, true, countsPending\)/);
});

test('single-facet result kinds use the same parent state calculation', () => {
  const indexSource = source('index.js');
  const start = indexSource.indexOf('function syncSearchKindFilter(');
  const end = indexSource.indexOf('\nfunction searchKindForFacet(', start);
  const functionSource = indexSource.slice(start, end);

  assert.match(functionSource, /const facetSelections = facetKeys\.map/);
  assert.match(functionSource, /parent\.checked = everyFacetHasSelection/);
  assert.match(functionSource, /parent\.indeterminate = everyFacetHasSelection && !allChildrenSelected/);
  assert.doesNotMatch(functionSource, /facetKeys\.length > 1/);
  assert.doesNotMatch(functionSource, /data-meta-child-filter="search-\$\{kind\}"/);
  assert.match(
    indexSource,
    /for \(const kind of \[[\s\S]{0,100}'videos',[\s\S]{0,40}'clips',[\s\S]{0,40}'playlists',[\s\S]{0,40}'channels'/,
  );
});

test('video and clip categories restore plugin-provided facets', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function enableDefaultSearchKind\(kind\)[\s\S]*kind === 'videos'[\s\S]*browserVideoFilterPlugins\(\)[\s\S]*kind === 'clips'[\s\S]*browserClipFilterPlugins\(\)/);
  assert.match(indexSource, /const state = kind === 'videos'[\s\S]*browserVideoFacetState\(filterPlugin\)[\s\S]*browserClipFacetState\(filterPlugin\)/);
  assert.match(indexSource, /const defaults = kind === 'videos'[\s\S]*defaultBrowserVideoFacetVisibility\(filterPlugin\)[\s\S]*defaultBrowserClipFacetVisibility\(filterPlugin\)/);
});

test('search URLs omit state already implied by their scoped route', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function searchMetaPresetBaseline\(groupName, preset = activeSearchPreset\)/);
  assert.match(indexSource, /function metaFilterSelectionMatches\(visibility, baseline, excludedKeys = \[\]\)/);
  assert.match(indexSource, /const baseline = searchMetaPresetBaseline\(groupName\);[\s\S]{0,120}metaFilterSelectionMatches\(visibility, baseline, optInKeys\)/);
  assert.match(indexSource, /function browserVideoFacetPresetBaseline\(plugin, preset = activeSearchPreset\)/);
  assert.match(indexSource, /state\.present !== baseline\.present/);
  assert.match(indexSource, /state\.absent !== baseline\.absent/);
  assert.match(indexSource, /const enableDetectedCategories = \([\s\S]{0,180}Object\.values\(searchMetaVisibility\.uploaderCategory\)\.some\(Boolean\)/);
  assert.match(indexSource, /searchMetaVisibility\.uploaderCategory\[key\] = enableDetectedCategories/);
});

test('channel group navigation is recursive and uses the generic search contract', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /let channelMemberships = new Map\(\);/);
  assert.match(indexSource, /for \(const item of data\.channelMemberships \|\| \[\]\)/);
  assert.match(indexSource, /'channel-group': \{ kind: 'channels', sort: 'title' \}/);
  assert.match(indexSource, /channel_group_key: searchChannelGroupKey/);
  assert.match(indexSource, /function appendGroupTree\([\s\S]*appendGroupTree\(/);
  assert.match(indexSource, /appendGroupTree\([\s\S]*'channel-group'[\s\S]*channelMemberships[\s\S]*channelChildren/);
  assert.match(indexSource, /while \(pending\.length\)[\s\S]*identifiers\.add\(identifier\)/);
  assert.match(indexHtml, /--group-depth/);
});

test('navigation group branches collapse with persisted disclosure state', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /const navigationGroupTreeCollapsed = new Set\(/);
  assert.match(indexSource, /toggle\.dataset\.groupTreeToggle = nodeId/);
  assert.match(indexSource, /aria-controls/);
  assert.match(indexSource, /childContainer\.hidden = !expanded/);
  assert.match(indexSource, /function toggleNavigationGroupTreeNode\(/);
  assert.match(indexSource, /\/api\/settings\/navigation-group-tree/);
  assert.match(indexSource, /navigationGroupTreeSaveChain\.catch\(\(\) => \{\}\)\.then\(save\)/);
  assert.match(indexHtml, /\.group-tree-children\[hidden\] \{ display: none; \}/);
  assert.match(indexHtml, /\.search-tree-toggle\[aria-expanded="true"\] svg/);
});

test('plugin navigation groups render beneath count-free persisted parents', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /function appendNavigationGroupTrees\(/);
  assert.match(indexSource, /String\(group\.source_plugin_id \|\| ''\)\.trim\(\)/);
  assert.match(indexSource, /function appendPluginGroupTree\(/);
  assert.match(indexSource, /`plugin-root:\$\{pluginId\}`/);
  assert.match(indexSource, /replace\(\/\^YT\\s\+\/i, ''\)/);
  assert.match(indexSource, /labelNode\.className = 'group group-tree-label'/);
  assert.match(indexSource, /labelNode\.textContent = label/);
  assert.match(indexSource, /appendNavigationGroupTrees\([\s\S]*'playlist-group'/);
  assert.match(indexSource, /appendNavigationGroupTrees\([\s\S]*'channel-group'/);
  assert.match(indexHtml, /\.group-tree-label \{ cursor: default; font-weight: 600; \}/);
});

test('search filter tree folds facets and persists disclosure state', () => {
  const indexHtml = source('index.html');
  const indexSource = source('index.js');

  assert.match(indexHtml, /\.app \{[^}]*grid-template-columns: 300px minmax\(0, 1fr\)/);
  assert.match(indexHtml, /\.search-tree-toggle[\s\S]*transition: transform 160ms ease/);
  assert.match(indexHtml, /\.search-tree-toggle\[aria-expanded="true"\][\s\S]*rotate\(90deg\)/);
  assert.match(indexHtml, /\.meta-filter input \{ accent-color: var\(--accent\); margin: 0; \}/);
  assert.match(indexHtml, /\.search-meta-facet-children \{[\s\S]*grid-template-columns: 14px minmax\(0, 1fr\)/);
  assert.match(indexHtml, /\.search-meta-facet > \.meta-filter \{ margin-left: 0; \}/);
  assert.match(indexHtml, /\.filter-parent-checkbox > input:indeterminate \+ \.filter-parent-checkbox-indicator \{ display: inline-flex; \}/);
  assert.match(indexHtml, /\.filter-parent-checkbox-indicator svg \{[^}]*stroke-width: 2\.5;/);
  assert.match(indexSource, /function parentFilterCheckboxHtml[\s\S]*M2\.75 6\.5h7\.5M6\.5 2\.75v7\.5/);
  assert.match(indexSource, /defaultSearchFilterTreeExpanded = \[[\s\S]*'kind:videos'[\s\S]*'kind:playlists'[\s\S]*'kind:channels'/);
  assert.match(indexSource, /data-search-tree-toggle="\$\{escapeHtml\(nodeId\)\}"/);
  assert.match(indexSource, /class="search-meta-facet-children"[\s\S]*\$\{expanded && countsReady \? '' : 'hidden'\}[\s\S]*class="search-tree-toggle-spacer"[\s\S]*class="search-meta-controls"/);
  assert.match(indexSource, /\/api\/settings\/search-filter-tree/);
});

test('uploader category facet requires detected categories', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function uploaderCategoryMetaFilterDefinitions\(counts\)[\s\S]*?if \(!categories\.length\) return \[\]/);
  assert.match(indexSource, /\.\.\.\(uploaderCategoryDefinitions\.length \? \[[\s\S]*?allLabel: 'Uploader category'/);
});

test('sidebar keeps facet trees separate from category navigation', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexHtml, /id="search-for-filters" class="search-for-filters"/);
  assert.match(indexSource, /const searchFilterTree = document\.getElementById\('search-for-filters'\)/);
  assert.match(indexSource, /const searchFilterRegion = groupsEl\.parentElement/);
  assert.match(indexSource, /function appendSearchFilterCategory[\s\S]*if \(contextKind === kind\) \{[\s\S]*container\.appendChild\(searchFilterSlot\(kind\)\)/);
  assert.match(indexSource, /function searchFilterMount\(kind, navigationSection\)[\s\S]*searchContextKind\(\) === kind \? navigationSection : searchFilterTree/);
  assert.match(indexSource, /videoSection\.appendChild\(presetLink\('videos', 'Videos',/);
  assert.match(indexSource, /searchFilterMount\('videos', videoSection\)/);
  assert.match(indexSource, /searchFilterMount\('clips', videoSection\)/);
  assert.match(indexSource, /playlistSection\.appendChild\(presetLink\('playlists', 'Playlists',/);
  assert.match(indexSource, /searchFilterMount\('playlists', playlistSection\)/);
  assert.match(indexSource, /channelSection\.appendChild\(presetLink\('channels', 'Channels',/);
  assert.match(indexSource, /searchFilterMount\('channels', channelSection\)/);
  assert.match(indexSource, /data-search-filter-slot/);
  assert.doesNotMatch(indexHtml, />Search for</);
  assert.doesNotMatch(indexSource, /presetLink\('(playlisted|liked|subscribed|terminated)'/);
  assert.doesNotMatch(indexSource, /activeSearchPreset === '(playlisted|liked|subscribed|terminated)'/);
  assert.doesNotMatch(indexSource, /browserSearchPresets/);
  assert.doesNotMatch(indexSource, /all-playlists/);
  assert.match(indexSource, /const invalidPreset = Boolean\([\s\S]{0,220}!requestedDefinition/);
  assert.match(indexSource, /if \(pathname === '\/' \|\| invalidPreset\) updateSearchUrl\(true\)/);
});

test('search and playlist video sorts include both title directions', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /const searchSortOptions = new Set\(\[[\s\S]{0,180}'title_desc'/);
  assert.match(indexSource, /function searchResultsSortHtml\(\)[\s\S]{0,320}\['title', 'Title A-Z'\][\s\S]{0,80}\['title_desc', 'Title Z-A'\]/);
  assert.match(indexSource, /function videoSortHtml\(value, scope\)[\s\S]{0,260}\['title', 'Title A-Z'\][\s\S]{0,80}\['title_desc', 'Title Z-A'\]/);
  assert.doesNotMatch(indexSource, /browserPluginForcesRelevance|forceRelevance \?/);
});

test('history heatmap can return to the current year and day', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /current\.dataset\.historyCurrent = ''/);
  assert.match(indexSource, /current\.title = 'Today'/);
  assert.match(indexSource, /current\.setAttribute\('aria-label', 'Today'\)/);
  assert.match(indexSource, /current\.innerHTML = historyHeatmapNavigationIcon/);
  assert.match(indexSource, /async function jumpToCurrentHistoryActivity\(\)/);
  assert.match(indexSource, /historyActivityYearOffset = 0/);
  assert.match(
    indexSource,
    /async function jumpToCurrentHistoryActivity\(\)[\s\S]{0,420}selected === '__history__'[\s\S]{0,220}historyNavigationDate = '';[\s\S]{0,140}updateCurrentUrl\(false\)/,
  );
  assert.match(indexSource, /historyActivityDayNear\(activity, localDateKey\(new Date\(\)\)\)/);
  assert.match(indexSource, /current\.disabled = historyActivityYearOffset === 0 && currentPage === 1/);
  assert.match(indexSource, /function restoreHistoryNavigationButtons\(container\)/);
});

test('history document title includes the active page or date', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /historyNavigationDate[\s\S]{0,120}historyDayLabel\(\{ watch_date: historyNavigationDate \}\)/);
  assert.match(indexSource, /`page \$\{pageInfo\.page\}`/);
  assert.match(indexSource, /setDocumentTitle\(`History \$\{historyTitleLocation\}`\)/);
});

test('channel history tabs persist in the URL from page one', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function channelDetailParams\(\)[\s\S]*?params\.set\('tab', 'history'\)[\s\S]*?params\.set\('page', String\(currentPage\)\)/);
  assert.match(indexSource, /function channelDetailTabFromParams\(params\)[\s\S]*?params\.get\('tab'\) === 'history'/);
  assert.match(indexSource, /channelDetailTab = channelDetailTabFromParams\(params\)/);
});

test('channel tabs use independent persisted card layouts', () => {
  const indexSource = source('index.js');

  assert.match(
    indexSource,
    /'channel-playlists': cardLayouts\.has\(pageConfig\.channelPlaylistCardLayout\)[\s\S]{0,120}: 'grid'/,
  );
  assert.match(
    indexSource,
    /'channel-history': cardLayouts\.has\(pageConfig\.channelHistoryCardLayout\)[\s\S]{0,120}: 'detailed'/,
  );
  assert.match(
    indexSource,
    /const layoutContext = 'channel-history'[\s\S]{0,500}renderHistoryResults\(\{[\s\S]{0,700}cardLayoutHtml\(cardLayoutFor\(layoutContext\), layoutContext\)[\s\S]{0,300}layoutContext/,
  );
  assert.match(
    indexSource,
    /const layoutContext = 'channel-playlists'[\s\S]{0,500}cardLayoutHtml\(cardLayoutFor\(layoutContext\), layoutContext\)[\s\S]{0,500}rows\.map\(playlistVideoCardFor\)/,
  );
  assert.doesNotMatch(
    indexSource,
    /grid\.className = channelDetailTab === 'history'/,
  );
});

test('card layout controls place compact beside grid', () => {
  const indexSource = source('index.js');
  const grid = indexSource.indexOf("['grid', 'Grid']");
  const compact = indexSource.indexOf("['compact', 'Compact list']");
  const detailed = indexSource.indexOf("['detailed', 'Detailed list']");

  assert.ok(grid >= 0);
  assert.ok(compact > grid);
  assert.ok(detailed > compact);
});

test('theme selection normalizes, persists, and publishes changes', () => {
  const stored = new Map([['yt-library-theme', 'light']]);
  const events = [];
  const context = {
    CustomEvent: class CustomEvent {
      constructor(type, options) {
        this.type = type;
        this.detail = options.detail;
      }
    },
    document: { documentElement: { dataset: {} } },
    window: {
      dispatchEvent: event => events.push(event),
      localStorage: {
        getItem: key => stored.get(key) || '',
        setItem: (key, value) => stored.set(key, value),
      },
    },
  };

  vm.runInNewContext(source('theme.js'), context, { filename: 'theme.js' });

  assert.equal(context.document.documentElement.dataset.theme, 'light');
  assert.equal(context.window.YTLibraryTheme.set('unsupported'), 'dark');
  assert.equal(stored.get('yt-library-theme'), 'dark');
  assert.equal(events.at(-1).type, 'ytlibrarythemechange');
  assert.equal(events.at(-1).detail.theme, 'dark');
});

test('video-card helpers escape markup and clamp watch progress', () => {
  const context = { document: {}, window: {} };
  vm.runInNewContext(source('video-card.js'), context, {
    filename: 'video-card.js',
  });
  const helpers = context.window.YTLibraryVideoCard;

  assert.equal(helpers.escapeHtml('<a href="x">&</a>'), '&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;');
  assert.equal(
    helpers.searchHighlight.textHtml('Reflecting & reflecting <b>', 'reflecting'),
    '<mark class="search-highlight">Reflecting</mark> &amp; <mark class="search-highlight">reflecting</mark> &lt;b&gt;',
  );
  assert.equal(
    helpers.searchHighlight.snippetHtml('before <mark>hit & <b></mark> <script>'),
    'before <mark class="search-highlight">hit &amp; &lt;b&gt;</mark> &lt;script&gt;',
  );
  assert.equal(
    helpers.searchHighlight.excerptHtml(
      'Discard these opening words because the reflecting match is later than the card can show safely.',
      'reflecting',
      { before: 12, after: 20 },
    ),
    '…the <mark class="search-highlight">reflecting</mark> match is later…',
  );
  assert.equal(
    helpers.searchHighlight.excerptHtml('No match & <b>', 'reflecting'),
    'No match &amp; &lt;b&gt;',
  );
  assert.equal(helpers.watchProgressPercent({ watch_progress_percent: 0.4 }), 1);
  assert.equal(helpers.watchProgressPercent({ watch_progress_percent: 104 }), 100);
  assert.equal(helpers.watchProgressPercent({ watch_progress_percent: 'bad' }), 0);
  assert.equal(
    helpers.compactWatchCountHtml({ watch_count: 1 }),
    '<span class="compact-watch-count"> · 1 watch</span>',
  );
  assert.equal(
    helpers.compactWatchCountHtml({ watch_count: 3 }),
    '<span class="compact-watch-count"> · 3 watches</span>',
  );
  assert.equal(helpers.compactWatchCountHtml({ watch_count: 0 }), '');
  assert.equal(
    helpers.watchedLineHtml({ watch_progress_percent: 25, watch_count: 2 }),
    '<div class="watched-line has-progress"><span class="watched-progress">Watched 25%</span><span class="watched-count"> · 2 watches</span></div>',
  );
  assert.equal(
    helpers.watchedLineHtml({ watch_count: 1 }),
    '<div class="watched-line"><span class="watched-count">Watched · 1 watch</span></div>',
  );
  assert.match(
    source('index.js'),
    /const compactWatchCountHtml = VideoCard\.compactWatchCountHtml;/,
  );
  assert.match(
    source('video-card.js'),
    /class="video-availability-row"[^`]+compactAvailabilityHtml/,
  );
  assert.match(
    source('index.js'),
    /class="compact-video-duration"/,
  );
});
