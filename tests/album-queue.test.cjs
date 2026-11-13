const test = require('node:test');
const assert = require('node:assert/strict');
const {performance} = require('node:perf_hooks');
const {create} = require('../album-queue.js');

function photos(count) {
  return Array.from({length: count}, (_, i) => 'album/photo-' + i + '.jpg');
}

function seededRandom(seed = 1234567) {
  return () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };
}

function show(queue, count = 1, excluded = []) {
  const urls = queue.reserve(count, excluded);
  urls.forEach(url => assert.equal(queue.commit(url), true));
  return urls;
}

test('UMD queue normalizes unique nonempty URLs without mutating input', () => {
  const input = [' a ', 'a', '', null, 'b', 42, ' __proto__ '];
  const queue = create(input, () => 0.5);
  assert.equal(queue.stats().albumSize, 3);
  assert.deepEqual(input, [' a ', 'a', '', null, 'b', 42, ' __proto__ ']);
  assert.deepEqual(new Set(show(queue, 3)), new Set(['a', 'b', '__proto__']));
});

test('reserve is atomic and never consumes seen before commit', () => {
  const queue = create(['a', 'b', 'c'], () => 0.99);
  assert.deepEqual(queue.reserve(2, ['b', 'c']), []);
  assert.equal(queue.stats().reservedCount, 0);
  assert.deepEqual(queue.reserve(2), ['a', 'b']);
  assert.equal(queue.stats().seenCount, 0);
  const before = queue.stats();
  assert.deepEqual(queue.reserve(2), []);
  assert.deepEqual(queue.stats(), before);
  assert.equal(queue.commit('a'), true);
  assert.equal(queue.commit('a'), false);
  assert.equal(queue.commit('unknown'), false);
  assert.equal(queue.stats().seenCount, 1);
  assert.deepEqual(queue.reserve(1), ['c']);
  assert.equal(queue.stats().reservedCount, 2);
});

test('release makes a reservation available, fail releases it for the whole cycle', () => {
  const queue = create(['a', 'b', 'c'], () => 0.99);
  assert.deepEqual(queue.reserve(1), ['a']);
  assert.equal(queue.release('a'), true);
  assert.equal(queue.release('a'), false);
  assert.deepEqual(queue.reserve(1), ['a']);
  assert.equal(queue.fail('a'), true);
  assert.equal(queue.fail('a'), false);
  assert.equal(queue.stats().failedCount, 1);
  assert.equal(queue.stats().seenCount, 0);
  assert.equal(queue.stats().reservedCount, 0);
  assert.deepEqual(show(queue, 2), ['b', 'c']);
  assert.equal(queue.stats().cycle, 1);
  assert.deepEqual(queue.reserve(1, ['b', 'c']), ['a']);
  assert.equal(queue.stats().cycle, 2);
  assert.equal(queue.stats().failedCount, 0);
});

test('cycle reset excludes all visible URLs and stays atomic when count is impossible', () => {
  const queue = create(photos(10), seededRandom());
  const all = show(queue, 10);
  const visible = all.slice(0, 9);
  const before = queue.stats();
  assert.deepEqual(queue.reserve(2, visible), []);
  assert.deepEqual(queue.stats(), before);
  const next = queue.reserve(1, visible);
  assert.deepEqual(next, all.slice(9));
  assert.equal(queue.stats().cycle, 2);
  assert.equal(queue.stats().reservedCount, 1);
  const reserved = queue.stats();
  assert.deepEqual(queue.reserve(1, visible), []);
  assert.deepEqual(queue.stats(), reserved);
});

test('a two-photo request cannot reset while one unseen URL remains', () => {
  const queue = create(photos(9), seededRandom());
  const visible = show(queue, 8);
  const before = queue.stats();
  assert.deepEqual(queue.reserve(2, visible), []);
  assert.deepEqual(queue.stats(), before);
  const last = show(queue, 1, visible);
  assert.equal(last.length, 1);
  assert.equal(queue.stats().seenCount, 9);
  assert.equal(queue.stats().cycle, 1);
});

