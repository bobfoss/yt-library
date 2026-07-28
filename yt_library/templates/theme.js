(() => {
  const storageKey = 'yt-library-theme';
  const supportedThemes = new Set(['dark', 'light']);

  function normalize(theme) {
    return supportedThemes.has(theme) ? theme : 'dark';
  }

  function storedTheme() {
    try {
      return window.localStorage.getItem(storageKey);
    } catch (error) {
      return '';
    }
  }

  function apply(theme) {
    const normalized = normalize(theme);
    document.documentElement.dataset.theme = normalized;
    return normalized;
  }

  function set(theme) {
    const normalized = apply(theme);
    try {
      window.localStorage.setItem(storageKey, normalized);
    } catch (error) {
      // The selected theme still applies for this page when storage is unavailable.
    }
    window.dispatchEvent(new CustomEvent('ytlibrarythemechange', { detail: { theme: normalized } }));
    return normalized;
  }

  window.YTLibraryTheme = {
    current: () => normalize(document.documentElement.dataset.theme),
    set,
  };
  apply(storedTheme() || 'dark');
})();
