(function historyWorkflowModule(global) {
  'use strict';

  async function loadPage(options) {
    const {
      channelId = '',
      fetchActivity,
      fetchLocation,
      isCurrent = () => true,
      pendingDate = '',
      syncActivityYear,
      syncEnabled = false,
    } = options;
    const [payload, initialActivity] = await fetchLocation(channelId);
    if (!isCurrent()) return null;
    const rows = Array.isArray(payload?.watch) ? payload.watch : [];
    const activity = syncEnabled && syncActivityYear(rows, pendingDate)
      ? await fetchActivity(channelId)
      : initialActivity;
    if (!isCurrent()) return null;
    const total = Number(
      payload?.totals?.filtered_watch_rows
      ?? payload?.totals?.watch_rows
      ?? rows.length
    );
    return { activity, payload, rows, total };
  }

  function setHeatmapBusy(heatmap, busy) {
    if (busy) heatmap.setAttribute('aria-busy', 'true');
    else heatmap.removeAttribute('aria-busy');
    for (const button of heatmap.querySelectorAll('.history-heatmap-nav button')) {
      button.disabled = busy;
    }
  }

  async function runTransition(options) {
    const {
      applyState,
      captureState,
      commit,
      heatmap,
      isCurrent = () => true,
      load,
      restoreControls = () => {},
      restoreState,
    } = options;
    const snapshot = captureState();
    let restored = false;
    const restore = () => {
      if (restored) return;
      restored = true;
      restoreState(snapshot);
    };
    setHeatmapBusy(heatmap, true);
    try {
      applyState();
      const value = await load();
      if (!isCurrent() || !heatmap.isConnected) {
        restore();
        return { committed: false, stale: true };
      }
      await commit(value);
      return { committed: true, stale: false };
    } catch (error) {
      restore();
      throw error;
    } finally {
      if (heatmap.isConnected) {
        setHeatmapBusy(heatmap, false);
        restoreControls(heatmap);
      }
    }
  }

  global.YTLibraryHistoryWorkflow = Object.freeze({
    loadPage,
    runTransition,
  });
})(window);
