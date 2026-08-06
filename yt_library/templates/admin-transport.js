(function adminTransportModule(global) {
  'use strict';

  async function postJson(path, params = {}) {
    const query = new URLSearchParams(params).toString();
    const url = query ? `${path}?${query}` : path;
    const response = await global.fetch(url, { method: 'POST' });
    let payload = {};
    try {
      payload = await response.json();
    } catch (error) {
      // Restarting the service can leave a successful response without a JSON body.
    }
    if (!response.ok) {
      const providedMessage = payload && typeof payload.error === 'string'
        ? payload.error.trim()
        : '';
      const message = providedMessage || `Request failed: ${response.status}`;
      throw new Error(message);
    }
    return payload;
  }

  global.YTLibraryAdminTransport = Object.freeze({ postJson });
})(window);
