const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const ROOT = path.resolve(__dirname, '..');

function photos(count) {
  return Array.from({length: count}, (_, i) => 'photos/album/photo-' + i + '.jpg');
}

function fixture(count = 20) {
  const dom = new JSDOM('<!doctype html><body><div class="gallery"></div></body>', {
    url: 'https://example.test/', runScripts: 'outside-only', pretendToBeVisual: true
  });
  const w = dom.window;
  w.eval(fs.readFileSync(path.join(ROOT, 'jquery-1.8.3.js'), 'utf8'));
  let hidden = false, nextTimer = 0;
  Object.defineProperty(w.document, 'hidden', {configurable: true, get: () => hidden});
  const timers = new Map(), requests = [], events = [];
  w.setTimeout = (fn, ms) => { timers.set(++nextTimer, {fn, ms}); return nextTimer; };
  w.clearTimeout = id => timers.delete(id);
  w.Image = class {
    constructor() { this.naturalWidth = 1600; this.naturalHeight = 900; }
    set src(url) { this.url = url; requests.push(this); }
  };
  w.Math.random = () => 0.999999;
  w.eval(fs.readFileSync(path.join(ROOT, 'album-queue.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(ROOT, 'shiftingtiles.js'), 'utf8'));
  const $ = w.jQuery, gallery = $('.gallery'), source = photos(count);
  for (const name of ['st-image-ready', 'st-initial-ready', 'st-animate-before', 'st-animate',
    'st-animate-after', 'st-galleryrestart']) gallery.on(name, () => events.push(name));
  gallery.shiftingtiles(source, {fallbackPhotos: ['foreign/fallback.jpg']});
  const api = gallery.data('shiftingtiles-playback');
  return {
    dom, w, $, gallery, source, timers, requests, events, api,
    tiles: () => gallery.find('.single, .dual > div'),
    load(request) { assert.equal(typeof request.onload, 'function'); request.onload(); },
    fail(request) { assert.equal(typeof request.onerror, 'function'); request.onerror(); },
    ready() { requests.slice().forEach(request => { if (request.onload) request.onload(); }); },
    nominalTimers: () => [...timers.values()].filter(timer => timer.ms === 5000),
    tick() {
      const item = [...timers.entries()].find(([, timer]) => timer.ms === 5000);
      assert.ok(item, 'one nominal frame timer must exist');
      timers.delete(item[0]); item[1].fn();
    },
    frame(kind = 'single') {
      if (kind === 'duo') {
        const row = gallery.children('.row').first();
        row.prepend(row.children('.dual').first());
      }
      const boxes = gallery.find('.single:not(:last-child), .dual:not(:last-child)');
      const selected = boxes.filter(kind === 'duo' ? '.dual' : '.single').first();
      const index = boxes.toArray().indexOf(selected[0]);
      w.Math.random = () => (Math.max(0, index) + 0.01) / boxes.length;
      return api.frame();
    },
    end() { gallery.find('.disappear').trigger('animationend'); },
    hide(value) { hidden = value; $(w.document).trigger('visibilitychange'); },
    close() { api.destroy(); dom.window.close(); }
  };
}

function uniqueVisible(f) {
  const visible = Array.from(f.api.stats().visibleURLs);
  assert.equal(new Set(visible).size, visible.length, 'incoming and outgoing photos must never duplicate');
  return visible.sort();
}

test('443-photo album only constructs eight initial Images and publishes matching metadata', () => {
  const f = fixture(443);
  assert.equal(f.requests.length, 8);
  assert.equal(new Set(f.requests.map(request => request.url)).size, 8);
  assert.equal(f.api.stats().seenCount, 0);
  assert.equal(f.api.stats().reservedCount, 8);
  assert.equal(f.api.stats().loading, true);
  assert.equal(f.tiles().length, 8);
  f.ready();
  assert.equal(f.requests.length, 8);
  assert.equal(f.api.stats().seenCount, 8);
  assert.equal(f.api.stats().reservedCount, 0);
  assert.equal(f.api.stats().loading, false);
  assert.equal(f.nominalTimers().length, 1);
  assert.equal(f.events.filter(event => event === 'st-image-ready').length, 8);
  assert.equal(f.events.filter(event => event === 'st-initial-ready').length, 1);
  f.tiles().each(function() {
    const tile = f.$(this), url = tile.data('st-photo-url');
    assert.ok(f.source.includes(url));
    assert.ok(tile.css('background-image').includes(url));
    assert.equal(tile.data('st-natural-width'), 1600);
    assert.equal(tile.data('st-natural-height'), 900);
    assert.equal(tile.data('st-image-request'), tile.data('st-image-loaded-request'));
  });
  uniqueVisible(f); f.close();
});

test('Duo preloads exactly two images before cloning, committing or emitting animation', () => {
  const f = fixture(10); f.ready();
  const old = uniqueVisible(f);
  f.gallery.on('st-animate-before', () => {
    assert.equal(f.tiles().length, 8);
    assert.ok(f.requests.slice(8).every(request => request.onload === null));
  });
  assert.equal(f.frame('duo'), true);
  assert.equal(f.requests.length, 10);
  assert.equal(f.tiles().length, 8);
  assert.equal(f.api.stats().reservedCount, 2);
  assert.equal(f.api.stats().seenCount, 8);
  assert.ok(f.requests.slice(8).every(request => !old.includes(request.url)));
  f.load(f.requests[8]);
  assert.equal(f.tiles().length, 8);
  assert.equal(f.events.includes('st-animate'), false);
  f.load(f.requests[9]);
  assert.equal(f.tiles().length, 10);
  assert.equal(f.api.stats().seenCount, 10);
  assert.equal(f.api.stats().reservedCount, 0);
  assert.equal(f.api.stats().animating, true);
  assert.equal(f.api.stats().loading, false);
  uniqueVisible(f);
  const incoming = f.gallery.find('.disappear').parent().children().last();
  assert.deepEqual(Array.from(incoming.children().toArray(), node => f.$(node).data('st-photo-url')), f.source.slice(8));
  incoming.children().each(function() {
    const tile = f.$(this);
    assert.equal(tile.data('st-image-request'), tile.data('st-image-loaded-request'));
    assert.equal(tile.data('st-natural-width'), 1600);
  });
  f.end();
  assert.equal(f.tiles().length, 8);
  assert.equal(f.api.stats().animating, false);
  assert.equal(f.events.filter(event => event === 'st-animate-after').length, 1);
  assert.equal(f.nominalTimers().length, 1);
  f.close();
});

test('nine-photo album falls back from Duo to Single and excludes visible URLs across reset', () => {
  const f = fixture(9); f.ready();
  assert.equal(f.frame('duo'), true);
  assert.equal(f.requests.length, 9);
  assert.equal(f.api.stats().reservedCount, 1);
  f.load(f.requests[8]);
  assert.equal(f.gallery.find('.disappear').hasClass('single'), true);
  assert.equal(f.tiles().length, 9);
  assert.equal(f.api.stats().seenCount, 9);
  assert.equal(f.frame(), false);
  f.end();
  const visible = uniqueVisible(f);
  assert.equal(f.frame('duo'), true);
  assert.equal(f.requests.length, 10);
  assert.ok(!visible.includes(f.requests[9].url));
  assert.equal(f.api.stats().cycle, 2);
  assert.equal(f.events.filter(event => event === 'st-galleryrestart').length, 1);
  f.load(f.requests[9]); uniqueVisible(f); f.end(); f.close();
});

test('exactly eight URLs stay fixed; fewer than eight wait without duplicating photos', () => {
  const f = fixture(8); f.ready();
  const before = uniqueVisible(f);
  for (let i = 0; i < 20; i++) {
    f.tick();
    f.$(f.w.document.body).trigger(f.$.Event('keydown', {keyCode: 32}));
    assert.equal(f.nominalTimers().length, 1);
  }
  assert.equal(f.requests.length, 8);
  assert.deepEqual(uniqueVisible(f), before);
  assert.equal(f.api.stats().cycle, 1);
  assert.equal(f.gallery.find('.disappear').length, 0);
  f.close();
  const short = fixture(7);
  assert.equal(short.requests.length, 0);
  assert.equal(short.tiles().length, 0);
  assert.equal(short.nominalTimers().length, 0);
  short.close();
});

test('space and repeated ticks never overlap pending load or active animation', () => {
  const f = fixture(20); f.ready();
  f.frame('duo');
  for (let i = 0; i < 5; i++) {
    f.tick();
    f.$(f.w.document.body).trigger(f.$.Event('keydown', {keyCode: 32}));
    assert.equal(f.requests.length, 10);
    assert.equal(f.tiles().length, 8);
    assert.equal(f.nominalTimers().length, 1);
  }
  f.load(f.requests[8]); f.load(f.requests[9]);
  for (let i = 0; i < 5; i++) {
    f.tick();
    assert.equal(f.frame(), false);
    assert.equal(f.requests.length, 10);
    assert.equal(f.tiles().length, 10);
    assert.equal(f.nominalTimers().length, 1);
    uniqueVisible(f);
  }
  f.end(); f.frame();
  assert.equal(f.requests.length, 11);
  assert.equal(f.tiles().length, 8);
  f.close();
});

test('initial errors retry only different URLs from the same album', () => {
  const f = fixture(12);
  const failed = f.requests[0].url;
  f.fail(f.requests[0]);
  assert.equal(f.requests.length, 9);
  assert.notEqual(f.requests[8].url, failed);
  assert.ok(f.source.includes(f.requests[8].url));
  assert.equal(new Set(f.requests.map(request => request.url)).size, 9);
  f.ready();
  assert.equal(f.api.stats().failedCount, 1);
  assert.equal(f.api.stats().seenCount, 8);
  assert.equal(f.tiles().length, 8);
  uniqueVisible(f); f.close();
});

test('failure of the ninth URL never retries that same URL immediately across a reset', () => {
  const f = fixture(9); f.ready();
  const visible = uniqueVisible(f);
  f.frame('duo');
  f.fail(f.requests[8]);
  assert.equal(f.requests.length, 9);
  assert.equal(f.api.stats().cycle, 1);
  assert.equal(f.api.stats().failedCount, 1);
  assert.equal(f.api.stats().reservedCount, 0);
  assert.deepEqual(uniqueVisible(f), visible);
  assert.equal(f.tiles().length, 8);
  f.close();
});

test('a load deadline retries within the album and invalidates the timed-out callback', () => {
  const f = fixture(12); f.ready(); f.frame();
  const timedOut = f.requests[8], stale = timedOut.onload;
  const item = [...f.timers.entries()].find(([, timer]) => timer.ms === 20000);
  assert.ok(item);
  f.timers.delete(item[0]); item[1].fn();
  assert.equal(f.requests.length, 10);
  assert.notEqual(f.requests[9].url, timedOut.url);
  assert.ok(f.source.includes(f.requests[9].url));
  stale();
  assert.equal(f.tiles().length, 8);
  assert.equal(f.api.stats().seenCount, 8);
  f.load(f.requests[9]); uniqueVisible(f); f.end();
  assert.equal(f.api.stats().seenCount, 9);
  f.close();
});

test('three failed attempts retain the old Duo and release its successful but uncommitted sibling', () => {
  const f = fixture(12); f.ready();
  const visible = uniqueVisible(f);
  f.frame('duo');
  f.load(f.requests[9]);
  f.fail(f.requests[8]);
  f.fail(f.requests[10]);
  f.fail(f.requests[11]);
  assert.equal(f.requests.length, 12);
  assert.equal(f.api.stats().failedCount, 3);
  assert.equal(f.api.stats().seenCount, 8);
  assert.equal(f.api.stats().reservedCount, 0);
  assert.equal(f.api.stats().loading, false);
  assert.equal(f.api.stats().animating, false);
  assert.deepEqual(uniqueVisible(f), visible);
  assert.ok(f.requests.every(request => f.source.includes(request.url)));
  assert.equal(f.frame('duo'), true);
  assert.equal(f.requests.at(-1).url, f.source[9]);
  f.load(f.requests.at(-1)); uniqueVisible(f); f.end(); f.close();
});

for (const phase of ['initial', 'transition']) {
  test('empty update clears photos and invalidates stale ' + phase + ' callbacks', () => {
    const f = fixture(20);
    if (phase === 'transition') { f.ready(); f.frame('duo'); }
    const callbacks = f.requests.filter(request => request.onload).map(request => [request.onload, request.onerror]);
    const count = f.requests.length;
    f.gallery.trigger('st-photos-update', [[]]);
    callbacks.forEach(([load, fail]) => { load(); fail(); });
    assert.equal(f.requests.length, count);
    assert.equal(f.api.stats().albumSize, 0);
    assert.equal(f.api.stats().seenCount, 0);
    assert.equal(f.api.stats().reservedCount, 0);
    assert.equal(f.api.stats().loading, false);
    assert.equal(f.api.stats().animating, false);
    assert.equal(f.nominalTimers().length, 0);
    f.tiles().each(function() {
      const tile = f.$(this);
      assert.equal(tile.css('background-image'), 'none');
      assert.equal(tile.data('st-photo-url'), undefined);
      assert.equal(tile.data('st-image-request'), undefined);
      assert.equal(tile.data('st-image-loaded-request'), undefined);
    });
    const replacement = photos(10).map(url => url.replace('album', 'replacement'));
    f.gallery.trigger('st-photos-update', [replacement]);
    assert.equal(f.requests.length, count + 8);
    f.ready();
    assert.equal(f.api.stats().seenCount, 8);
    assert.equal(f.api.stats().visibleURLs.length, 8);
    assert.ok(Array.from(f.api.stats().visibleURLs).every(url => replacement.includes(url)));
    assert.equal(f.events.filter(event => event === 'st-initial-ready').length, phase === 'transition' ? 2 : 1);
    assert.equal(f.nominalTimers().length, 1);
    f.close();
  });
}

test('nonempty refresh preserves loaded history and an existing pending reservation', () => {
  const f = fixture(10); f.ready(); f.frame();
  const request = f.requests[8];
  f.gallery.trigger('st-photos-update', [f.source.concat('photos/album/new.jpg').reverse()]);
  assert.equal(f.requests.length, 9);
  assert.equal(f.api.stats().seenCount, 8);
  assert.equal(f.api.stats().reservedCount, 1);
  assert.equal(f.api.stats().cycle, 1);
  f.load(request); f.end();
  assert.equal(f.api.stats().seenCount, 9);
  f.frame();
  assert.equal(f.requests.at(-1).url, f.source[9]);
  assert.equal(f.api.stats().cycle, 1);
  f.close();
});

test('refresh cancels removed pending URLs without allowing their late onload to commit', () => {
  const f = fixture(10); f.ready(); f.frame();
  const stale = f.requests[8].onload;
  f.gallery.trigger('st-photos-update', [f.source.filter(url => url !== f.requests[8].url)]);
  assert.equal(f.api.stats().loading, false);
  assert.equal(f.api.stats().reservedCount, 0);
  assert.equal(f.api.stats().seenCount, 8);
  stale();
  assert.equal(f.tiles().length, 8);
  assert.equal(f.api.stats().seenCount, 8);
  f.frame('duo');
  assert.equal(f.requests.at(-1).url, f.source[9]);
  f.load(f.requests.at(-1)); uniqueVisible(f); f.end(); f.close();
});

test('visibility pause never appends a ready clone while hidden and resumes with one timer', () => {
  const f = fixture(20); f.ready(); f.frame('duo');
  f.hide(true);
  assert.equal(f.nominalTimers().length, 0);
  f.load(f.requests[8]); f.load(f.requests[9]);
  assert.equal(f.tiles().length, 8);
  assert.equal(f.api.stats().seenCount, 8);
  assert.equal(f.api.stats().loading, true);
  assert.equal(f.frame(), false);
  f.hide(false);
  assert.equal(f.tiles().length, 10);
  assert.equal(f.api.stats().seenCount, 10);
  assert.equal(f.nominalTimers().length, 1);
  f.hide(true);
  assert.equal(f.tiles().length, 8);
  assert.equal(f.api.stats().animating, false);
  assert.equal(f.nominalTimers().length, 0);
  f.hide(false);
  assert.equal(f.nominalTimers().length, 1);
  uniqueVisible(f); f.close();
});

test('443-photo playback completes whole odd cycles with eight unique photos', () => {
  const f = fixture(443); f.ready();
  let randomState = 123456;
  f.w.Math.random = () => {
    randomState = (Math.imul(randomState, 1664525) + 1013904223) >>> 0;
    return randomState / 4294967296;
  };
  let cycle = 1, seen = new Set(f.api.stats().visibleURLs), completed = 0;
  for (let step = 0; step < 1500 && completed < 2; step++) {
    const before = f.requests.length;
    assert.equal(f.api.frame(), true, JSON.stringify(f.api.stats()));
    const nextCycle = f.api.stats().cycle;
    if (nextCycle !== cycle) {
      assert.equal(seen.size, 443, 'all photos were displayed before the next cycle');
      completed++; cycle = nextCycle; seen = new Set();
    }
    for (const request of f.requests.slice(before)) {
      assert.equal(seen.has(request.url), false, 'no repeated URL within a cycle');
      seen.add(request.url); f.load(request);
    }
    uniqueVisible(f); f.end();
    assert.equal(uniqueVisible(f).length, 8);
    f.gallery.children('.row').each(function () {
      assert.equal(f.$(this).find('.single,.dual>div').length, 4);
    });
    assert.equal(f.nominalTimers().length, 1);
  }
  assert.equal(completed, 2); f.close();
});

test('duplicate initialization, arrow control and destroy preserve lifecycle boundaries', () => {
  const f = fixture(20); f.ready();
  f.gallery.shiftingtiles(photos(100));
  assert.equal(f.gallery.data('shiftingtiles-playback'), f.api);
  assert.equal(f.requests.length, 8);
  assert.equal(f.gallery.children('.row').length, 2);
  assert.equal(f.nominalTimers().length, 1);
  f.$(f.w.document.body).trigger(f.$.Event('keydown', {keyCode: 38}));
  assert.equal(f.gallery.hasClass('leave'), true);
  f.frame();
  const stale = f.requests[8].onload;
  f.api.destroy(); stale();
  assert.equal(f.nominalTimers().length, 0);
  assert.equal(f.api.stats().reservedCount, 0);
  assert.equal(f.gallery.data('shiftingtiles-playback'), undefined);
  const before = f.requests.length;
  f.$(f.w.document.body).trigger(f.$.Event('keydown', {keyCode: 32}));
  assert.equal(f.requests.length, before);
  f.dom.window.close();
});
