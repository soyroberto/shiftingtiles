/* Eight existing photos, two rows, contain-only geometry. No DOM, network or timers. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ShiftingTilesLayout = factory();
}(typeof window !== 'undefined' ? window : typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  var BORDER = 10, GAP = 10, SHORTLIST = 16, MATERIAL = 0.02, TIE = 1e-12;
  var BOUNDS = { 2: [0.2, 0.8], 3: [0.12, 0.65], 4: [0.12, 0.5] };

  function positive(x) { return typeof x === 'number' && isFinite(x) && x > 0; }
  function sum(values) { return values.reduce(function (a, b) { return a + b; }, 0); }
  function logAdd(a, b) {
    var hi = Math.max(a, b), lo = Math.min(a, b);
    return hi + Math.log1p(Math.exp(lo - hi));
  }
  function harmonicLog(a, b) {
    var lo = Math.min(a, b), hi = Math.max(a, b);
    return lo - Math.log1p(Math.exp(lo - hi));
  }
  function key(rows) {
    return rows.map(function (row) {
      return row.map(function (g) { return g.type + ':' + g.photoIndices.join(','); }).join('|');
    }).join('/');
  }
  function group(indices) { return { type: indices.length === 1 ? 'single' : 'dual', photoIndices: indices.slice() }; }

  function readInput(rows, width, height) {
    if (!positive(width) || !positive(height) || width <= 20 || height <= 40) {
      throw new RangeError('Canvas must exceed 20px width and 40px height for two bordered Duo rows.');
    }
    if (!Array.isArray(rows) || rows.length !== 2) throw new TypeError('Expected two rows.');
    var ratios = [], identity = [];
    rows.forEach(function (row) {
      if (!Array.isArray(row) || row.length < 2 || row.length > 4) throw new TypeError('Expected 2..4 groups per row.');
      var count = 0;
      identity.push(row.map(function (g) {
        if (!g || (g.type !== 'single' && g.type !== 'dual') || !Array.isArray(g.ratios) ||
            g.ratios.length !== (g.type === 'single' ? 1 : 2) || !g.ratios.every(positive)) {
          throw new TypeError('Invalid Single/Duo or natural aspect ratios.');
        }
        var indices = g.ratios.map(function (ratio) { var index = ratios.length; ratios.push(ratio); count++; return index; });
        return group(indices);
      }));
      if (count !== 4) throw new TypeError('Each row must contain exactly four photos.');
    });
    return { ratios: ratios, identity: identity };
  }

  function children(indices, ratios, height) {
    if (indices.length === 1) return [height - BORDER];
    var available = height - BORDER - GAP;
    var a = Math.log(ratios[indices[0]]), b = Math.log(ratios[indices[1]]);
    var first = available * Math.exp(b - logAdd(a, b));
    // Keep both inner boxes representably positive even for subnormal/extreme ratios.
    var epsilon = Math.max(Number.MIN_VALUE, available * 1e-12);
    first = Math.max(epsilon, Math.min(available - epsilon, first));
    return [first, height - BORDER - first]; // second includes its 10px top gap
  }

  function currentRows(current, width, height) {
    if (!current || !Array.isArray(current.rows) || current.rows.length !== 2) throw new TypeError('Invalid currentLayout rows.');
    var seen = [], oldTotal = 0;
    current.rows.forEach(function (row) {
      if (!row || !positive(row.height) || !Array.isArray(row.groups) || row.groups.length < 2 || row.groups.length > 4) {
        throw new TypeError('Invalid currentLayout row.');
      }
      oldTotal += row.height;
      var count = 0;
      row.groups.forEach(function (g) {
        var n = g && g.type === 'single' ? 1 : g && g.type === 'dual' ? 2 : 0;
        if (!n || !Array.isArray(g.photoIndices) || g.photoIndices.length !== n || !positive(g.width) ||
            !Array.isArray(g.childHeights) || g.childHeights.length !== n || !g.childHeights.every(positive) ||
            g.width <= BORDER || (n === 2 && g.childHeights[1] <= GAP)) throw new TypeError('Invalid currentLayout group geometry.');
        if (Math.abs(sum(g.childHeights) - (row.height - BORDER)) > Math.max(1, row.height) * 1e-8) {
          throw new TypeError('currentLayout children must sum to row height minus 10px.');
        }
        g.photoIndices.forEach(function (index) {
          if (!Number.isInteger(index) || index < 0 || index > 7 || seen[index]) throw new TypeError('currentLayout must use source indices 0..7 exactly once.');
          seen[index] = true; count++;
        });
      });
      if (count !== 4 || !positive(sum(row.groups.map(function (g) { return g.width; })))) throw new TypeError('Invalid currentLayout photo count.');
    });
    if (!positive(oldTotal)) throw new TypeError('Invalid currentLayout total height.');
    // A previous plan may come from a different canvas. Preserve identity and scale
    // outer widths/row proportions, then preserve the Duo's INNER height split.
    var sameHeight = oldTotal === height;
    var firstHeight = sameHeight ? current.rows[0].height : height * (current.rows[0].height / oldTotal);
    return current.rows.map(function (row, r) {
      var h = r ? height - firstHeight : firstHeight;
      var totalWidth = sum(row.groups.map(function (g) { return g.width; }));
      var sameWidth = Math.abs(totalWidth - width) <= Math.max(1, width) * 1e-12;
      var used = 0;
      var groups = row.groups.map(function (g, index) {
        var w = sameWidth ? g.width : width * (g.width / totalWidth);
        // Preserve the current values verbatim when only summation order changed.
        if (!sameWidth && index === row.groups.length - 1) w = width - used;
        used += w;
        var child;
        if (h === row.height) child = g.childHeights.slice();
        else if (g.type === 'single') child = [h - BORDER];
        else {
          var inner = h - BORDER - GAP;
          var proportion = g.childHeights[0] / (row.height - BORDER - GAP);
          child = [inner * proportion, h - BORDER - inner * proportion];
        }
        if (w <= BORDER || child[0] <= 0 || (g.type === 'dual' && child[1] <= GAP)) {
          throw new RangeError('currentLayout cannot fit positive bordered photo boxes on this canvas.');
        }
        return { type: g.type, photoIndices: g.photoIndices.slice(), width: w, childHeights: child };
      });
      return { height: h, groups: groups };
    });
  }

  function measure(rows, ratios, width, height) {
    var coverage = 0, fills = [], rowFill = [];
    rows.forEach(function (row) {
      var rowFills = [];
      row.groups.forEach(function (g) {
        var iw = g.width - BORDER;
        g.photoIndices.forEach(function (index, c) {
          var ih = g.childHeights[c] - (c ? GAP : 0);
          // Contain: fill = min(photoRatio / boxRatio, boxRatio / photoRatio).
          // Log space avoids intermediate overflow; normalized areas avoid W*H overflow.
          var fill = Math.exp(-Math.abs(Math.log(ratios[index]) - (Math.log(iw) - Math.log(ih))));
          fill = Math.max(0, Math.min(1, fill));
          fills.push(fill); rowFills.push(fill);
          coverage += (iw / width) * (ih / height) * fill;
        });
      });
      rowFill.push(sum(rowFills) / 4);
    });
    var meanFill = sum(fills) / 8, minFill = Math.min.apply(Math, fills);
    return { rows: rows, coverage: Math.max(0, Math.min(1, coverage)), meanFill: meanFill,
      minFill: minFill, rowFill: rowFill,
      quality: meanFill * 0.55 + Math.min.apply(Math, rowFill) * 0.30 + minFill * 0.15 };
  }

  function positions(rows) {
    var result = [];
    rows.forEach(function (row, r) {
      row.forEach(function (g, j) {
        g.photoIndices.forEach(function (index, c) {
          result[index] = { row: r, x: (j + 0.5) / row.length, type: g.type, child: c };
        });
      });
    });
    return result;
  }
  function distance(rows, reference) {
    var p = positions(rows), d = 0;
    p.forEach(function (v, i) {
      var old = reference[i];
      d += (v.row !== old.row ? 8 : 0) + (v.type !== old.type ? 2 : 0) + Math.abs(v.x - old.x) + (v.child !== old.child ? 0.1 : 0);
    });
    return d;
  }
  function packingOptions(indices, rank) {
    var options = [];
    function add(parts) {
      parts = parts.map(function (part) { return part.slice().sort(function (a, b) { return rank[a] - rank[b]; }); });
      parts.sort(function (a, b) { return rank[a[0]] - rank[b[0]]; });
      options.push(parts.map(group));
    }
    add(indices.map(function (i) { return [i]; }));
    for (var a = 0; a < 4; a++) for (var b = a + 1; b < 4; b++) {
      var parts = [[indices[a], indices[b]]];
      for (var c = 0; c < 4; c++) if (c !== a && c !== b) parts.push([indices[c]]);
      add(parts);
    }
    for (var partner = 1; partner < 4; partner++) {
      var rest = indices.filter(function (_, i) { return i !== 0 && i !== partner; });
      add([[indices[0], indices[partner]], rest]);
    }
    return options; // 1 all-Single + 6 one-Duo + 3 two-Duo
  }
  function effectiveLog(row, logs) {
    return row.reduce(function (total, g) {
      var value = g.type === 'single' ? logs[g.photoIndices[0]] : harmonicLog(logs[g.photoIndices[0]], logs[g.photoIndices[1]]);
      return total === null ? value : logAdd(total, value);
    }, null);
  }

  function widthsFor(row, ratios, height, width, diagnostics) {
    var bounds = BOUNDS[row.length], lower = Math.max(bounds[0], (BORDER + Math.max(1e-9, width * Number.EPSILON * 4)) / width);
    var upper = bounds[1];
    if (lower * row.length >= 1 || lower > upper) return null;
    var logs = row.map(function (g) {
      var value = g.type === 'single' ? Math.log(ratios[g.photoIndices[0]]) + Math.log(height - BORDER) :
        harmonicLog(Math.log(ratios[g.photoIndices[0]]), Math.log(ratios[g.photoIndices[1]])) + Math.log(height - BORDER - GAP);
      // Ideal OUTER width is natural inner width plus the two 5px side borders.
      return logAdd(value, Math.log(BORDER));
    });
    var largest = Math.max.apply(Math, logs);
    logs = logs.map(function (v) { return v - largest; });
    var lowLog = Math.log(lower), highLog = Math.log(upper);
    var lo = lowLog - 1, hi = highLog - Math.min.apply(Math, logs) + 1;
    var fractions, total;
    // Bounded bisection; an active cap can release an earlier floor.
    for (var pass = 0; pass < 60; pass++) {
      var mid = lo + (hi - lo) / 2;
      fractions = logs.map(function (v) {
        var x = v + mid;
        return x <= lowLog ? lower : x >= highLog ? upper : Math.exp(x);
      });
      total = sum(fractions);
      if (total > 1) hi = mid; else lo = mid;
    }
    diagnostics.widthSolves++; diagnostics.boundedChecks += 60;
    var remaining = 1 - total;
    for (var i = 0; i < fractions.length && remaining !== 0; i++) {
      var delta = Math.max(lower - fractions[i], Math.min(upper - fractions[i], remaining));
      fractions[i] += delta; remaining -= delta;
    }
    var result = fractions.map(function (f) { return f * width; });
    // Last group absorbs only roundoff, so left-to-right summation is exactly W.
    result[result.length - 1] = width - sum(result.slice(0, -1));
    return result;
  }
  function layoutFor(shape, ratios, width, height, firstHeight, diagnostics) {
    var result = [];
    for (var r = 0; r < 2; r++) {
      var h = r ? height - firstHeight : firstHeight;
      var hasDuo = shape[r].some(function (g) { return g.type === 'dual'; });
      if (h <= BORDER + (hasDuo ? GAP : 0)) return null;
      var widths = widthsFor(shape[r], ratios, h, width, diagnostics);
      if (!widths) return null;
      result.push({ height: h, groups: shape[r].map(function (g, i) {
        return { type: g.type, photoIndices: g.photoIndices.slice(), width: widths[i], childHeights: children(g.photoIndices, ratios, h) };
      }) });
    }
    return result;
  }

  function plan(rows, width, height, currentLayout, options) {
    var requireSingle = options && options.requireSingle;
    function permitted(shape) {
      return !requireSingle || shape.some(function (row) { return row.some(function (g) { return g.type === 'single'; }); });
    }
    var input = readInput(rows, width, height), ratios = input.ratios;
    var oldRows = currentLayout == null ? null : currentRows(currentLayout, width, height);
    var identity = oldRows ? oldRows.map(function (row) { return row.groups.map(function (g) { return group(g.photoIndices); }); }) : input.identity;
    var old = oldRows && permitted(oldRows.map(function (r) { return r.groups; })) ? measure(oldRows, ratios, width, height) : null;
    var reference = positions(identity), rank = [], serial = 0;
    identity.forEach(function (row) { row.forEach(function (g) { g.photoIndices.forEach(function (i) { rank[i] = serial++; }); }); });
    var logs = ratios.map(Math.log), canvasLog = Math.log(width) - Math.log(height), candidates = [];
    var diagnostics = { partitions: 0, packingCandidates: 0, shortlistCount: 0, evaluatedLayouts: 0,
      widthSolves: 0, boundedChecks: 0, heightSamples: 41, shortlistLimit: SHORTLIST, materialThreshold: MATERIAL };
    for (var a = 1; a <= 5; a++) for (var b = a + 1; b <= 6; b++) for (var c = b + 1; c <= 7; c++) {
      var top = [0, a, b, c], bottom = [];
      for (var index = 1; index < 8; index++) if (top.indexOf(index) === -1) bottom.push(index);
      var first = packingOptions(top, rank), second = packingOptions(bottom, rank);
      diagnostics.partitions++;
      first.forEach(function (r0) {
        var s0 = effectiveLog(r0, logs);
        second.forEach(function (r1) {
          var s1 = effectiveLog(r1, logs), shape = [r0, r1], d = distance(shape, reference);
          if (!permitted(shape)) return;
          var aspect = harmonicLog(s0, s1);
          var fit = Math.exp(-Math.abs(aspect - canvasLog));
          candidates.push({ shape: shape, key: key(shape), distance: d, heuristic: fit - d * 0.0003 });
        });
      });
    }
    diagnostics.packingCandidates = candidates.length;
    candidates.sort(function (a, b) {
      return b.heuristic - a.heuristic || a.distance - b.distance || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
    });
    var shortlist = candidates.slice(0, SHORTLIST), identityKey = key(identity);
    if (permitted(identity) && !shortlist.some(function (c) { return c.key === identityKey; })) shortlist.push({ shape: identity, key: identityKey, distance: 0 });
    diagnostics.shortlistCount = shortlist.length;
    var preferredHeight = oldRows ? oldRows[0].height / height : 0.5;
    var best = null, bestIdentity = null;
    function better(candidate, previous) {
      return !previous || candidate.result.quality > previous.result.quality + TIE ||
        (Math.abs(candidate.result.quality - previous.result.quality) <= TIE &&
          (candidate.distance < previous.distance - TIE ||
            (Math.abs(candidate.distance - previous.distance) <= TIE && candidate.heightDistance < previous.heightDistance - TIE)));
    }
    shortlist.forEach(function (candidate) {
      for (var percent = 30; percent <= 70; percent++) {
        var geometry = layoutFor(candidate.shape, ratios, width, height, height * (percent / 100), diagnostics);
        if (!geometry) continue;
        diagnostics.evaluatedLayouts++;
        var entry = { result: measure(geometry, ratios, width, height), key: candidate.key, distance: candidate.distance,
          heightDistance: Math.abs(percent / 100 - preferredHeight) };
        if (better(entry, best)) best = entry;
        if (candidate.key === identityKey && better(entry, bestIdentity)) bestIdentity = entry;
      }
    });
    if (!best && !old) throw new RangeError('Canvas is too small for positive bordered photo boxes.');
    var result, decision;
    // Compare actual measured current geometry, never trust cached scores supplied by a caller.
    if (old && (!best || best.result.quality - old.quality < MATERIAL)) {
      result = old; decision = 'kept-current';
    } else if (bestIdentity && best.key !== identityKey && best.result.quality - bestIdentity.result.quality < MATERIAL) {
      if (old && bestIdentity.result.quality - old.quality < MATERIAL) { result = old; decision = 'kept-current'; }
      else { result = bestIdentity.result; decision = 'identity-refit'; }
    } else { result = best.result; decision = best.key === identityKey ? 'identity-refit' : 'material-repack'; }
    diagnostics.decision = decision;
    diagnostics.reordered = key(result.rows.map(function (r) { return r.groups; })) !== identityKey;
    diagnostics.bestQuality = best ? best.result.quality : old.quality;
    diagnostics.identityQuality = bestIdentity ? bestIdentity.result.quality : null;
    diagnostics.oldCurrent = old ? { coverage: old.coverage, meanFill: old.meanFill, minFill: old.minFill, rowFill: old.rowFill.slice(), quality: old.quality } : null;
    result.diagnostics = diagnostics;
    return result;
  }
  return { plan: plan };
}));
