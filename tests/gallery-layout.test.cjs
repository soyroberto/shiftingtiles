'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { performance } = require('node:perf_hooks');
const { plan } = require('../gallery-layout.js');

const single = ratio => ({ type: 'single', ratios: [ratio] });
const dual = (a, b) => ({ type: 'dual', ratios: [a, b] });
const squares = () => [Array.from({ length: 4 }, () => single(1)), Array.from({ length: 4 }, () => single(1))];
const capture = () => [[single(1), dual(1, 1), single(1.606)], [single(1.547), single(0.667), dual(1.547, 1)]];
const close = (a, b, epsilon = 1e-9) => assert.ok(Math.abs(a - b) <= epsilon * Math.max(1, Math.abs(a), Math.abs(b)), `${a} != ${b}`);
const flatRatios = rows => rows.flatMap(row => row.flatMap(g => g.ratios));
const shape = result => result.rows.map(row => row.groups.map(g => ({ type: g.type, photoIndices: g.photoIndices })));

function verify(result, input, W, H, bounded = true) {
  assert.equal(result.rows.length, 2);
  close(result.rows.reduce((s, row) => s + row.height, 0), H);
  const seen = [], ratios = flatRatios(input), fills = [], rowFills = [];
  let area = 0;
  result.rows.forEach(row => {
    assert.ok(Number.isFinite(row.height) && row.height > 10);
    assert.ok(row.groups.length >= 2 && row.groups.length <= 4);
    close(row.groups.reduce((s, g) => s + g.width, 0), W);
    const bounds = { 2: [0.2, 0.8], 3: [0.12, 0.65], 4: [0.12, 0.5] }[row.groups.length];
    const local = [];
    row.groups.forEach(g => {
      assert.ok(Number.isFinite(g.width) && g.width > 10);
      if (bounded) {
        assert.ok(g.width / W >= bounds[0] - 1e-12);
        assert.ok(g.width / W <= bounds[1] + 1e-12);
      }
      assert.ok(g.type === 'single' || g.type === 'dual');
      assert.equal(g.photoIndices.length, g.type === 'single' ? 1 : 2);
      assert.equal(g.childHeights.length, g.photoIndices.length);
      close(g.childHeights.reduce((s, h) => s + h, 0), row.height - 10);
      g.childHeights.forEach(h => assert.ok(Number.isFinite(h) && h > 0));
      g.photoIndices.forEach((index, c) => {
        seen.push(index);
        const iw = g.width - 10, ih = g.childHeights[c] - (c ? 10 : 0);
        assert.ok(iw > 0 && ih > 0, 'positive bordered inner box, including Duo gap');
        const ratio = ratios[index];
        const scale = Math.min(iw / ratio, ih);
        const photoW = ratio * scale, photoH = scale;
        if (photoW > 0 && photoH > 0 && Number.isFinite(photoW)) {
          close(photoW / photoH, ratio); // deformation = 0, natural aspect preserved
          assert.ok(photoW <= iw + 1e-8 && photoH <= ih + 1e-8, 'contain: no crop');
          assert.ok(Math.abs(photoW - iw) < 1e-7 || Math.abs(photoH - ih) < 1e-7);
        }
        const fill = Math.exp(-Math.abs(Math.log(ratio) - Math.log(iw / ih)));
        fills.push(fill); local.push(fill);
        area += iw / W * (ih / H) * fill;
      });
    });
    assert.equal(local.length, 4);
    rowFills.push(local.reduce((a, b) => a + b, 0) / 4);
  });
  assert.deepEqual(seen.slice().sort((a, b) => a - b), [0, 1, 2, 3, 4, 5, 6, 7]);
  close(result.coverage, area);
  close(result.meanFill, fills.reduce((a, b) => a + b, 0) / 8);
  close(result.minFill, Math.min(...fills));
  result.rowFill.forEach((fill, r) => close(fill, rowFills[r]));
  close(result.quality, result.meanFill * 0.55 + Math.min(...result.rowFill) * 0.30 + result.minFill * 0.15);
  [result.coverage, result.meanFill, result.minFill, result.quality, ...result.rowFill].forEach(v => assert.ok(Number.isFinite(v) && v >= 0 && v <= 1));
}

