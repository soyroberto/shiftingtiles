const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
// Set NODE_PATH to the local test-tool installation; these tests never install dependencies.
const {JSDOM} = require('jsdom');
const ROOT = path.resolve(__dirname, '..');
const photo = 'photos/onedrive/spotify/' + 'a'.repeat(64) + '.jpg';
const bundled = Array.from({length: 10}, (_, i) => 'f' + (i + 1) + '.jpeg');

function manifestPhotos(library, count = 10) {
  return Array.from({length: count}, (_, i) => 'photos/onedrive/' + library + '/' +
    i.toString(16).padStart(64, '0') + '.png');
}
const spotifyPhotos = [photo].concat(manifestPhotos('spotify', 9));
const instagramPhotos = manifestPhotos('instagram');

function fixture(search = '') {
  const dom = new JSDOM('<!doctype html><body><div class="gallery"></div></body>', {
    url: 'https://saauxfotos.z26.web.core.windows.net/' + search,
    runScripts: 'outside-only', pretendToBeVisual: true
  });
  const w = dom.window;
  w.eval(fs.readFileSync(path.join(ROOT, 'jquery-1.8.3.js'), 'utf8'));
  const timers = new Map(); let id = 0;
  w.setTimeout = (fn, ms) => { timers.set(++id, {fn, ms}); return id; };
  w.clearTimeout = id => timers.delete(id);
  const requests = [];
  w.Image = class {
    constructor() { this.naturalWidth = 1600; this.naturalHeight = 900; }
    set src(value) { this.url = value; requests.push(this); }
  };
  w.Math.random = () => 0.999999;
  for (const file of ['album-queue.js', 'shiftingtiles.js', 'gallery-source.js',
    'gallery-layout.js', 'gallery-player.js']) {
    if (fs.existsSync(path.join(ROOT, file))) w.eval(fs.readFileSync(path.join(ROOT, file), 'utf8'));
  }
  const $ = w.jQuery;
  return {dom, w, $, gallery: $('.gallery'), timers, requests};
}

function succeed(requests) {
  for (const request of requests.slice()) if (request.onload) request.onload();
}

function animationTimers(f) {
  return [...f.timers.values()].filter(timer => timer.ms === 5000);
}

function close(f, controller) {
  if (controller) controller.stop();
  const playback = f.gallery.data('shiftingtiles-playback');
  if (playback) playback.destroy();
  f.dom.window.close();
}

test('animation receives added URLs without creating a second animation timer', () => {
  const f = fixture();
  const initial = bundled.slice(0, 8);
  f.gallery.shiftingtiles(initial, {fallbackPhotos: ['foreign.jpeg']});
  succeed(f.requests);
  assert.equal(f.gallery.find('.single, .dual > div').length, 8);
  assert.equal(animationTimers(f).length, 1);
  f.gallery.trigger('st-photos-update', [initial.concat(photo)]);
  f.gallery.data('shiftingtiles-playback').frame();
  assert.equal(f.requests.at(-1).url, photo);
  assert.equal(f.gallery.find('.single, .dual > div').length, 8);
  succeed(f.requests);
  assert.equal(animationTimers(f).length, 1);
  close(f);
});

test('a failed OneDrive photo retries from the same album instead of a foreign fallback', () => {
  const f = fixture();
  f.gallery.shiftingtiles(spotifyPhotos, {fallbackPhotos: ['foreign.jpeg']});
  const first = f.requests[0]; first.onerror();
  const replacement = f.requests.at(-1);
  assert.notEqual(replacement.url, first.url);
  assert.ok(spotifyPhotos.includes(replacement.url));
  assert.ok(f.requests.every(request => spotifyPhotos.includes(request.url)));
  replacement.onload();
  assert.ok(f.gallery.find('.single').first().attr('style').includes(replacement.url));
  close(f);
});

