(function () {
  'use strict';

  const nativeKinds = new Set(['video', 'clip', 'playlist', 'channel']);
  const identityFields = Object.freeze({
    video: 'video_id',
    clip: 'clip_id',
    playlist: 'playlist_id',
    channel: 'channel_id',
  });
  const contributionSlots = Object.freeze({
    actions: 'span',
    primaryMetadata: 'span',
    secondaryMetadata: 'div',
  });
  const cardRuns = new WeakMap();

  function validateDefinition(definition) {
    if (!definition || typeof definition !== 'object') {
      throw new TypeError('entityCards must be an object');
    }
    const capability = String(definition.capability || '').trim();
    if (!capability) throw new TypeError('entityCards capability is required');
    if (!Array.isArray(definition.kinds) || !definition.kinds.length) {
      throw new TypeError('entityCards kinds must be a nonempty array');
    }
    const kinds = definition.kinds.map(kind => String(kind || '').trim());
    const invalidKind = kinds.find(kind => !nativeKinds.has(kind));
    if (invalidKind) throw new TypeError(`Unsupported entityCards kind: ${invalidKind}`);
    if (new Set(kinds).size !== kinds.length) {
      throw new TypeError('entityCards kinds must be unique');
    }
    if (definition.prepare !== undefined && typeof definition.prepare !== 'function') {
      throw new TypeError('entityCards prepare must be a function');
    }
    if (typeof definition.render !== 'function') {
      throw new TypeError('entityCards render must be a function');
    }
  }

  function descriptor(kind, item) {
    const normalizedKind = String(kind || '').trim();
    if (!nativeKinds.has(normalizedKind) || !item || typeof item !== 'object') return null;
    const id = String(item[identityFields[normalizedKind]] || '').trim();
    return id ? Object.freeze({ kind: normalizedKind, id, item }) : null;
  }

  function slotFor(card, name) {
    return card?.querySelector?.(`[data-entity-card-slot="${name}"]`) || null;
  }

  function contributionFor(slot, pluginId, name) {
    if (!slot) return null;
    const contribution = document.createElement(contributionSlots[name]);
    const classSuffix = name.replace(/[A-Z]/g, value => `-${value.toLowerCase()}`);
    contribution.className = `entity-card-plugin-contribution entity-card-plugin-${classSuffix}`;
    contribution.dataset.browserPluginId = pluginId;
    contribution.dataset.entityCardContribution = name;
    contribution.hidden = true;
    slot.append(contribution);
    return contribution;
  }

  function resultElements(result, name) {
    if (result === null || result === undefined) return [];
    if (!result || typeof result !== 'object') {
      throw new TypeError('entityCards render must return an object or null');
    }
    const elements = result[name] === undefined ? [] : result[name];
    if (!Array.isArray(elements) || elements.some(element => !(element instanceof HTMLElement))) {
      throw new TypeError(`entityCards ${name} must be an array of HTMLElements`);
    }
    return elements;
  }

  function reportFailure(message, pluginId, error) {
    console.error(`${message}: ${pluginId}`, error);
  }

  async function decorateBatch(options = {}) {
    const entries = (Array.isArray(options.entries) ? options.entries : [])
      .filter(entry => entry?.card instanceof HTMLElement);
    const plugins = Array.isArray(options.plugins) ? options.plugins : [];
    const context = options.context && typeof options.context === 'object'
      ? options.context
      : {};
    const supports = typeof options.supports === 'function'
      ? options.supports
      : () => true;
    const hostFor = typeof options.hostFor === 'function'
      ? options.hostFor
      : () => ({});
    const isCurrent = typeof options.isCurrent === 'function'
      ? options.isCurrent
      : () => true;
    const token = {};

    if (!isCurrent()) return false;
    for (const entry of entries) {
      cardRuns.set(entry.card, token);
      for (const name of Object.keys(contributionSlots)) {
        slotFor(entry.card, name)?.replaceChildren();
      }
    }

    const plans = [];
    for (const plugin of plugins) {
      const definition = plugin?.entityCards;
      const pluginId = String(plugin?.id || '');
      if (!definition || !supports(pluginId, definition.capability)) continue;
      const kinds = new Set(definition.kinds);
      const occurrences = entries.filter(entry => entry.entity && kinds.has(entry.entity.kind));
      if (!occurrences.length) continue;
      const uniqueEntities = [];
      const seen = new Set();
      for (const entry of occurrences) {
        const key = `${entry.entity.kind}:${entry.entity.id}`;
        if (seen.has(key)) continue;
        seen.add(key);
        uniqueEntities.push(entry.entity);
      }
      const contributions = new Map();
      for (const entry of occurrences) {
        contributions.set(
          entry,
          Object.fromEntries(Object.keys(contributionSlots).map(name => [
            name,
            contributionFor(slotFor(entry.card, name), pluginId, name),
          ])),
        );
      }
      plans.push({ plugin, definition, occurrences, uniqueEntities, contributions });
    }

    await Promise.all(plans.map(async plan => {
      const pluginId = String(plan.plugin.id);
      const host = hostFor(pluginId);
      let preparedState;
      try {
        preparedState = typeof plan.definition.prepare === 'function'
          ? await plan.definition.prepare(plan.uniqueEntities, host, context)
          : undefined;
      } catch (error) {
        reportFailure('Plugin entity-card preparation failed', pluginId, error);
        return;
      }
      if (!isCurrent()) return;
      for (const entry of plan.occurrences) {
        if (!isCurrent() || cardRuns.get(entry.card) !== token) return;
        const contribution = plan.contributions.get(entry);
        try {
          const result = plan.definition.render(entry.entity, preparedState, host, context);
          if (result && typeof result.then === 'function') {
            throw new TypeError('entityCards render must return synchronously');
          }
          for (const name of Object.keys(contributionSlots)) {
            const elements = resultElements(result, name);
            if (!contribution[name]) continue;
            contribution[name].replaceChildren(...elements);
            contribution[name].hidden = elements.length === 0;
          }
        } catch (error) {
          reportFailure('Plugin entity-card rendering failed', pluginId, error);
        }
      }
    }));
    return isCurrent();
  }

  window.YTLibraryEntityCardExtensions = Object.freeze({
    decorateBatch,
    descriptor,
    validateDefinition,
  });
})();
