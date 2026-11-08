const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('../gallery-source.js');
const photo = library => 'photos/onedrive/' + library + '/' + 'a'.repeat(64) + '.jpg';
const manifest = images => ({version: 1, images});
const cacheKey = library => 'shiftingtiles-public-gallery-v1:photos/onedrive/' + library + '/gallery.json';

function fixture(libraryId, data = {}, storageThrows = false) {
  const updates = [], requests = [], timers = new Map(); let id = 0;
  const reads = [], writes = [];
  const controller = api.createController({
    libraryId,
    fallback: ['f1.jpeg', 'f2.jpeg'],
    // Even caller-supplied legacy/shared keys cannot bypass library scoping.
    cacheKey: 'shared-cache',
    storage: {
      getItem(key) {
        reads.push(key);
        if (storageThrows) throw Error('blocked');
        return data[key] || null;
      },
      setItem(key, value) {
        writes.push(key);
        if (storageThrows) throw Error('blocked');
        data[key] = value;
      }
    },
    schedule(fn, ms) { timers.set(++id, {fn, ms}); return id; },
    cancel(id) { timers.delete(id); },
    request(success, failure) { requests.push({success, failure}); },
    onImages(images, source) { updates.push({images, source}); }
  });
  return {controller, data, requests, updates, reads, writes, timers};
}

function assertBlank(f) {
  assert.deepEqual(f.controller.current(), {images: [], source: 'empty'});
  assert.ok(f.updates.every(update => !update.images.includes('f1.jpeg')));
}

test('library query defaults only when absent and selects exact safe IDs', () => {
  for (const search of ['', '?', '?other=instagram']) assert.equal(api.libraryFromSearch(search), 'spotify');
  for (const [search, library] of [
    ['?library=spotify', 'spotify'], ['?library=instagram', 'instagram'],
    ['?foo=bar&library=new-library_2&other=x', 'new-library_2'],
    ['?%6cibrary=%73potify', 'spotify'], ['?library=' + 'a'.repeat(64), 'a'.repeat(64)]
  ]) {
    assert.equal(api.libraryFromSearch(search), library);
    assert.equal(api.manifestUrlFor(library), 'photos/onedrive/' + library + '/gallery.json');
  }
});

test('explicit invalid or ambiguous query never defaults to Spotify', () => {
  for (const search of [
    '?library', '?library=', '?library=Spotify', '?library=../spotify',
    '?library=spotify%2f', '?library=%2fspotify', '?library=spotify%0a',
    '?library=spotify%0d', '?library=spotify+', '?library=%00spotify',
    '?library=https://example.com', '?library=%', '?library=%FF',
    '?library=spotify&library=instagram', '?library=spotify&library=spotify',
    '?library=instagram&%6cibrary=spotify', '?library=' + 'a'.repeat(65)
  ]) {
    assert.equal(api.libraryFromSearch(search), null, search);
  }
  for (const value of [null, undefined, '', '../spotify', 'spotify\n', 'Spotify']) {
    assert.equal(api.manifestUrlFor(value), null);
  }
});

test('manifest validation rejects an entire foreign or mixed library response', () => {
  for (const library of ['spotify', 'instagram', 'new-library_2']) {
    const own = photo(library);
    assert.deepEqual(api.manifestImages(manifest([own, own]), library), [own]);
    assert.deepEqual(api.manifestImages(manifest([]), library), []);
    const foreign = photo(library === 'spotify' ? 'instagram' : 'spotify');
    for (const images of [[foreign], [own, foreign], [foreign, own]]) {
      assert.throws(() => api.manifestImages(manifest(images), library));
    }
    for (const image of [
      own + '\n', own + '\r', own + '?secret=x', own + '#fragment',
      own.replace('/' + library + '/', '/' + library.toUpperCase() + '/'),
      'photos/onedrive/' + library + '-other/file.jpg',
      'photos/onedrive/' + library + '/nested/file.jpg',
      'photos/onedrive/' + library + '/%2e%2e/file.jpg',
      'photos/onedrive/' + library + '/file.svg', 'https://example.com/file.jpg'
    ]) assert.throws(() => api.manifestImages(manifest([image]), library), image);
  }
  assert.throws(() => api.manifestImages(manifest([]), null));
});