function capturedCurrent(equalDuo = false) {
  const input = capture(), W = 1915, H = 924;
  const heights = [0.64 * H, H - 0.64 * H];
  const fractions = [[0.35, 0.16, 0.49], [0.55, 0.24, 0.21]];
  let source = 0;
  return { rows: input.map((row, r) => ({ height: heights[r], groups: row.map((g, j) => {
    const photoIndices = g.ratios.map(() => source++);
    let childHeights;
    if (g.type === 'single') childHeights = [heights[r] - 10];
    else {
      const first = equalDuo ? (heights[r] - 10) / 2 : (heights[r] - 20) * g.ratios[1] / (g.ratios[0] + g.ratios[1]);
      childHeights = [first, heights[r] - 10 - first];
    }
    return { type: g.type, photoIndices, width: fractions[r][j] * W, childHeights };
  }) })) };
}

const fixtureInput = capture();
const fixtureCurrent = capturedCurrent();
let fixtureResult;

test('UMD installs ShiftingTilesLayout without DOM dependencies', () => {
  const context = {};
  vm.runInNewContext(fs.readFileSync(require.resolve('../gallery-layout.js'), 'utf8'), context);
  assert.equal(typeof context.ShiftingTilesLayout.plan, 'function');
  const browser = { window: {} };
  vm.runInNewContext(fs.readFileSync(require.resolve('../gallery-layout.js'), 'utf8'), browser);
  assert.equal(typeof browser.window.ShiftingTilesLayout.plan, 'function');
});

test('square photos on wide desktop use four Singles in each row', () => {
  const input = squares(), result = plan(input, 1915, 924);
  verify(result, input, 1915, 924);
  assert.deepEqual(result.rows.map(r => r.groups.length), [4, 4]);
  assert.ok(result.rows.every(r => r.groups.every(g => g.type === 'single')));
  assert.ok(result.meanFill > 0.95);
});

test('landscape ratio 1.6 at 16:9 retains a Duo when it fits better', () => {
  const input = [[single(1.6), dual(1.6, 1.6), single(1.6)], [single(1.6), single(1.6), dual(1.6, 1.6)]];
  const result = plan(input, 1600, 900);
  verify(result, input, 1600, 900);
  assert.ok(result.rows.every(r => r.groups.some(g => g.type === 'dual')));
  const allSingles = { rows: [0, 1].map(r => ({ height: 450, groups: [0, 1, 2, 3].map(j => ({ type: 'single', photoIndices: [r * 4 + j], width: 400, childHeights: [440] })) })) };
  const comparison = plan(input, 1600, 900, allSingles);
  assert.ok(result.meanFill > comparison.diagnostics.oldCurrent.meanFill + 0.2);
});

test('two Duos per row are allowed and selected on a tall canvas', () => {
  const input = [[dual(1.6, 1.6), dual(1.6, 1.6)], [dual(1.6, 1.6), dual(1.6, 1.6)]];
  const result = plan(input, 800, 1400);
  verify(result, input, 800, 1400);
  assert.deepEqual(result.rows.map(r => r.groups.length), [2, 2]);
  assert.ok(result.rows.every(r => r.groups.every(g => g.type === 'dual')));
});

