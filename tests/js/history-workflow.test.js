const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const workflowSource = fs.readFileSync(
  path.join(process.cwd(), 'yt_library', 'templates', 'history-workflow.js'),
  'utf8',
);

function loadWorkflow() {
  const context = { window: {} };
  vm.runInNewContext(workflowSource, context, { filename: 'history-workflow.js' });
  return context.window.YTLibraryHistoryWorkflow;
}

function fakeHeatmap() {
  const attributes = new Map();
  const buttons = [{ disabled: false }, { disabled: false }];
  return {
    attributes,
    buttons,
    isConnected: true,
    querySelectorAll: selector => (
      selector === '.history-heatmap-nav button' ? buttons : []
    ),
    removeAttribute: name => attributes.delete(name),
    setAttribute: (name, value) => attributes.set(name, value),
  };
}

test('history page loading shares activity-year refresh and total semantics', async () => {
  const workflow = loadWorkflow();
  const activityCalls = [];
  const result = await workflow.loadPage({
    channelId: 'UCexample',
    fetchActivity: async channelId => {
      activityCalls.push(channelId);
      return { activity: [{ watch_date: '2025-08-06' }] };
    },
    fetchLocation: async channelId => [
      {
        watch: [{ video_id: 'one' }, { video_id: 'two' }],
        totals: { filtered_watch_rows: 7, watch_rows: 9 },
      },
      { activity: [{ watch_date: '2026-08-06' }], channelId },
    ],
    pendingDate: '2025-08-06',
    syncActivityYear: (rows, pendingDate) => (
      rows.length === 2 && pendingDate === '2025-08-06'
    ),
    syncEnabled: true,
  });

  assert.equal(result.total, 7);
  assert.deepEqual(result.rows.map(row => row.video_id), ['one', 'two']);
  assert.deepEqual(result.activity.activity, [{ watch_date: '2025-08-06' }]);
  assert.deepEqual(activityCalls, ['UCexample']);
});

test('stale history loads stop before sibling activity work or commit', async () => {
  const workflow = loadWorkflow();
  let current = true;
  let activityCalls = 0;
  const result = await workflow.loadPage({
    fetchActivity: async () => {
      activityCalls += 1;
      return {};
    },
    fetchLocation: async () => {
      current = false;
      return [{ watch: [{}] }, { activity: [] }];
    },
    isCurrent: () => current,
    syncActivityYear: () => true,
    syncEnabled: true,
  });

  assert.equal(result, null);
  assert.equal(activityCalls, 0);
});

test('heatmap transitions own busy controls and commit applied state once', async () => {
  const workflow = loadWorkflow();
  const heatmap = fakeHeatmap();
  const state = { page: 1 };
  let commits = 0;
  let controlsRestored = 0;

  const result = await workflow.runTransition({
    applyState: () => { state.page = 2; },
    captureState: () => ({ ...state }),
    commit: value => {
      commits += 1;
      assert.equal(value, 'loaded');
      assert.equal(state.page, 2);
    },
    heatmap,
    load: async () => {
      assert.equal(heatmap.attributes.get('aria-busy'), 'true');
      assert.ok(heatmap.buttons.every(button => button.disabled));
      return 'loaded';
    },
    restoreControls: () => { controlsRestored += 1; },
    restoreState: snapshot => Object.assign(state, snapshot),
  });

  assert.equal(result.committed, true);
  assert.equal(result.stale, false);
  assert.equal(state.page, 2);
  assert.equal(commits, 1);
  assert.equal(controlsRestored, 1);
  assert.equal(heatmap.attributes.has('aria-busy'), false);
  assert.ok(heatmap.buttons.every(button => !button.disabled));
});

test('failed and stale heatmap transitions roll back every captured field', async () => {
  const workflow = loadWorkflow();
  for (const outcome of ['failure', 'stale']) {
    const heatmap = fakeHeatmap();
    const state = {
      navigationDate: '2026-08-06',
      page: 3,
      pendingDate: '2026-08-06',
    };
    let commits = 0;
    const transition = workflow.runTransition({
      applyState: () => Object.assign(state, {
        navigationDate: '2025-08-06',
        page: 8,
        pendingDate: '2025-08-06',
      }),
      captureState: () => ({ ...state }),
      commit: () => { commits += 1; },
      heatmap,
      isCurrent: () => outcome !== 'stale',
      load: async () => {
        if (outcome === 'failure') throw new Error('activity failed');
        return {};
      },
      restoreState: snapshot => Object.assign(state, snapshot),
    });

    if (outcome === 'failure') {
      await assert.rejects(transition, /activity failed/);
    } else {
      const result = await transition;
      assert.equal(result.committed, false);
      assert.equal(result.stale, true);
    }
    assert.deepEqual(state, {
      navigationDate: '2026-08-06',
      page: 3,
      pendingDate: '2026-08-06',
    });
    assert.equal(commits, 0);
    assert.equal(heatmap.attributes.has('aria-busy'), false);
  }
});