test('setImages preserves seen, failed and reserved URLs and admits additions without reset', () => {
  const queue = create(['a', 'b', 'c', 'd'], () => 0.99);
  assert.deepEqual(show(queue), ['a']);
  assert.deepEqual(queue.reserve(1), ['b']);
  queue.fail('b');
  assert.deepEqual(queue.reserve(1), ['c']);
  queue.setImages(['d', 'c', 'a', 'b', 'e', 'e']);
  assert.deepEqual(queue.stats(), {albumSize: 5, cycle: 1, seenCount: 1, reservedCount: 1, failedCount: 1});
  assert.deepEqual(show(queue, 2), ['d', 'e']);
  assert.equal(queue.commit('c'), true);
  assert.equal(queue.stats().seenCount, 4);
  queue.setImages(['a', 'b', 'e', 'f']);
  assert.deepEqual(queue.stats(), {albumSize: 4, cycle: 1, seenCount: 2, reservedCount: 0, failedCount: 1});
  assert.deepEqual(queue.reserve(1), ['f']);
});

test('removed reservations cannot commit after refresh or after empty publication', () => {
  const queue = create(['a', 'b', 'c'], () => 0.99);
  assert.deepEqual(queue.reserve(2), ['a', 'b']);
  queue.setImages(['b', 'c']);
  assert.equal(queue.commit('a'), false);
  assert.equal(queue.commit('b'), true);
  queue.reserve(1);
  queue.setImages([]);
  assert.equal(queue.commit('c'), false);
  assert.equal(queue.stats().reservedCount, 0);
  assert.equal(queue.stats().albumSize, 0);
  queue.setImages(['d']);
  assert.deepEqual(show(queue), ['d']);
});

test('invalid counts and pathological random functions are bounded', () => {
  for (const value of [NaN, Infinity, -10, 1, 10]) {
    const queue = create(photos(20), () => value);
    assert.equal(new Set(show(queue, 20)).size, 20);
  }
  const queue = create(photos(10));
  const before = queue.stats();
  for (const count of [0, -1, 1.5, '2', NaN, Infinity, 11]) assert.deepEqual(queue.reserve(count), []);
  assert.deepEqual(queue.stats(), before);
});

for (const size of [443, 10000]) {
  test(size + ' URLs complete three random cycles without visible or reserved repeats', t => {
    let randomCalls = 0;
    const rng = seededRandom(size);
    const start = performance.now();
    const queue = create(photos(size), () => { randomCalls++; return rng(); });
    const visible = [];
    const seenPerCycle = new Map();
    let completed = 0;
    while (completed < 3) {
      const before = queue.stats();
      let urls = queue.reserve(visible.length ? 2 : 8, visible);
      if (!urls.length) urls = queue.reserve(1, visible);
      assert.ok(urls.length);
      const cycle = queue.stats().cycle;
      if (!seenPerCycle.has(cycle)) seenPerCycle.set(cycle, new Set());
      const seen = seenPerCycle.get(cycle);
      for (const url of urls) {
        assert.ok(!visible.includes(url), 'visible URLs must be excluded across reset');
        assert.ok(!seen.has(url), 'never repeat within one cycle');
      }
      const reservedState = queue.stats();
      assert.equal(reservedState.seenCount, cycle === before.cycle ? before.seenCount : 0);
      // Concurrent reservations cannot steal these URLs, even if callers omit exclusions.
      const concurrent = queue.reserve(1, visible);
      assert.ok(concurrent.every(url => !urls.includes(url)));
      concurrent.forEach(url => queue.release(url));
      for (const url of urls) {
        queue.commit(url);
        seen.add(url);
        visible.push(url);
        if (visible.length > 8) visible.shift();
      }
      if (seen.size === size) completed++;
    }
    assert.equal(queue.stats().cycle, 3);
    assert.equal(queue.stats().seenCount, size);
    assert.equal(queue.stats().reservedCount, 0);
    assert.equal(randomCalls, 3 * (size - 1), 'one bounded Fisher-Yates shuffle per cycle');
    const elapsed = performance.now() - start;
    t.diagnostic(size + ' URLs / 3 cycles: ' + elapsed.toFixed(1) + ' ms; ' + randomCalls + ' RNG calls');
    assert.ok(elapsed < 10000, 'full traversal must remain bounded for 10,000 photos');
  });
}
