(function(root, factory) {
  if(typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.ShiftingTilesAlbumQueue = factory();
  }
})(typeof self !== "undefined" ? self : this, function() {
  "use strict";

  function unique(images) {
    var result = [], present = Object.create(null);
    (Array.isArray(images) ? images : []).forEach(function(url) {
      if(typeof url !== "string") return;
      url = url.trim();
      if(url && !present[url]) {
        present[url] = true;
        result.push(url);
      }
    });
    return result;
  }

  function create(images, random) {
    random = typeof random === "function" ? random : Math.random;
    var order = [], members = Object.create(null), cursor = 0, cycle = 1;
    var seen = Object.create(null), failed = Object.create(null);
    var reserved = Object.create(null), seenCount = 0, failedCount = 0;

    function shuffle(values) {
      for(var i = values.length - 1; i > 0; i--) {
        var value = Number(random());
        if(!isFinite(value)) value = 0;
        var j = Math.floor(Math.max(0, Math.min(value, 0.9999999999999999)) * (i + 1));
        var temporary = values[i];
        values[i] = values[j];
        values[j] = temporary;
      }
      return values;
    }

    function advance() {
      while(cursor < order.length && (seen[order[cursor]] || failed[order[cursor]])) cursor++;
    }

    function setImages(nextImages) {
      var urls = unique(nextImages), next = Object.create(null), additions = [];
      urls.forEach(function(url) {
        next[url] = true;
        if(!members[url]) additions.push(url);
      });
      order = order.filter(function(url) { return !!next[url]; }).concat(shuffle(additions));
      [seen, failed, reserved].forEach(function(state) {
        Object.keys(state).forEach(function(url) {
          if(!next[url]) delete state[url];
        });
      });
      members = next;
      seenCount = Object.keys(seen).length;
      failedCount = Object.keys(failed).length;
      cursor = 0;
      advance();
    }

    function reserve(count, excludedArray) {
      if(!Number.isInteger(count) || count < 1 || count > order.length) return [];
      var excluded = Object.create(null), selected = [];
      unique(excludedArray).forEach(function(url) { excluded[url] = true; });
      for(var i = cursor; i < order.length && selected.length < count; i++) {
        var url = order[i];
        if(!seen[url] && !failed[url] && !reserved[url] && !excluded[url]) selected.push(url);
      }
      if(selected.length !== count) {
        if(!order.length || seenCount + failedCount !== order.length) return [];
        // Check capacity before changing history: an impossible reservation is a no-op.
        var eligible = order.filter(function(url) { return !excluded[url] && !reserved[url]; });
        if(eligible.length < count) return [];
        order = shuffle(order.slice());
        seen = Object.create(null);
        failed = Object.create(null);
        seenCount = failedCount = cursor = 0;
        cycle++;
        selected = [];
        for(var j = 0; j < order.length && selected.length < count; j++) {
          if(!excluded[order[j]] && !reserved[order[j]]) selected.push(order[j]);
        }
      }
      selected.forEach(function(url) { reserved[url] = true; });
      return selected;
    }

    function commit(url) {
      if(!members[url] || !reserved[url]) return false;
      delete reserved[url];
      if(!seen[url]) {
        seen[url] = true;
        seenCount++;
      }
      advance();
      return true;
    }

    function release(url) {
      if(!reserved[url]) return false;
      delete reserved[url];
      return true;
    }

    function fail(url) {
      if(!members[url] || !reserved[url]) return false;
      delete reserved[url];
      if(!failed[url] && !seen[url]) {
        failed[url] = true;
        failedCount++;
      }
      advance();
      return true;
    }

    function stats() {
      return {
        albumSize: order.length,
        cycle: cycle,
        seenCount: seenCount,
        reservedCount: Object.keys(reserved).length,
        failedCount: failedCount
      };
    }

    setImages(images);
    return {setImages: setImages, reserve: reserve, commit: commit, release: release, fail: fail, stats: stats};
  }

  return {create: create};
});
