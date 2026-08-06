const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const templateDirectory = path.join(process.cwd(), 'yt_library', 'templates');
const indexSource = fs.readFileSync(path.join(templateDirectory, 'index.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(templateDirectory, 'index.html'), 'utf8');
const videoCardSource = fs.readFileSync(path.join(templateDirectory, 'video-card.js'), 'utf8');
const collectionCardSource = fs.readFileSync(path.join(templateDirectory, 'collection-card.js'), 'utf8');
const extensionSource = fs.readFileSync(
  path.join(templateDirectory, 'entity-card-extensions.js'),
  'utf8',
);

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.className = '';
    this.hidden = false;
    this.textContent = '';
  }

  append(...elements) {
    for (const element of elements) {
      if (element.parentNode) {
        element.parentNode.children = element.parentNode.children.filter(item => item !== element);
      }
      element.parentNode = this;
      this.children.push(element);
    }
  }

  replaceChildren(...elements) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.append(...elements);
  }

  querySelector(selector) {
    const match = /^\[data-entity-card-slot="([^"]+)"\]$/.exec(selector);
    if (match && this.dataset.entityCardSlot === match[1]) return this;
    for (const child of this.children) {
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
}

function loadEntityCardExtensions(errors = []) {
  const context = {
    console: { error: (...args) => errors.push(args) },
    document: { createElement: tagName => new FakeElement(tagName) },
    HTMLElement: FakeElement,
    window: {},
  };
  vm.runInNewContext(extensionSource, context, { filename: 'entity-card-extensions.js' });
  return context.window.YTLibraryEntityCardExtensions;
}

function fakeCard() {
  const card = new FakeElement('article');
  const actions = new FakeElement('span');
  actions.dataset.entityCardSlot = 'actions';
  const secondaryMetadata = new FakeElement('div');
  secondaryMetadata.dataset.entityCardSlot = 'secondaryMetadata';
  card.append(actions, secondaryMetadata);
  return { actions, card, secondaryMetadata };
}

function labeledElement(label) {
  const element = new FakeElement('button');
  element.textContent = label;
  return element;
}

function plugin(id, overrides = {}) {
  return {
    id,
    entityCards: {
      capability: `${id}_cards`,
      kinds: ['video'],
      render: entity => ({
        actions: [labeledElement(`${id}:${entity.id}:action`)],
        secondaryMetadata: [labeledElement(`${id}:${entity.id}:metadata`)],
      }),
      ...overrides,
    },
  };
}

function videoEntry(extensions, id, card = fakeCard().card) {
  const item = { video_id: id, metadata_title: `Video ${id}` };
  return {
    card,
    entity: extensions.descriptor('video', item),
    legacyResult: { kind: 'video', item },
  };
}

function contributionLabels(slot) {
  return slot.children.flatMap(contribution => (
    contribution.children.map(element => element.textContent)
  ));
}

