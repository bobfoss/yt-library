const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const transportSource = fs.readFileSync(
  path.join(process.cwd(), 'yt_library', 'templates', 'admin-transport.js'),
  'utf8',
);

function loadTransport(fetchImpl) {
  const window = { fetch: fetchImpl };
  const context = { URLSearchParams, window };
  vm.runInNewContext(transportSource, context, { filename: 'admin-transport.js' });
  return window.YTLibraryAdminTransport;
}

function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    async json() { return payload; },
  };
}

test('Admin parameter posts share query encoding and POST setup', async () => {
  const calls = [];
  const transport = loadTransport(async (url, options) => {
    calls.push({ options, url });
    return jsonResponse({ queued: true });
  });

  const payload = await transport.postJson('/api/admin/queue/add-target', {
    enabled: 1,
    target: 'two words',
  });
  await transport.postJson('/api/admin/queue/stop');

  assert.equal(payload.queued, true);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, '/api/admin/queue/add-target?enabled=1&target=two+words');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[1].url, '/api/admin/queue/stop');
  assert.equal(calls[1].options.method, 'POST');
});

test('Admin parameter posts preserve structured API errors', async () => {
  const transport = loadTransport(async () => jsonResponse(
    { error: 'Queue is already stopping' },
    { ok: false, status: 409 },
  ));

  await assert.rejects(
    transport.postJson('/api/admin/queue/stop'),
    /Queue is already stopping/,
  );
});

test('Admin parameter posts use status errors when error JSON is unavailable', async () => {
  const transport = loadTransport(async () => ({
    ok: false,
    status: 503,
    async json() { throw new SyntaxError('empty response'); },
  }));

  await assert.rejects(
    transport.postJson('/api/admin/service/restart'),
    /Request failed: 503/,
  );
});

test('Successful Admin posts tolerate an empty restart response', async () => {
  const transport = loadTransport(async () => ({
    ok: true,
    status: 200,
    async json() { throw new SyntaxError('connection closed'); },
  }));

  const payload = await transport.postJson('/api/admin/service/restart');

  assert.equal(Object.keys(payload).length, 0);
});

test('Admin parameter posts propagate network failures', async () => {
  const transport = loadTransport(async () => {
    throw new Error('service unavailable');
  });

  await assert.rejects(
    transport.postJson('/api/admin/update/start'),
    /service unavailable/,
  );
});
