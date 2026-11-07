const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const api = require('../gallery-source.js');
const photo = 'photos/onedrive/spotify/' + 'a'.repeat(64) + '.jpg';
const photo2 = 'photos/onedrive/second/' + 'b'.repeat(64) + '.png';
const manifest = images => ({version: 1, images, updatedAt: '2026-11-06T08:00:00Z'});

function fixture(cached, storageThrows = false) {
  let success, failure, nextTimer = 0;
  const updates = [], timers = new Map(), data = {};
  if (cached) data.cache = JSON.stringify(cached);
  const controller = api.createController({
    fallback: ['f1.jpeg', 'f2.jpeg', 'f1.jpeg'],
    cacheKey: 'cache',
    storage: {
      getItem: key => { if (storageThrows) throw Error('blocked'); return data[key] || null; },
      setItem: (key, value) => { if (storageThrows) throw Error('blocked'); data[key] = value; }
    },
    schedule: (fn, ms) => { const id = ++nextTimer; timers.set(id, {fn, ms}); return id; },
    cancel: id => timers.delete(id),
    request: (ok, fail) => { success = ok; failure = fail; },
    onImages: (images, source) => updates.push({images, source})
  });
  return {controller, updates, timers, data,
    success: value => success(value), failure: () => failure(Error('offline'))};
}

test('manifest paths are same-origin opaque raster paths and duplicates are removed', () => {
  assert.deepEqual(api.manifestImages(manifest([photo, photo, photo2])), [photo, photo2]);
  assert.deepEqual(api.manifestImages(manifest([])), []);
});

test('invalid formats and unsafe paths cannot replace the gallery', () => {
  for (const value of [null, {}, {version: 2, images: []}, {version: 1, images: 'x'}]) {
    assert.throws(() => api.manifestImages(value));
  }
  for (const value of ['https://example.com/a.jpg', '//example.com/a.jpg', '../private.jpg',
    'photos/onedrive/spotify/../../private.jpg', 'photos/onedrive/spotify/a.svg',
    'photos/onedrive/spotify/a.jpg?secret=1', 'photos/onedrive/spotify/a%2fsecret.jpg', 5]) {
    assert.throws(() => api.manifestImages(manifest([value])));
  }
});

test('startup plays bundled fallback immediately, then prefers OneDrive', () => {
  const f = fixture(); f.controller.start();
  assert.deepEqual(f.updates[0].images, ['f1.jpeg', 'f2.jpeg']);
  f.success(manifest([photo]));
  assert.deepEqual(f.controller.current(), {images: [photo], source: 'onedrive'});
  assert.equal(f.timers.size, 1);
  assert.equal([...f.timers.values()][0].ms, 900000);
});

test('temporary request failure retains last good photos; later success updates in place', () => {
  const f = fixture(); f.controller.start(); f.success(manifest([photo]));
  f.controller.refresh(); f.failure();
  assert.deepEqual(f.controller.current().images, [photo]);
  assert.equal(f.updates.length, 2);
  f.controller.refresh(); f.success(manifest([photo2]));
  assert.deepEqual(f.controller.current().images, [photo2]);
  assert.equal(f.timers.size, 1);
});

test('valid empty source clears stale cache and selects bundled fallback', () => {
  const f = fixture(manifest([photo])); f.controller.start();
  assert.equal(f.controller.current().source, 'cached-onedrive');
  f.success(manifest([]));
  assert.deepEqual(f.controller.current().images, ['f1.jpeg', 'f2.jpeg']);
  assert.deepEqual(JSON.parse(f.data.cache).images, []);
});

test('invalid manifest retains cached photos and does not poison the cache', () => {
  const f = fixture(manifest([photo])); f.controller.start();
  f.success({version: 1, images: ['../credentials.json']});
  assert.deepEqual(f.controller.current().images, [photo]);
  assert.deepEqual(JSON.parse(f.data.cache).images, [photo]);
});

test('blocked browser storage and unavailable manifest still allow bundled gallery', () => {
  const f = fixture(null, true); f.controller.start(); f.failure();
  assert.deepEqual(f.controller.current().images, ['f1.jpeg', 'f2.jpeg']);
  f.controller.refresh(); f.success(manifest([photo]));
  assert.deepEqual(f.controller.current().images, [photo]);
});

test('duplicate starts and unchanged manifests do not reinitialize animation', () => {
  const f = fixture(); f.controller.start(); f.controller.start();
  assert.equal(f.updates.length, 1);
  f.success(manifest([photo])); f.controller.refresh(); f.success(manifest([photo]));
  assert.equal(f.updates.length, 2);
  f.controller.stop(); assert.equal(f.timers.size, 0);
});

test('controller copies arrays so the mutating animation cannot poison source/cache', () => {
  const f = fixture(); f.controller.start(); const value = manifest([photo]); f.success(value);
  f.updates[1].images.splice(0, 1);
  const returned = f.controller.current(); returned.images.length = 0;
  assert.deepEqual(f.controller.current().images, [photo]);
  assert.deepEqual(value.images, [photo]);
});

test('all bundled image references exist and the missing legacy filename is corrected', () => {
  const root = path.resolve(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
  const images = [...html.matchAll(/"([^"\n]+\.(?:jpe?g|png|webp|gif|avif))"/gi)].map(m => m[1]);
  assert.ok(images.length > 100);
  for (const name of images) assert.ok(fs.existsSync(path.join(root, name)), name);
  assert.ok(html.includes('gallery-source.js'));
  assert.ok(html.includes('ShiftingTilesPhotos.mount'));
  assert.ok(!html.includes('f104.jpeg'));
});

test('frontend JavaScript and inline initializer have valid syntax', () => {
  for (const name of ['gallery-source.js', 'shiftingtiles.js']) {
    new vm.Script(fs.readFileSync(path.resolve(__dirname, '..', name), 'utf8'));
  }
  const html = fs.readFileSync(path.resolve(__dirname, '..', 'index.html'), 'utf8');
  const inline = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
  new vm.Script(inline);
});
