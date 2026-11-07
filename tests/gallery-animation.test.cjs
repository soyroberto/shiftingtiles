const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
// Set NODE_PATH to the local test-tool installation, or install jsdom in your test environment.
const {JSDOM} = require('jsdom');
const ROOT = path.resolve(__dirname, '..');
const photo = 'photos/onedrive/spotify/' + 'a'.repeat(64) + '.jpg';

function fixture() {
  const dom = new JSDOM('<!doctype html><body><div class="gallery"></div></body>', {
    url: 'https://saauxfotos.z26.web.core.windows.net/', runScripts: 'outside-only'
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
  const f = fixture(); let resolve, reject;
  f.$.ajax = () => ({done(fn) {resolve = fn; return this;}, fail(fn) {reject = fn; return this;}});
  const controller = f.w.ShiftingTilesPhotos.mount(f.gallery, ['f1.jpeg']);
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
