/* Fit the eight loaded photos once, then keep Single/Duo geometry stable. */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ShiftingTilesPlayer = api;
}(typeof window !== 'undefined' ? window : this, function () {
  'use strict';
  var serial = 0;

  function attach($gallery) {
    var gallery = $gallery[0], win = gallery.ownerDocument.defaultView;
    var $ = $gallery.constructor, namespace = '.stableLayout' + (++serial);
    var needsFit = true, raf = null, destroyed = false, observer = null;
    var lastWidth = 0, lastHeight = 0, last = null, fits = 0;

    function positive(value) {
      return typeof value === 'number' && isFinite(value) && value > 0;
    }

    function snapshot() {
      var photos = [], rows = [], nodes = [], measured = [], ready = true;
      var rowNodes = $gallery.children('.row');
      if (rowNodes.length !== 2) return null;
      rowNodes.each(function () {
        var row = [], groups = [], geometry = [], count = 0;
        var rowHeight = this.getBoundingClientRect().height;
        $(this).children('.single, .dual').each(function () {
          var type = $(this).hasClass('dual') ? 'dual' : 'single';
          var leaves = type === 'dual' ? $(this).children('div') : $(this);
          var ratios = [], indices = [];
          if (leaves.length !== (type === 'dual' ? 2 : 1)) ready = false;
          leaves.each(function () {
            var leaf = $(this), width = leaf.data('st-natural-width');
            var height = leaf.data('st-natural-height'), url = leaf.data('st-photo-url');
            var request = leaf.data('st-image-request'), loaded = leaf.data('st-image-loaded-request');
            var image = this.style.backgroundImage;
            if (!positive(width) || !positive(height) || !positive(width / height) ||
                typeof url !== 'string' || !url || !image || image === 'none' ||
                (request && request !== loaded)) ready = false;
            indices.push(photos.length);
            ratios.push(width / height);
            photos.push({ url: url, image: image, width: width, height: height, request: request, loaded: loaded });
            count++;
          });
          var childHeights;
          if (type === 'single') childHeights = [rowHeight - 10];
          else {
            var first = leaves[0].getBoundingClientRect().height;
            var second = leaves[1].getBoundingClientRect().height - 10;
            var scaledFirst = (rowHeight - 20) * first / (first + second);
            childHeights = [scaledFirst, rowHeight - 10 - scaledFirst];
          }
          row.push({ type: type, ratios: ratios });
          groups.push({ node: this, indices: indices, type: type });
          geometry.push({ type: type, photoIndices: indices, width: this.getBoundingClientRect().width, childHeights: childHeights });
        });
        if (count !== 4 || row.length < 2 || row.length > 4) ready = false;
        rows.push(row);
        nodes.push({ node: this, groups: groups });
        measured.push({ height: rowHeight, groups: geometry });
      });
      if (!ready || photos.length !== 8 || new Set(photos.map(function (p) { return p.url; })).size !== 8) return null;
      var validMeasurements = measured.every(function (row) {
        return positive(row.height) && row.groups.every(function (g) {
          return positive(g.width) && g.childHeights.every(positive);
        });
      });
      return { rows: rows, photos: photos, nodes: nodes, current: validMeasurements ? { rows: measured } : null };
    }

    function populate(node, photo) {
      node.style.backgroundImage = photo.image;
      $(node).data('st-photo-url', photo.url).data('st-natural-width', photo.width).data('st-natural-height', photo.height);
      if (photo.request) $(node).data('st-image-request', photo.request).data('st-image-loaded-request', photo.loaded);
    }

    function apply(plan, input) {
      var source = input.nodes.map(function (row) {
        return row.groups.map(function (g) { return { type: g.type, indices: g.indices }; });
      });
      var target = plan.rows.map(function (row) {
        return row.groups.map(function (g) { return { type: g.type, indices: g.photoIndices }; });
      });
      var changed = JSON.stringify(source) !== JSON.stringify(target);
      if (changed) plan.rows.forEach(function (row, r) {
        var parent = input.nodes[r].node;
        $(parent).empty();
        row.groups.forEach(function (group) {
          var node = win.document.createElement('div');
          node.className = group.type;
          if (group.type === 'single') populate(node, input.photos[group.photoIndices[0]]);
          else group.photoIndices.forEach(function (index) {
            var leaf = win.document.createElement('div');
            populate(leaf, input.photos[index]);
            node.appendChild(leaf);
          });
          parent.appendChild(node);
        });
      });
      $gallery.addClass('gallery-balanced');
      plan.rows.forEach(function (row, r) {
        var parent = input.nodes[r].node;
        parent.style.height = (row.height / gallery.clientHeight * 100) + '%';
        Array.from(parent.children).forEach(function (node, g) {
          var group = row.groups[g], width = (group.width / gallery.clientWidth * 50) + '%';
          node.style.setProperty('--st-box-width', width);
          node.style.setProperty('--st-start-width', width);
          if (group.type === 'dual') Array.from(node.children).forEach(function (leaf, c) {
            leaf.style.height = (group.childHeights[c] / (row.height - 10) * 100) + '%';
          });
        });
      });
      fits++;
      last = JSON.parse(JSON.stringify(plan));
      last.fits = fits;
      last.width = gallery.clientWidth;
      last.height = gallery.clientHeight;
      last.rearranged = changed;
    }

    function flush() {
      raf = null;
      if (destroyed || !needsFit || $gallery.find('.disappear').length ||
          $gallery.hasClass('leave') || $gallery.hasClass('left')) return;
      var playback = $gallery.data('shiftingtiles-playback');
      var state = playback && playback.stats ? playback.stats() : null;
      if (state && (state.loading || state.animating)) return;
      var width = gallery.clientWidth, height = gallery.clientHeight;
      if (width <= 20 || height <= 40) return;
      var input = snapshot();
      if (!input) return;
      // A nine-photo album only has one spare URL: keep Single boxes available.
      if (!state || state.albumSize >= 10) {
        var plan = win.ShiftingTilesLayout.plan(input.rows, width, height, input.current, { requireSingle: true });
        apply(plan, input);
      } else {
        last = { fits: ++fits, width: width, height: height, rearranged: false, smallAlbum: true };
      }
      lastWidth = width;
      lastHeight = height;
      needsFit = false;
      $gallery.trigger('st-layout-updated');
    }

    function schedule() {
      if (destroyed || !needsFit || raf !== null) return;
      raf = win.requestAnimationFrame(flush);
    }

    function resized() {
      if (gallery.clientWidth !== lastWidth || gallery.clientHeight !== lastHeight) {
        needsFit = true;
        schedule();
      }
    }

    var controller = {
      metrics: function () { return last ? JSON.parse(JSON.stringify(last)) : null; },
      destroy: function () {
        destroyed = true;
        if (raf !== null) win.cancelAnimationFrame(raf);
        $gallery.off(namespace);
        if (observer) observer.disconnect();
        else win.removeEventListener('resize', resized);
      }
    };
    $gallery.on('st-image-ready' + namespace + ' st-initial-ready' + namespace + ' st-animate-after' + namespace, schedule);
    if (typeof win.ResizeObserver === 'function') {
      observer = new win.ResizeObserver(resized);
      observer.observe(gallery);
    } else win.addEventListener('resize', resized);
    $gallery.data('shiftingtiles-layout', controller);
    schedule();
    return controller;
  }

  return { attach: attach };
}));
