const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
// Set NODE_PATH to the local test-tool installation, or install jsdom in your test environment.
const {JSDOM} = require('jsdom');
const ROOT = path.resolve(__dirname, '..');
const photo = 'photos/onedrive/spotify/' + 'a'.repeat(64) + '.jpg';

function fixture(search = '') {
  const dom = new JSDOM('<!doctype html><body><div class="gallery"></div></body>', {
    url: 'https://saauxfotos.z26.web.core.windows.net/' + search, runScripts: 'outside-only'
  });
  const w = dom.window;
  w.eval(fs.readFileSync(path.join(ROOT, 'jquery-1.8.3.js'), 'utf8'));
  const timers = new Map(); let id = 0;
  w.setTimeout = (fn, ms) => { timers.set(++id, {fn, ms}); return id; };
  w.clearTimeout = id => timers.delete(id);
  const requests = [];
  w.Image = class {
    set src(value) { this.url = value; requests.push(this); }
  };
  w.eval(fs.readFileSync(path.join(ROOT, 'shiftingtiles.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(ROOT, 'gallery-source.js'), 'utf8'));
  const $ = w.jQuery;
  return {dom, w, $, gallery: $('.gallery'), timers, requests};
}

function succeed(requests) {
  for (const request of requests.slice()) if (request.onload) request.onload();
}

test('animation receives new URLs without creating a second animation timer', () => {
  const f = fixture();
  f.gallery.shiftingtiles(['f1.jpeg'], {fallbackPhotos: ['f2.jpeg']});
  succeed(f.requests);
  assert.equal(f.gallery.find('.single, .dual > div').length, 8);
  assert.equal([...f.timers.values()].filter(t => t.ms === 5000).length, 1);
  f.gallery.trigger('st-photos-update', [[photo]]);
  const frame = [...f.timers.values()].find(t => t.ms === 5000);
  frame.fn();
  assert.equal(f.requests.at(-1).url, photo);
  succeed(f.requests);
  assert.equal([...f.timers.values()].filter(t => t.ms === 5000).length, 1);
  f.dom.window.close();
});

test('a failed OneDrive photo loads a bundled fallback instead of breaking the frame', () => {
  const f = fixture();
  f.gallery.shiftingtiles([photo], {fallbackPhotos: ['f1.jpeg']});
  const first = f.requests[0]; first.onerror();
  const fallback = f.requests.at(-1);
  assert.equal(fallback.url, 'f1.jpeg');
  fallback.onload();
  assert.ok(f.gallery.find('.single').first().attr('style').includes('f1.jpeg'));
  f.dom.window.close();
});

test('mount uses the manifest and later refreshes without restarting the plugin', () => {
  const f = fixture(); let resolve, reject, ajaxOptions;
  f.$.ajax = options => {
    ajaxOptions = options;
    return {done(fn) {resolve = fn; return this;}, fail(fn) {reject = fn; return this;}};
  };
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, ['f1.jpeg']);
  assert.equal(ajaxOptions.url, 'photos/onedrive/spotify/gallery.json');
  assert.equal(ajaxOptions.ifModified, true);
  assert.equal(ajaxOptions.cache, false);
  assert.equal(ajaxOptions.timeout, 10000);
  assert.equal(f.gallery.attr('data-photo-source'), 'bundled');
  resolve({version: 1, images: [photo]});
  assert.equal(f.gallery.attr('data-photo-source'), 'onedrive');
  controller.refresh(); reject();
  assert.equal(f.gallery.attr('data-photo-source'), 'onedrive');
  assert.equal(f.gallery.children('.row').length, 2);
  assert.equal([...f.timers.values()].filter(t => t.ms === 5000).length, 1);
  controller.stop(); f.dom.window.close();
});

test('space and arrow controls preserve the original animation behavior', () => {
  const f = fixture();
  f.gallery.shiftingtiles(['f1.jpeg'], {fallbackPhotos: ['f2.jpeg']});
  const before = f.requests.length;
  f.$(f.w.document.body).trigger(f.$.Event('keydown', {keyCode: 32}));
  assert.ok(f.requests.length > before);
  f.$(f.w.document.body).trigger(f.$.Event('keydown', {keyCode: 38}));
  assert.ok(f.gallery.hasClass('leave'));
  f.dom.window.close();
});

function stubManifest(f) {
  const calls = [];
  f.$.ajax = options => {
    const call = {options}; calls.push(call);
    return {done(fn) {call.resolve = fn; return this;}, fail(fn) {call.reject = fn; return this;}};
  };
  return calls;
}

const instagramPhoto = 'photos/onedrive/instagram/' + 'b'.repeat(64) + '.png';

for (const library of ['instagram', 'new-library']) {
  test(library + ' starts blank without animation and cannot fetch Spotify fallback', () => {
    const f = fixture('?library=' + library), calls = stubManifest(f);
    const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, ['f1.jpeg'], {manifestUrl: 'gallery.json'});
    assert.equal(calls[0].options.url, 'photos/onedrive/' + library + '/gallery.json');
    assert.equal(calls[0].options.ifModified, true);
    assert.equal(f.gallery.attr('data-photo-library'), library);
    assert.equal(f.gallery.attr('data-photo-source'), 'empty');
    assert.equal(f.gallery.css('visibility'), 'hidden');
    assert.equal(f.gallery.children().length, 0);
    assert.equal(f.requests.length, 0);
    assert.equal([...f.timers.values()].filter(t => t.ms === 5000).length, 0);
    calls[0].reject();
    controller.refresh(); calls[1].resolve({version: 1, images: [photo]});
    assert.equal(f.requests.length, 0);
    assert.equal(f.gallery.children().length, 0);
    controller.refresh(); calls[2].resolve({version: 1, images: []});
    assert.equal(f.gallery.attr('data-photo-source'), 'empty');
    assert.equal(f.requests.length, 0);
    assert.equal([...f.timers.values()].filter(t => t.ms === 60000).length, 1);
    controller.stop(); f.dom.window.close();
  });
}

test('Instagram first images initialize once; 304, empty and recovery keep one animation timer', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, ['f1.jpeg']);
  calls[0].resolve({version: 1, images: [instagramPhoto]}); succeed(f.requests);
  assert.equal(f.gallery.css('visibility'), 'visible');
  assert.equal(f.gallery.children('.row').length, 2);
  assert.equal([...f.timers.values()].filter(t => t.ms === 5000).length, 1);
  controller.refresh(); calls[1].resolve(undefined, 'notmodified');
  assert.equal(f.gallery.attr('data-photo-source'), 'onedrive');
  assert.equal([...f.timers.values()].filter(t => t.ms === 5000).length, 1);
  controller.refresh(); calls[2].resolve({version: 1, images: []});
  assert.equal(f.gallery.attr('data-photo-source'), 'empty');
  assert.equal(f.gallery.css('visibility'), 'hidden');
  for (const tile of f.gallery.find('.single, .dual > div').toArray()) {
    assert.equal(f.$(tile).css('background-image'), 'none');
  }
  const count = f.requests.length;
  [...f.timers.values()].find(t => t.ms === 5000).fn();
  assert.equal(f.requests.length, count); // Empty frames never load stale or bundled photos.
  controller.refresh(); calls[3].resolve({version: 1, images: [instagramPhoto]});
  assert.equal(f.gallery.css('visibility'), 'visible');
  assert.equal(f.gallery.children('.row').length, 2);
  assert.equal([...f.timers.values()].filter(t => t.ms === 5000).length, 1);
  [...f.timers.values()].find(t => t.ms === 5000).fn(); succeed(f.requests);
  assert.equal(f.requests.at(-1).url, instagramPhoto);
  assert.ok(f.requests.every(request => request.url === instagramPhoto));
  controller.stop(); f.dom.window.close();
});