test('capture improves the worse row and individual mean, not merely global area', () => {
  fixtureResult = plan(fixtureInput, 1915, 924, fixtureCurrent);
  verify(fixtureResult, fixtureInput, 1915, 924);
  const old = fixtureResult.diagnostics.oldCurrent;
  assert.ok(Math.min(...fixtureResult.rowFill) > Math.min(...old.rowFill) + 0.2);
  assert.ok(fixtureResult.meanFill > old.meanFill + 0.2);
  assert.ok(fixtureResult.minFill > old.minFill + 0.25);
  assert.ok(fixtureResult.rowFill[1] > old.rowFill[1] + 0.2, 'bottom wide empty bands shrink substantially');
  assert.ok(fixtureResult.quality > old.quality + 0.2);
  console.log('CAPTURE_FIXTURE', JSON.stringify({ old, result: { coverage: fixtureResult.coverage, meanFill: fixtureResult.meanFill, minFill: fixtureResult.minFill, rowFill: fixtureResult.rowFill, quality: fixtureResult.quality }, heights: fixtureResult.rows.map(r => r.height), shape: shape(fixtureResult), diagnostics: { shortlistCount: fixtureResult.diagnostics.shortlistCount, evaluatedLayouts: fixtureResult.diagnostics.evaluatedLayouts, decision: fixtureResult.diagnostics.decision } }));
});

test('capture also improves legacy 50/50 Duo geometry', () => {
  const result = plan(capture(), 1915, 924, capturedCurrent(true));
  verify(result, capture(), 1915, 924);
  assert.ok(Math.min(...result.rowFill) > Math.min(...result.diagnostics.oldCurrent.rowFill) + 0.2);
});

test('eight source indices remain a permutation for mixed aspect ratios', () => {
  const input = [[dual(0.35, 2.8), single(1.2), single(0.7)], [dual(2.1, 0.55), dual(1, 1.7)]];
  const result = plan(input, 1366, 768);
  verify(result, input, 1366, 768);
});

test('natural inverse-aspect Duo children include exactly one 10px gap', () => {
  const input = [[dual(0.8, 1.6), dual(0.8, 1.6)], [dual(0.8, 1.6), dual(0.8, 1.6)]];
  const result = plan(input, 800, 1200);
  verify(result, input, 800, 1200);
  const ratios = flatRatios(input);
  result.rows.forEach(row => row.groups.filter(g => g.type === 'dual').forEach(g => {
    close(g.childHeights[0] * ratios[g.photoIndices[0]], (g.childHeights[1] - 10) * ratios[g.photoIndices[1]]);
  }));
  assert.ok(result.rows.some(r => r.groups.some(g => g.type === 'dual')));
});

test('stable current optimum keeps identical roles, photo positions and geometry', () => {
  const input = capture(), current = plan(input, 1915, 924);
  const result = plan(input, 1915, 924, current);
  assert.deepEqual(result.rows, current.rows);
  assert.equal(result.diagnostics.decision, 'kept-current');
  assert.equal(result.diagnostics.reordered, false);
  verify(result, input, 1915, 924);
});

test('sub-0.02 quality improvement preserves slightly perturbed current geometry', () => {
  const input = squares(), current = plan(input, 1915, 924);
  current.rows.forEach(row => { row.groups[0].width += 1; row.groups[1].width -= 1; });
  const result = plan(input, 1915, 924, current);
  assert.ok(result.diagnostics.bestQuality - result.diagnostics.oldCurrent.quality < 0.02);
  assert.deepEqual(result.rows, current.rows);
  assert.equal(result.diagnostics.decision, 'kept-current');
});

test('current source identity, including swapped rows, wins equal-quality ties', () => {
  const input = squares(), current = plan(input, 1915, 924);
  current.rows.reverse();
  current.rows.forEach(row => row.groups.reverse());
  const result = plan(input, 1915, 924, current);
  assert.deepEqual(result.rows, current.rows);
  assert.equal(result.diagnostics.reordered, false);
  verify(result, input, 1915, 924);
});

test('material geometry refit does not repack equal square photo identities', () => {
  const input = squares(), current = plan(input, 1915, 924);
  current.rows.forEach(row => {
    row.height = 462;
    row.groups.forEach((g, i) => { g.width = [0.49, 0.12, 0.12, 0.27][i] * 1915; g.childHeights = [452]; });
  });
  const result = plan(input, 1915, 924, current);
  assert.ok(result.quality - result.diagnostics.oldCurrent.quality >= 0.02);
  assert.deepEqual(shape(result), shape(current));
  assert.equal(result.diagnostics.decision, 'identity-refit');
});

