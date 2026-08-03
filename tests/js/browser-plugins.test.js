const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const templateDirectory = path.join(process.cwd(), 'yt_library', 'templates');
const indexSource = fs.readFileSync(path.join(templateDirectory, 'index.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(templateDirectory, 'index.html'), 'utf8');

test('browser plugins are loaded through a generic registration contract', () => {
  assert.match(indexSource, /window\.YTLibraryBrowserPlugins = Object\.freeze/);
  assert.match(indexSource, /register: registerBrowserPlugin/);
  assert.match(indexSource, /status\.browserAssets \|\| \[\]/);
  assert.match(indexSource, /\/plugins\/\$\{encodeURIComponent\(pluginId\)\}\/assets\//);
  assert.match(indexSource, /libraryVideos,/);
  assert.match(indexSource, /createSearchVideoCard: searchVideoCardFor/);
  assert.match(indexSource, /browserSearchPresets\('videos'\)/);
  assert.match(indexSource, /searchPresetDefinition\(preset\)/);
  assert.match(indexSource, /query \|\| plugin\.search\.fetchEmptyQuery === true/);
});

test('plugin-specific browser presentation stays outside core templates', () => {
  assert.doesNotMatch(indexSource, /subtitle/i);
  assert.doesNotMatch(indexHtml, /subtitle/i);
});