for (const library of ['instagram', 'new-library_2']) {
  test(library + ' unavailable or empty stays blank without Spotify fallback', () => {
    const f = fixture(library); f.controller.start();
    assertBlank(f);
    f.requests[0].failure(Error('offline'));
    assertBlank(f);
    assert.equal(f.timers.size, 1);
    f.controller.refresh(); f.requests[1].success(manifest([]));
    assertBlank(f);
    assert.deepEqual(JSON.parse(f.data[cacheKey(library)]).images, []);
    assert.equal(f.timers.size, 1);
    assert.equal([...f.timers.values()][0].ms, 60000);
    f.controller.stop();
  });

  test(library + ' rejects Spotify, mixed and unsafe caches even under its own key', () => {
    for (const images of [[photo('spotify')], [photo(library), photo('spotify')], ['../secret.jpg']]) {
      const cached = JSON.stringify(manifest(images));
      const f = fixture(library, {[cacheKey(library)]: cached}); f.controller.start();
      assertBlank(f);
      assert.deepEqual(f.reads, [cacheKey(library)]);
      f.requests[0].success(manifest(images));
      assertBlank(f);
      assert.equal(f.data[cacheKey(library)], cached);
      assert.deepEqual(f.writes, []);
      f.controller.stop();
    }
  });

  test(library + ' never reads Spotify or legacy shared cache', () => {
    const cached = JSON.stringify(manifest([photo('spotify')]));
    const f = fixture(library, {
      [cacheKey('spotify')]: cached,
      'shiftingtiles-public-gallery-v1': cached,
      'shiftingtiles-public-gallery-v1:gallery.json': cached,
      'shared-cache': cached
    });
    f.controller.start(); assertBlank(f);
    assert.deepEqual(f.reads, [cacheKey(library)]);
    f.controller.stop();
  });

  test(library + ' empty publication clears last good photos and never restores Spotify', () => {
    const f = fixture(library, {[cacheKey(library)]: JSON.stringify(manifest([photo(library)]))});
    f.controller.start();
    assert.deepEqual(f.controller.current(), {images: [photo(library)], source: 'cached-onedrive'});
    f.requests[0].success(manifest([])); assertBlank(f);
    f.controller.refresh(); f.requests[1].failure(Error('offline')); assertBlank(f);
    const restarted = fixture(library, f.data); restarted.controller.start(); assertBlank(restarted);
    f.controller.stop(); restarted.controller.stop();
  });

  test(library + ' rejects foreign response without erasing or poisoning last good photos', () => {
    const f = fixture(library); f.controller.start();
    f.requests[0].success(manifest([photo(library)]));
    const cache = f.data[cacheKey(library)];
    f.controller.refresh(); f.requests[1].success(manifest([photo(library), photo('spotify')]));
    assert.deepEqual(f.controller.current().images, [photo(library)]);
    assert.equal(f.data[cacheKey(library)], cache);
    assert.deepEqual(f.writes, [cacheKey(library)]);
    f.controller.stop();
  });

  test(library + ' 304 keeps scoped cache and minute timer without another image update', () => {
    const f = fixture(library); f.controller.start();
    f.requests[0].success(manifest([photo(library)]));
    const cache = f.data[cacheKey(library)], count = f.updates.length;
    f.controller.refresh(); f.requests[1].success(undefined, true);
    assert.deepEqual(f.controller.current().images, [photo(library)]);
    assert.equal(f.data[cacheKey(library)], cache);
    assert.equal(f.updates.length, count);
    assert.equal(f.timers.size, 1);
    assert.equal([...f.timers.values()][0].ms, 60000);
    f.controller.stop();
  });
}

test('Spotify also rejects foreign caches and responses while preserving its existing fallback', () => {
  const f = fixture('spotify', {[cacheKey('spotify')]: JSON.stringify(manifest([photo('instagram')]))});
  f.controller.start();
  assert.deepEqual(f.controller.current().images, ['f1.jpeg', 'f2.jpeg']);
  f.requests[0].success(manifest([photo('instagram')]));
  assert.deepEqual(f.controller.current().images, ['f1.jpeg', 'f2.jpeg']);
  assert.deepEqual(f.writes, []);
  f.controller.stop();
});

test('shared storage holds independent last-good caches for both libraries', () => {
  const data = {}, spotify = fixture('spotify', data), instagram = fixture('instagram', data);
  spotify.controller.start(); instagram.controller.start();
  spotify.requests[0].success(manifest([photo('spotify')]));
  instagram.requests[0].success(manifest([photo('instagram')]));
  assert.deepEqual(Object.keys(data).sort(), [cacheKey('instagram'), cacheKey('spotify')].sort());
  for (const f of [spotify, instagram]) f.controller.stop();
  for (const library of ['spotify', 'instagram']) {
    const f = fixture(library, data); f.controller.start();
    assert.deepEqual(f.controller.current().images, [photo(library)]);
    f.controller.stop();
  }
});

test('invalid selection is blank and does not read, fetch, write or schedule', () => {
  for (const library of [null, '', '../spotify', 'Spotify', 'spotify\n']) {
    const f = fixture(library); f.controller.start(); f.controller.start(); f.controller.refresh();
    assertBlank(f);
    assert.equal(f.updates.length, 1);
    assert.equal(f.reads.length, 0);
    assert.equal(f.writes.length, 0);
    assert.equal(f.requests.length, 0);
    assert.equal(f.timers.size, 0);
    f.controller.stop();
  }
});

test('blocked storage cannot trigger non-Spotify bundled fallback', () => {
  const f = fixture('instagram', {}, true); f.controller.start();
  assertBlank(f); f.requests[0].failure(Error('offline')); assertBlank(f);
  f.controller.refresh(); f.requests[1].success(manifest([photo('instagram')]));
  assert.deepEqual(f.controller.current().images, [photo('instagram')]);
  f.controller.stop();
});

test('stop/restart ignores old callbacks and duplicate responses without duplicating polling timers', () => {
  const f = fixture('instagram'); f.controller.start(); const old = f.requests[0];
  f.controller.stop(); f.controller.start();
  old.success(manifest([photo('instagram')])); old.failure(Error('late'));
  assertBlank(f); assert.equal(f.timers.size, 0);
  const request = f.requests[1]; request.success(manifest([photo('instagram')]));
  const count = f.updates.length;
  request.success(manifest([])); request.failure(Error('duplicate'));
  assert.deepEqual(f.controller.current().images, [photo('instagram')]);
  assert.equal(f.updates.length, count);
  assert.equal(f.timers.size, 1);
  f.controller.stop();
});
