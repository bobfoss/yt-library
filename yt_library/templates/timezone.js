(function () {
  const config = window.YT_LIBRARY_CONFIG || {};

  async function persist(timeZone) {
    const params = new URLSearchParams({ value: timeZone });
    const response = await fetch(`/api/settings/timezone?${params}`, { method: 'POST' });
    if (!response.ok) throw new Error(`Could not save timezone (${response.status})`);
    config.displayTimezone = timeZone;
    window.dispatchEvent(new CustomEvent('ytlibrarytimezonechange', { detail: timeZone }));
    return timeZone;
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

  window.YTLibraryTime = {
    detected,
    format,
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