test('caller scores are not trusted and input objects are not mutated', () => {
  const input = capture(), current = capturedCurrent();
  current.quality = 1; current.coverage = 1;
  const beforeInput = JSON.stringify(input), beforeCurrent = JSON.stringify(current);
  const result = plan(input, 1915, 924, current);
  assert.ok(result.diagnostics.oldCurrent.quality < 0.9);
  assert.equal(JSON.stringify(input), beforeInput);
  assert.equal(JSON.stringify(current), beforeCurrent);
});

test('previous canvas geometry scales safely while preserving source identity', () => {
  const input = squares(), current = plan(input, 1915, 924);
  const result = plan(input, 1930, 930, current);
  verify(result, input, 1930, 930, false);
  assert.deepEqual(shape(result), shape(current));
});

test('shortlist and bisection work have hard, inspectable upper bounds', () => {
  const result = plan(capture(), 1915, 924);
  const d = result.diagnostics;
  assert.equal(d.partitions, 35);
  assert.equal(d.packingCandidates, 3500);
  assert.ok(d.shortlistCount >= 16 && d.shortlistCount <= 17);
  assert.equal(d.heightSamples, 41);
  assert.ok(d.evaluatedLayouts <= d.shortlistCount * 41);
  assert.ok(d.widthSolves <= d.shortlistCount * 41 * 2);
  assert.equal(d.boundedChecks, d.widthSolves * 60);
});

test('positive finite extreme ratios do not overflow geometry or scores', () => {
  const input = [[dual(Number.MIN_VALUE, Number.MAX_VALUE), single(1e-200), single(1e200)], [single(1e-300), single(1e300), dual(1, 2)]];
  const result = plan(input, 1915, 924);
  verify(result, input, 1915, 924);
});

test('small viable canvas skips impossible bordered packing shapes', () => {
  const input = squares(), result = plan(input, 30, 70);
  verify(result, input, 30, 70);
  assert.deepEqual(result.rows.map(r => r.groups.length), [2, 2]);
});

test('invalid input and duplicate current identities fail explicitly', () => {
  assert.throws(() => plan(squares(), 0, 924), RangeError);
  assert.throws(() => plan(squares(), 1915, Infinity), RangeError);
  assert.throws(() => plan(squares(), 20, 924), RangeError);
  assert.throws(() => plan(squares(), 1915, 40), RangeError);
  assert.throws(() => plan([squares()[0]], 1915, 924), TypeError);
  assert.throws(() => plan([[single(1), dual(1, 1)], squares()[0]], 1915, 924), TypeError);
  const badRatio = squares(); badRatio[0][0].ratios[0] = NaN;
  assert.throws(() => plan(badRatio, 1915, 924), TypeError);
  const badType = squares(); badType[0][0].type = 'triple';
  assert.throws(() => plan(badType, 1915, 924), TypeError);
  const current = capturedCurrent(); current.rows[1].groups[0].photoIndices[0] = 0;
  assert.throws(() => plan(capture(), 1915, 924, current), TypeError);
});

test('playback keeps a Single for the last unused photo of an odd album', () => {
  const input = [[dual(1, 1), dual(1, 1)], [dual(1, 1), dual(1, 1)]];
  const result = plan(input, 390, 844, null, { requireSingle: true });
  verify(result, input, 390, 844);
  assert.ok(result.rows.some(row => row.groups.some(group => group.type === 'single')));
});

test('deterministic repeated plan and advisory Node runtime', () => {
  const input = capture();
  const start = performance.now();
  const first = plan(input, 1915, 924);
  const elapsedMs = performance.now() - start;
  const second = plan(input, 1915, 924);
  assert.deepEqual(first, second);
  console.log('BOUNDED_RUNTIME', JSON.stringify({ elapsedMs: +elapsedMs.toFixed(3), targetMs: 100, advisoryOnly: true, shortlisted: first.diagnostics.shortlistCount, evaluatedLayouts: first.diagnostics.evaluatedLayouts }));
});