test('browser plugins are loaded through a generic registration contract', () => {
  assert.match(indexSource, /window\.YTLibraryBrowserPlugins = Object\.freeze/);
  assert.match(indexSource, /register: registerBrowserPlugin/);
  assert.match(indexSource, /features: Object\.freeze\(\{ entityCards: 1 \}\)/);
  assert.match(indexSource, /status\.browserAssets \|\| \[\]/);
  assert.match(indexSource, /\/plugins\/\$\{encodeURIComponent\(pluginId\)\}\/assets\//);
  assert.match(indexSource, /\?v=\$\{encodeURIComponent\(version\)\}/);
  assert.match(indexSource, /loadBrowserPluginAsset\(status\.id, asset, status\.version\)/);
  assert.match(
    indexSource,
    /try \{\s*await loadBrowserPluginAsset[\s\S]{0,180}console\.error\(`Plugin asset failed:/,
  );
  assert.match(indexSource, /libraryVideos,/);
  assert.match(indexSource, /createSearchVideoCard: searchVideoCardFor/);
  assert.match(indexSource, /searchHighlight,/);
  assert.match(indexSource, /browserSearchPresets\('videos'\)/);
  assert.match(indexSource, /searchPresetDefinition\(preset\)/);
  assert.match(indexSource, /query \|\| plugin\.search\.fetchEmptyQuery === true/);
  assert.match(indexSource, /decorateEntry: options\.legacySearch \? decorateLegacySearchCard : null/);
  assert.match(indexSource, /search-plugin-facet-filter/);
  assert.match(indexSource, /browserVideoFacetState/);
  assert.match(indexSource, /facetSelections\.push\([\s\S]*browserVideoFilterPlugins/);
  assert.match(indexSource, /restoreEmptySearchKindFacets[\s\S]*Object\.assign\(state, \{ present: true, absent: true \}\)/);
  assert.match(indexSource, /requestParams\.append\('video_facet_plugin', plugin\.id\)/);
  assert.match(indexSource, /requestParams\.append\('video_filter_plugin', plugin\.id\)/);
  assert.match(indexSource, /requestParams\.append\('video_exclude_filter_plugin', plugin\.id\)/);
  assert.match(indexSource, /plugin\?\.search\?\.searchField/);
  assert.match(indexSource, /label\.dataset\.browserPluginSearchField = plugin\.id/);
  assert.match(indexSource, /requestParams\.append\('video_search_plugin', plugin\.id\)/);
  assert.match(indexSource, /requestParams\.append\('video_search_plugin', '__none__'\)/);
});

test('legacy search preparation and video-detail panels remain available', () => {
  assert.match(indexSource, /async function decorateCoreSearchResults\(/);
  assert.match(indexSource, /plugin\.search\.decorateCoreResults\(/);
  assert.match(indexSource, /plugin\.search\.decorateCoreResultCard\(/);
  assert.match(indexSource, /async function renderBrowserPluginVideoPanels\(/);
  assert.match(indexSource, /const extension = plugin\.videoDetail/);
  assert.match(indexSource, /renderBrowserPluginVideoPanels\(videoId\)/);
});

test('entity-card definitions and canonical native descriptors are validated', () => {
  const extensions = loadEntityCardExtensions();

  assert.doesNotThrow(() => extensions.validateDefinition(plugin('plain').entityCards));
  assert.throws(
    () => extensions.validateDefinition({ capability: 'x', kinds: ['unknown'], render() {} }),
    /Unsupported entityCards kind/,
  );
  assert.throws(
    () => extensions.validateDefinition({ capability: 'x', kinds: ['video', 'video'], render() {} }),
    /must be unique/,
  );
  assert.equal(extensions.descriptor('video', {}), null);
  assert.equal(extensions.descriptor('clip', { clip_id: 'clip-1' }).id, 'clip-1');
});

test('entity-card plugins without search prepare once per batch and decorate every former view', async () => {
  const extensions = loadEntityCardExtensions();
  const cases = [
    { kind: 'video', view: 'search' },
    { kind: 'clip', view: 'search' },
    { kind: 'video', view: 'playlist' },
    { kind: 'video', view: 'history' },
    { kind: 'video', view: 'channel-history' },
    { kind: 'video', view: 'channel-playlists' },
    { kind: 'video', view: 'video-detail' },
    { kind: 'clip', view: 'clip-detail' },
  ];
  const preparedContexts = [];
  const genericPlugin = plugin('generic', {
    kinds: ['video', 'clip'],
    prepare: async (entities, _host, context) => {
      preparedContexts.push({ ids: entities.map(entity => entity.id), view: context.view });
      return new Set(entities.map(entity => entity.id));
    },
    render: (entity, prepared, _host, context) => ({
      actions: [labeledElement(`${context.view}:${entity.id}:${prepared.has(entity.id)}`)],
      secondaryMetadata: [],
    }),
  });

  for (const { kind, view } of cases) {
    const first = fakeCard();
    const second = fakeCard();
    const item = kind === 'clip' ? { clip_id: 'clip-1' } : { video_id: 'video-1' };
    const entries = [first, second].map(({ card }) => ({
      card,
      entity: extensions.descriptor(kind, item),
    }));
    await extensions.decorateBatch({
      context: { view, layout: 'detailed' },
      entries,
      hostFor: () => ({}),
      plugins: [genericPlugin],
      supports: () => true,
    });
    assert.deepEqual(contributionLabels(first.actions), [`${view}:${item[`${kind}_id`]}:true`]);
  }

  assert.equal(preparedContexts.length, cases.length);
  assert.equal(preparedContexts.filter(item => item.view === 'search').length, 2);
  assert.ok(preparedContexts.some(item => item.ids[0] === 'clip-1'));
  assert.ok(preparedContexts.some(item => item.ids[0] === 'video-1'));
});

test('entity-card actions and metadata compose in registration order', async () => {
  const extensions = loadEntityCardExtensions();
  const { actions, card, secondaryMetadata } = fakeCard();

  await extensions.decorateBatch({
    context: { view: 'search', layout: 'grid' },
    entries: [videoEntry(extensions, 'one', card)],
    plugins: [plugin('first'), plugin('second')],
    supports: () => true,
  });

  assert.deepEqual(actions.children.map(item => item.dataset.browserPluginId), ['first', 'second']);
  assert.deepEqual(secondaryMetadata.children.map(item => item.dataset.browserPluginId), ['first', 'second']);
  assert.deepEqual(contributionLabels(actions), ['first:one:action', 'second:one:action']);
  assert.deepEqual(
    contributionLabels(secondaryMetadata),
    ['first:one:metadata', 'second:one:metadata'],
  );
});

test('preparation and per-card rendering failures stay isolated', async () => {
  const errors = [];
  const extensions = loadEntityCardExtensions(errors);
  const first = fakeCard();
  const second = fakeCard();
  const renderFailure = plugin('render_failure', {
    render: entity => {
      if (entity.id === 'one') throw new Error('one failed');
      return { actions: [labeledElement('second survived')], secondaryMetadata: [] };
    },
  });
  const preparationFailure = plugin('prepare_failure', {
    prepare: async () => { throw new Error('prepare failed'); },
  });

  await extensions.decorateBatch({
    context: { view: 'history' },
    entries: [
      videoEntry(extensions, 'one', first.card),
      videoEntry(extensions, 'two', second.card),
    ],
    plugins: [preparationFailure, renderFailure, plugin('healthy')],
    supports: () => true,
  });

  assert.deepEqual(contributionLabels(first.actions), ['healthy:one:action']);
  assert.deepEqual(
    contributionLabels(second.actions),
    ['second survived', 'healthy:two:action'],
  );
  assert.equal(errors.length, 2);
});

test('stale and superseded batches cannot append or duplicate contributions', async () => {
  const extensions = loadEntityCardExtensions();
  const target = fakeCard();
  let release;
  let current = true;
  const slowPlugin = plugin('slow', {
    prepare: () => new Promise(resolve => { release = resolve; }),
  });
  const stale = extensions.decorateBatch({
    context: { view: 'search' },
    entries: [videoEntry(extensions, 'one', target.card)],
    isCurrent: () => current,
    plugins: [slowPlugin],
    supports: () => true,
  });
  current = false;
  release({});
  await stale;
  assert.deepEqual(contributionLabels(target.actions), []);

  current = true;
  for (let run = 0; run < 2; run += 1) {
    await extensions.decorateBatch({
      context: { view: 'search' },
      entries: [videoEntry(extensions, 'one', target.card)],
      isCurrent: () => current,
      plugins: [plugin('repeat')],
      supports: () => true,
    });
  }
  assert.equal(target.actions.children.length, 1);
  assert.deepEqual(contributionLabels(target.actions), ['repeat:one:action']);
});

test('capability gating skips unavailable plugins and legacy decorators use the shared batch', async () => {
  const extensions = loadEntityCardExtensions();
  const target = fakeCard();
  let legacyCalls = 0;

  await extensions.decorateBatch({
    context: { view: 'search' },
    decorateEntry: entry => {
      legacyCalls += 1;
      entry.card.dataset.legacyDecorated = 'true';
    },
    entries: [videoEntry(extensions, 'one', target.card)],
    plugins: [plugin('ready'), plugin('disabled'), plugin('missing_capability')],
    supports: (pluginId, capability) => pluginId === 'ready' && capability === 'ready_cards',
  });

  assert.equal(legacyCalls, 1);
  assert.equal(target.card.dataset.legacyDecorated, 'true');
  assert.deepEqual(contributionLabels(target.actions), ['ready:one:action']);
});

test('all native render entry points call the shared entity-card batch', () => {
  for (const view of ['search', 'playlist', 'history', 'video-detail', 'clip-detail']) {
    assert.match(indexSource, new RegExp(`decorateEntityCardBatch\\([\\s\\S]{0,500}'${view}'`));
  }
  assert.match(
    indexSource,
    /const layoutContext = 'channel-history'[\s\S]{0,1200}decorateEntityCardBatch\(/,
  );
  assert.match(
    indexSource,
    /const layoutContext = 'channel-playlists'[\s\S]{0,900}decorateEntityCardBatch\(/,
  );
  assert.match(indexSource, /entityCardEntry\('channel', channel, channelCard\)/);
  assert.match(indexSource, /data-entity-card-slot="actions"/);
  assert.match(indexSource, /data-entity-card-slot="secondaryMetadata"/);
});

test('shared card builders keep host slots in the same structural order for every layout', () => {
  assert.match(
    videoCardSource,
    /title-row[\s\S]*data-entity-card-slot="actions"/,
  );
  assert.ok(
    videoCardSource.indexOf('uploaderCategoryHtml(options.uploaderCategory)')
      < videoCardSource.indexOf('data-entity-card-slot="secondaryMetadata"'),
  );
  assert.ok(
    videoCardSource.indexOf('data-entity-card-slot="secondaryMetadata"')
      < videoCardSource.indexOf('options.descriptionHtml'),
  );
  assert.ok(
    collectionCardSource.indexOf('options.actionsHtml')
      < collectionCardSource.indexOf('data-entity-card-slot="actions"'),
  );
  assert.ok(
    collectionCardSource.indexOf('options.bodyHtml')
      < collectionCardSource.indexOf('data-entity-card-slot="secondaryMetadata"'),
  );
  assert.ok(
    collectionCardSource.indexOf('data-entity-card-slot="secondaryMetadata"')
      < collectionCardSource.indexOf('options.tailHtml'),
  );
  assert.match(indexHtml, /\.search-grid\.layout-compact/);
  assert.match(indexHtml, /\.entity-card-slot:empty/);
});

test('plugin-specific browser presentation stays outside core templates', () => {
  assert.doesNotMatch(indexSource, /subtitle/i);
  assert.doesNotMatch(indexHtml, /subtitle/i);
});
