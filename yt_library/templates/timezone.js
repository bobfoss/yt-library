(function () {
  const config = window.YT_LIBRARY_CONFIG || {};

  function apply(timeZone) {
    config.displayTimezone = timeZone;
    window.dispatchEvent(new CustomEvent('ytlibrarytimezonechange', { detail: timeZone }));
    return timeZone;
  }

  async function persist(timeZone) {
    const params = new URLSearchParams({ value: timeZone });
    const response = await fetch(`/api/settings/timezone?${params}`, { method: 'POST' });
    if (!response.ok) throw new Error(`Could not save timezone (${response.status})`);
    return apply(timeZone);
  }

  function detected() {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  }

  function format(value, options) {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'medium',
      timeZone: config.displayTimezone || detected(),
      ...options,
    }).format(parsed);
  }

  function formatDate(value) {
    if (!value) return '';
    const text = String(value).trim();
    const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
    if (dateOnly) {
      const parsed = new Date(
        Number(dateOnly[1]),
        Number(dateOnly[2]) - 1,
        Number(dateOnly[3])
      );
      return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(parsed);
    }
    const parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) return text;
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeZone: config.displayTimezone || detected(),
    }).format(parsed);
  }

  function dateKey(value) {
    if (!value) return '';
    const text = String(value).trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
    const parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) return '';
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat('en-US', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        timeZone: config.displayTimezone || detected(),
      }).formatToParts(parsed).map(part => [part.type, part.value])
    );
    return `${parts.year}-${parts.month}-${parts.day}`;
  }

  window.YTLibraryTime = {
    apply,
    dateKey,
    detected,
    format,
    formatDate,
    get timeZone() { return config.displayTimezone || ''; },
    persist,
    async reset() {
      const response = await fetch('/api/settings/timezone', { method: 'DELETE' });
      if (!response.ok) throw new Error(`Could not reset timezone (${response.status})`);
      config.displayTimezone = '';
      return persist(detected());
    },
  };

  if (!config.displayTimezone) {
    persist(detected()).catch(error => console.warn(error));
  }
})();