test('mount uses the manifest and later refreshes without restarting the plugin', () => {
  const f = fixture(); let resolve, reject, ajaxOptions;
  f.$.ajax = options => {
    ajaxOptions = options;
    return {done(fn) {resolve = fn; return this;}, fail(fn) {reject = fn; return this;}};
  };
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, bundled);
  assert.equal(ajaxOptions.url, 'photos/onedrive/spotify/gallery.json');
  assert.equal(ajaxOptions.ifModified, true);
  assert.equal(ajaxOptions.cache, false);
  assert.equal(ajaxOptions.timeout, 10000);
  assert.equal(f.gallery.attr('data-photo-source'), 'bundled');
  resolve({version: 1, images: spotifyPhotos}); succeed(f.requests);
  assert.equal(f.gallery.attr('data-photo-source'), 'onedrive');
  const playback = f.gallery.data('shiftingtiles-playback');
  controller.refresh(); reject();
  assert.equal(f.gallery.attr('data-photo-source'), 'onedrive');
  assert.equal(f.gallery.data('shiftingtiles-playback'), playback);
  assert.equal(f.gallery.children('.row').length, 2);
  assert.equal(animationTimers(f).length, 1);
  close(f, controller);
});

test('space and arrow controls preserve the original animation behavior after initial readiness', () => {
  const f = fixture();
  f.gallery.shiftingtiles(bundled); succeed(f.requests);
  const before = f.requests.length;
  f.$(f.w.document.body).trigger(f.$.Event('keydown', {keyCode: 32}));
  assert.ok(f.requests.length > before);
  f.$(f.w.document.body).trigger(f.$.Event('keydown', {keyCode: 38}));
  assert.ok(f.gallery.hasClass('leave'));
  close(f);
});

function stubManifest(f) {
  const calls = [];
  f.$.ajax = options => {
    const call = {options}; calls.push(call);
    return {done(fn) {call.resolve = fn; return this;}, fail(fn) {call.reject = fn; return this;}};
  };
  return calls;
}

for (const library of ['instagram', 'new-library']) {
  test(library + ' starts blank without animation and cannot fetch Spotify fallback', () => {
    const f = fixture('?library=' + library), calls = stubManifest(f);
    const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, bundled, {manifestUrl: 'gallery.json'});
    assert.equal(calls[0].options.url, 'photos/onedrive/' + library + '/gallery.json');
    assert.equal(calls[0].options.ifModified, true);
    assert.equal(f.gallery.attr('data-photo-library'), library);
    assert.equal(f.gallery.attr('data-photo-source'), 'empty');
    assert.equal(f.gallery.css('visibility'), 'hidden');
    assert.equal(f.gallery.children().length, 0);
    assert.equal(f.requests.length, 0);
    assert.equal(animationTimers(f).length, 0);
    calls[0].reject();
    controller.refresh(); calls[1].resolve({version: 1, images: spotifyPhotos});
    assert.equal(f.requests.length, 0);
    assert.equal(f.gallery.children().length, 0);
    controller.refresh(); calls[2].resolve({version: 1, images: []});
    assert.equal(f.gallery.attr('data-photo-source'), 'empty');
    assert.equal(f.requests.length, 0);
    assert.equal([...f.timers.values()].filter(timer => timer.ms === 60000).length, 1);
    close(f, controller);
  });
}

test('Instagram first images initialize once; 304, empty and recovery keep at most one animation timer', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, bundled);
  calls[0].resolve({version: 1, images: instagramPhotos}); succeed(f.requests);
  assert.equal(f.gallery.css('visibility'), 'visible');
  assert.equal(f.gallery.children('.row').length, 2);
  assert.equal(animationTimers(f).length, 1);
  controller.refresh(); calls[1].resolve(undefined, 'notmodified');
  assert.equal(f.gallery.attr('data-photo-source'), 'onedrive');
  assert.equal(animationTimers(f).length, 1);
  controller.refresh(); calls[2].resolve({version: 1, images: []});
  assert.equal(f.gallery.attr('data-photo-source'), 'empty');
  assert.equal(f.gallery.css('visibility'), 'hidden');
  for (const tile of f.gallery.find('.single, .dual > div').toArray()) {
    assert.equal(f.$(tile).css('background-image'), 'none');
  }
  const count = f.requests.length;
  assert.equal(f.gallery.data('shiftingtiles-playback').frame(), false);
  assert.equal(f.requests.length, count);
  assert.equal(animationTimers(f).length, 0);
  controller.refresh(); calls[3].resolve({version: 1, images: instagramPhotos}); succeed(f.requests);
  assert.equal(f.gallery.css('visibility'), 'visible');
  assert.equal(f.gallery.children('.row').length, 2);
  assert.equal(animationTimers(f).length, 1);
  f.gallery.data('shiftingtiles-playback').frame(); succeed(f.requests);
  assert.ok(f.requests.every(request => instagramPhotos.includes(request.url)));
  close(f, controller);
});

