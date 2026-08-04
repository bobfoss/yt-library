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
  'index.js',
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

test('all served browser assets have valid JavaScript syntax', () => {
  for (const name of assetNames) {
    assert.doesNotThrow(
      () => new vm.Script(source(name), { filename: name }),
      `${name} must parse`,
    );
  }
});

test('detail navigation retains the active search state', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /let retainedSearchHash = '#search';/);
  assert.match(indexSource, /retainedSearchHash = hash;/);
  assert.match(indexSource, /window\.location\.hash = retainedSearchHash;/);
  assert.match(
    indexSource,
    /searchNav\?\.addEventListener\('click', activateSearchNavigation\);/,
  );
  assert.doesNotMatch(
    indexSource,
    /if \(selected !== '__search__'\) search\.value = '';/,
  );
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
  assert.match(indexSource, /playlistVideoOptInFilters\.find\(item => item\.key === filter\)/);
  assert.match(indexSource, /saveFilterPreference\(playlistFilter\.preferenceKey, target\.checked\)/);
  assert.match(indexSource, /metaAllFilter === 'playlist-videos'[\s\S]{0,100}savePlaylistVideoOptInPreferences\(\)/);
});

test('admin status polling clears stale running state on request failures', () => {
  const adminSource = source('admin.js');

  assert.match(adminSource, /const statusRequestTimeoutMs = 5000;/);
  assert.match(adminSource, /function renderServiceUnavailable\(error\)/);
  assert.match(adminSource, /fields\.serviceStatus\.textContent = 'Unavailable';/);
  assert.match(adminSource, /renderServiceUnavailable\(statusError\);/);
});

test('history views render shared day dividers', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /function historyDayLabel\(video\)/);
  assert.match(indexSource, /const options = \{ weekday: 'short' \}/);
  assert.match(indexSource, /return `\$\{weekday\}, \$\{dateLabel\}`/);
  assert.match(indexSource, /function historyRowsWithDayDividers\(rows, options = \{\}\)/);
  assert.equal((indexSource.match(/historyRowsWithDayDividers\(rows/g) || []).length, 3);
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
  assert.match(indexSource, /function syncSearchKindFilter\(kind, applyDisabledStyles = true\)/);
  assert.match(indexSource, /if \(applyDisabledStyles\) \{[\s\S]{0,300}row\.classList\.toggle\('dimmed'/);
  assert.match(indexSource, /function refreshSearchAfterFilterChange\(groupName, activatedFromHistory\)/);
  assert.match(indexSource, /refreshSearchAfterFilterChange[\s\S]{0,200}syncSearchKindFilter\(searchKindForFacet\(groupName\), false\)/);
  assert.match(indexSource, /setSearchKindFilter\(searchKindFilter, target\.checked\)[\s\S]{0,800}refreshSearchAfterFilterChange\(searchKindFilter, activatedFromHistory\)/);
  assert.match(indexSource, /syncMetaFilterGroup\(`search-\$\{groupName\}`\);[\s\S]{0,180}refreshSearchAfterFilterChange\(groupName, activatedFromHistory\)/);
  assert.match(indexSource, /function renderSearchMetaFilters[\s\S]*?for \(const kind of \[[\s\S]*?syncSearchKindFilter\(kind\)/);
});

test('search filter tree folds facets and persists disclosure state', () => {
  const indexHtml = source('index.html');
  const indexSource = source('index.js');

  assert.match(indexHtml, /\.search-tree-toggle[\s\S]*transition: transform 160ms ease/);
  assert.match(indexHtml, /\.search-tree-toggle\[aria-expanded="true"\][\s\S]*rotate\(90deg\)/);
  assert.match(indexHtml, /\.meta-filter input \{ accent-color: var\(--accent\); margin: 0; \}/);
  assert.match(indexHtml, /\.search-meta-facet-children \{[\s\S]*grid-template-columns: 14px minmax\(0, 1fr\)/);
  assert.match(indexSource, /defaultSearchFilterTreeExpanded = \[[\s\S]*'kind:videos'[\s\S]*'kind:playlists'[\s\S]*'kind:channels'/);
  assert.match(indexSource, /data-search-tree-toggle="\$\{escapeHtml\(nodeId\)\}"/);
  assert.match(indexSource, /class="search-meta-facet-children"[\s\S]*\$\{expanded \? '' : 'hidden'\}[\s\S]*class="search-tree-toggle-spacer"[\s\S]*class="search-meta-controls"/);
  assert.match(indexSource, /\/api\/settings\/search-filter-tree/);
});

test('uploader category facet requires detected categories', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function uploaderCategoryMetaFilterDefinitions\(counts\)[\s\S]*?if \(!categories\.length\) return \[\]/);
  assert.match(indexSource, /\.\.\.\(uploaderCategoryDefinitions\.length \? \[[\s\S]*?allLabel: 'Uploader category'/);
});

test('history heatmap can return to the current year and day', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /current\.dataset\.historyCurrent = ''/);
  assert.match(indexSource, /current\.textContent = '>\|'/);
  assert.match(indexSource, /async function jumpToCurrentHistoryActivity\(\)/);
  assert.match(indexSource, /historyActivityYearOffset = 0/);
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
  assert.equal(helpers.watchProgressPercent({ watch_progress_percent: 0.4 }), 1);
  assert.equal(helpers.watchProgressPercent({ watch_progress_percent: 104 }), 100);
  assert.equal(helpers.watchProgressPercent({ watch_progress_percent: 'bad' }), 0);
});