test('empty publication invalidates pending photos before late onload or retry callbacks', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, ['f1.jpeg']);
  calls[0].resolve({version: 1, images: [instagramPhoto]});
  const pending = f.requests.slice();
  controller.refresh(); calls[1].resolve({version: 1, images: []});
  for (let i = 0; i < pending.length; i++) {
    if (i % 2) pending[i].onload();
    else pending[i].onerror();
  }
  assert.equal(f.requests.length, pending.length);
  assert.equal(f.gallery.css('visibility'), 'hidden');
  for (const tile of f.gallery.find('.single, .dual > div').toArray()) {
    assert.equal(f.$(tile).css('background-image'), 'none');
  }
  assert.ok(f.requests.every(request => request.url === instagramPhoto));
  controller.stop(); f.dom.window.close();
});

test('Instagram image failures never use Spotify bundled retry photos', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, ['f1.jpeg']);
  calls[0].resolve({version: 1, images: [instagramPhoto]});
  for (const request of f.requests.slice()) request.onerror();
  assert.ok(f.requests.length >= 8);
  assert.ok(f.requests.every(request => request.url === instagramPhoto));
  controller.stop(); f.dom.window.close();
});

test('invalid explicit selection mounts a blank gallery without Ajax or timers', () => {
  for (const search of ['?library=', '?library=../spotify', '?library=spotify&library=instagram']) {
    const f = fixture(search), calls = stubManifest(f);
    const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, ['f1.jpeg']);
    assert.equal(f.gallery.attr('data-photo-source'), 'empty');
    assert.equal(f.gallery.children().length, 0);
    assert.equal(f.requests.length, 0);
    assert.equal(calls.length, 0);
    assert.equal(f.timers.size, 0);
    controller.stop(); f.dom.window.close();
  }
});

test('duplicate mount returns the existing source without duplicating plugin or poll timers', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, ['f1.jpeg']);
  calls[0].resolve({version: 1, images: [instagramPhoto]}); succeed(f.requests);
  assert.equal(f.w.ShiftingTilesPhotos.mount(f.gallery, ['f1.jpeg']), controller);
  assert.equal(calls.length, 1);
  assert.equal(f.gallery.children('.row').length, 2);
  assert.equal([...f.timers.values()].filter(t => t.ms === 5000).length, 1);
  assert.equal([...f.timers.values()].filter(t => t.ms === 60000).length, 1);
  controller.stop(); f.dom.window.close();
});

test('inline page initializer honors the URL selection instead of the legacy manifest alias', () => {
  const f = fixture('?library=instagram'), calls = stubManifest(f);
  const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
  f.w.eval(html.match(/<script>\s*([\s\S]*?)<\/script>/)[1]);
  assert.equal(calls[0].options.url, 'photos/onedrive/instagram/gallery.json');
  assert.equal(f.gallery.attr('data-photo-source'), 'empty');
  assert.equal(f.requests.length, 0);
  f.gallery.data('shiftingtiles-source').stop(); f.dom.window.close();
});