test('empty publication invalidates pending photos before late onload or retry callbacks', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, bundled);
  calls[0].resolve({version: 1, images: instagramPhotos});
  const callbacks = f.requests.map(request => ({load: request.onload, fail: request.onerror}));
  controller.refresh(); calls[1].resolve({version: 1, images: []});
  for (let i = 0; i < callbacks.length; i++) {
    if (i % 2) callbacks[i].load();
    else callbacks[i].fail();
  }
  assert.equal(f.requests.length, callbacks.length);
  assert.equal(f.gallery.css('visibility'), 'hidden');
  for (const tile of f.gallery.find('.single, .dual > div').toArray()) {
    assert.equal(f.$(tile).css('background-image'), 'none');
  }
  assert.ok(f.requests.every(request => instagramPhotos.includes(request.url)));
  close(f, controller);
});

test('Instagram image failures retry only URLs from the selected album', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, bundled);
  const source = manifestPhotos('instagram', 12);
  calls[0].resolve({version: 1, images: source});
  for (const request of f.requests.slice()) if (request.onerror) request.onerror();
  assert.ok(f.requests.length >= 8);
  assert.ok(f.requests.every(request => source.includes(request.url)));
  close(f, controller);
});

test('valid albums with fewer than eight photos wait instead of repeating to fill the gallery', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, bundled);
  calls[0].resolve({version: 1, images: manifestPhotos('instagram', 7)});
  assert.equal(f.requests.length, 0);
  assert.equal(f.gallery.children('.row').length, 0);
  assert.equal(animationTimers(f).length, 0);
  close(f, controller);
});

test('invalid explicit selection mounts a blank gallery without Ajax or timers', () => {
  for (const search of ['?library=', '?library=../spotify', '?library=spotify&library=instagram']) {
    const f = fixture(search), calls = stubManifest(f);
    const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, bundled);
    assert.equal(f.gallery.attr('data-photo-source'), 'empty');
    assert.equal(f.gallery.children().length, 0);
    assert.equal(f.requests.length, 0);
    assert.equal(calls.length, 0);
    assert.equal(f.timers.size, 0);
    close(f, controller);
  }
});

test('duplicate mount returns the existing source without duplicating plugin or poll timers', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, bundled);
  calls[0].resolve({version: 1, images: instagramPhotos}); succeed(f.requests);
  assert.equal(f.w.ShiftingTilesPhotos.mount(f.gallery, bundled), controller);
  assert.equal(calls.length, 1);
  assert.equal(f.gallery.children('.row').length, 2);
  assert.equal(animationTimers(f).length, 1);
  assert.equal([...f.timers.values()].filter(timer => timer.ms === 60000).length, 1);
  close(f, controller);
});

test('inline page initializer honors the URL selection instead of the legacy manifest alias', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
  f.w.eval(html.match(/<script>\s*([\s\S]*?)<\/script>/)[1]);
  assert.equal(calls[0].options.url, 'photos/onedrive/instagram/gallery.json');
  assert.equal(f.gallery.attr('data-photo-source'), 'empty');
  assert.equal(f.requests.length, 0);
  close(f, f.gallery.data('shiftingtiles-source'));
});
