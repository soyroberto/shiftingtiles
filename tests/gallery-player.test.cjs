'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const player = require('../gallery-player.js');
function fixture() {
  const dom = new JSDOM('<!doctype html><body><div id="gallery" class="shiftingtiles"></div></body>', { runScripts: 'outside-only' });
  const w = dom.window;
  w.eval(fs.readFileSync(path.resolve(__dirname, '../jquery-1.8.3.js'), 'utf8'));
  const $ = w.jQuery, g = $('#gallery'), queue = new Map();
  let serial = 0, resize, width = 1600, height = 800, plans = 0;
  w.requestAnimationFrame = fn => { queue.set(++serial, fn); return serial; };
  w.cancelAnimationFrame = n => queue.delete(n);
  w.ResizeObserver = class { constructor(fn) { resize = fn; } observe() {} disconnect() {} };
  for (let r = 0; r < 2; r++) g.append('<div class="row"><div class="single"></div><div class="single"></div><div class="dual"><div></div><div></div></div></div>');
  Object.defineProperties(g[0], { clientWidth: { get: () => width }, clientHeight: { get: () => height } });
  g.find('.single,.dual>div').each(function (i) {
    const marker = {};
    this.style.backgroundImage = 'url("p' + i + '.jpg")';
    $(this).data('st-photo-url', 'p' + i + '.jpg').data('st-natural-width', 100 + i * 10).data('st-natural-height', 100).data('st-image-request', marker).data('st-image-loaded-request', marker);
  });
  const state = { albumSize: 443, loading: false, animating: false };
  g.data('shiftingtiles-playback', { stats: () => state });
  w.ShiftingTilesLayout = { plan: () => { plans++; return { rows: [0, 1].map(r => ({ height: height / 2, groups: [0, 1, 2, 3].map(i => ({ type: 'single', photoIndices: [r * 4 + i], width: width / 4, childHeights: [height / 2 - 10] })) })) }; } };
  return { dom, w, $, g, state, queue, plans: () => plans, flush: () => { const fns = Array.from(queue.values()); queue.clear(); fns.forEach(fn => fn()); }, resize: (x, y) => { width = x; height = y; resize(); } };
}
test('initial fit preserves eight loaded URLs and metadata, without requesting images', () => {
  const f = fixture(); try {
    f.w.Image = function () { throw Error('No new image requests'); };
    const c = player.attach(f.g); f.flush();
    assert.equal(f.plans(), 1); assert.equal(f.g.find('.single').length, 8);
    assert.equal(new Set(f.g.find('.single').toArray().map(n => f.$(n).data('st-photo-url'))).size, 8);
    assert.equal(c.metrics().fits, 1); c.destroy();
  } finally { f.dom.window.close(); }
});
test('photo events do not change geometry or rearrange existing nodes', () => {
  const f = fixture(); try {
    const c = player.attach(f.g); f.flush(); const node = f.g.find('.single')[0];
    f.$(node).data('st-photo-url', 'new.jpg').data('st-natural-width', 60); node.style.backgroundImage = 'url("new.jpg")';
    f.g.trigger('st-image-ready'); f.g.trigger('st-animate-after'); f.flush();
    assert.equal(f.plans(), 1); assert.equal(f.g.find('.single')[0], node); assert.equal(c.metrics().fits, 1); c.destroy();
  } finally { f.dom.window.close(); }
});
test('resize waits for animation/load completion and fits only once per resize', () => {
  const f = fixture(); try {
    const c = player.attach(f.g); f.flush(); const node = f.g.find('.single').first().addClass('disappear');
    f.resize(1200, 900); f.flush(); assert.equal(f.plans(), 1);
    node.removeClass('disappear'); f.state.loading = true; f.g.trigger('st-animate-after'); f.flush(); assert.equal(f.plans(), 1);
    f.state.loading = false; f.g.trigger('st-image-ready'); f.flush(); assert.equal(f.plans(), 2);
    f.g.trigger('st-animate-after'); f.flush(); assert.equal(f.plans(), 2); c.destroy();
  } finally { f.dom.window.close(); }
});
test('duplicate or pending photo metadata does not fit; nine-photo albums retain Single boxes', () => {
  const f = fixture(); try {
    const leaf = f.g.find('.single').first(); leaf.data('st-photo-url', 'p1.jpg');
    const c = player.attach(f.g); f.flush(); assert.equal(f.plans(), 0);
    leaf.data('st-photo-url', 'p0.jpg'); f.state.albumSize = 9; f.g.trigger('st-initial-ready'); f.flush();
    assert.equal(f.plans(), 0); assert.equal(f.g.find('.single').length, 4); assert.ok(c.metrics().smallAlbum); c.destroy();
  } finally { f.dom.window.close(); }
});
