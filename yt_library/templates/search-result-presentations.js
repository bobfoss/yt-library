(function () {
  'use strict';

  const nativeKinds = new Set(['video', 'clip', 'playlist', 'channel']);
  const identityFields = Object.freeze({
    video: 'video_id',
    clip: 'clip_id',
    playlist: 'playlist_id',
    channel: 'channel_id',
  });

  function validateDefinition(definition) {
    if (!definition || typeof definition !== 'object') {
      throw new TypeError('search resultPresentation must be an object');
    }
    if (!Array.isArray(definition.kinds) || !definition.kinds.length) {
      throw new TypeError('search resultPresentation kinds must be a nonempty array');
    }
    const kinds = definition.kinds.map(kind => String(kind || '').trim());
    const invalidKind = kinds.find(kind => !nativeKinds.has(kind));
    if (invalidKind) {
      throw new TypeError(`Unsupported search resultPresentation kind: ${invalidKind}`);
    }
    if (new Set(kinds).size !== kinds.length) {
      throw new TypeError('search resultPresentation kinds must be unique');
    }
    if (definition.prepare !== undefined && typeof definition.prepare !== 'function') {
      throw new TypeError('search resultPresentation prepare must be a function');
    }
    if (typeof definition.render !== 'function') {
      throw new TypeError('search resultPresentation render must be a function');
    }
  }

  function descriptor(result) {
    const kind = String(result?.kind || '').trim();
    const item = result?.item;
    if (!nativeKinds.has(kind) || !item || typeof item !== 'object') return null;
    const id = String(item[identityFields[kind]] || '').trim();
    if (!id) return null;
    const pluginFacets = Object.freeze({ ...(result.pluginFacets || {}) });
    const pluginSearchMatches = Object.freeze(
      [...new Set((result.pluginSearchMatches || []).map(String).filter(Boolean))],
    );
    return Object.freeze({ kind, id, item, pluginFacets, pluginSearchMatches });
  }

  function normalizedPresentation(value) {
    if (value === null || value === undefined) return null;
    if (!value || typeof value !== 'object') {
      throw new TypeError('search resultPresentation render must return an object or null');
    }
    const kindLabel = value.kindLabel === undefined
      ? ''
      : String(value.kindLabel || '').trim();
    const summary = value.summary === undefined ? null : value.summary;
    if (summary !== null && !(summary instanceof HTMLElement)) {
      throw new TypeError('search resultPresentation summary must be an HTMLElement or null');
    }
    return kindLabel || summary ? Object.freeze({ kindLabel, summary }) : null;
  }

  async function prepareBatch(options = {}) {
    const records = (Array.isArray(options.results) ? options.results : [])
      .map(result => ({ result, descriptor: descriptor(result) }))
      .filter(record => record.descriptor);
    const plugins = Array.isArray(options.plugins) ? options.plugins : [];
    const context = options.context && typeof options.context === 'object'
      ? options.context
      : {};
    const hostFor = typeof options.hostFor === 'function'
      ? options.hostFor
      : () => ({});
    const applies = typeof options.applies === 'function'
      ? options.applies
      : () => true;
    const isCurrent = typeof options.isCurrent === 'function'
      ? options.isCurrent
      : () => true;

    if (!isCurrent()) return { failures: [], presentations: new Map() };
    const plans = [];
    for (const plugin of plugins) {
      const definition = plugin?.search?.resultPresentation;
      if (!definition) continue;
      const kinds = new Set(definition.kinds);
      const occurrences = records.filter(record => (
        kinds.has(record.descriptor.kind) && applies(plugin, record.descriptor)
      ));
      if (occurrences.length) plans.push({ plugin, definition, occurrences });
    }

    const preparedPlans = await Promise.all(plans.map(async plan => {
      const pluginId = String(plan.plugin.id || '');
      const host = hostFor(pluginId);
      try {
        const preparedState = typeof plan.definition.prepare === 'function'
          ? await plan.definition.prepare(
            plan.occurrences.map(record => record.descriptor),
            host,
            context,
          )
          : undefined;
        return { ...plan, host, pluginId, preparedState, preparationError: null };
      } catch (error) {
        return { ...plan, host, pluginId, preparedState: undefined, preparationError: error };
      }
    }));

    if (!isCurrent()) return { failures: [], presentations: new Map() };
    const failures = [];
    const presentations = new Map();
    for (const plan of preparedPlans) {
      if (plan.preparationError) {
        failures.push({ pluginId: plan.pluginId, error: plan.preparationError });
        continue;
      }
      for (const record of plan.occurrences) {
        try {
          const value = plan.definition.render(
            record.descriptor,
            plan.preparedState,
            plan.host,
            context,
          );
          if (value && typeof value.then === 'function') {
            throw new TypeError('search resultPresentation render must return synchronously');
          }
          const presentation = normalizedPresentation(value);
          if (!presentation) continue;
          const current = presentations.get(record.result) || { kindLabel: '', summaries: [] };
          if (!current.kindLabel && presentation.kindLabel) {
            current.kindLabel = presentation.kindLabel;
          }
          if (presentation.summary) {
            current.summaries.push({ pluginId: plan.pluginId, element: presentation.summary });
          }
          presentations.set(record.result, current);
        } catch (error) {
          failures.push({ pluginId: plan.pluginId, error });
        }
      }
    }
    return { failures, presentations };
  }

  function apply(card, presentation) {
    if (!(card instanceof HTMLElement) || !presentation) return card;
    const kind = card.querySelector('.result-kind');
    if (kind instanceof HTMLElement && presentation.kindLabel) {
      kind.textContent = presentation.kindLabel;
    }
    const slot = card.querySelector('[data-search-result-slot="summaries"]');
    if (!(slot instanceof HTMLElement)) return card;
    const summaries = Array.isArray(presentation.summaries) ? presentation.summaries : [];
    const contributions = summaries.map(({ pluginId, element }) => {
      const contribution = document.createElement('div');
      contribution.className = 'plugin-search-result-summary';
      contribution.dataset.browserPluginId = String(pluginId || '');
      contribution.append(element);
      return contribution;
    });
    slot.replaceChildren(...contributions);
    const nativeSummary = card.querySelector('[data-search-result-native-summary]');
    if (nativeSummary instanceof HTMLElement) nativeSummary.hidden = contributions.length > 0;
    return card;
  }

  window.YTLibrarySearchResultPresentations = Object.freeze({
    apply,
    descriptor,
    prepareBatch,
    validateDefinition,
  });
})();
