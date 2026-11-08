/* Public photo sources only. Microsoft tokens and library configuration stay server-side. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ShiftingTilesPhotos = api;
}(typeof window !== "undefined" ? window : this, function () {
  "use strict";

  function uniqueStrings(values) {
    var seen = {}, result = [];
    for (var i = 0; i < values.length; i++) {
      var value = values[i];
      if (typeof value === "string" && value && !seen["$" + value]) {
        seen["$" + value] = true;
        result.push(value);
      }
    }
    return result;
  }

  function manifestImages(manifest) {
    if (!manifest || manifest.version !== 1 || !Array.isArray(manifest.images)) {
      throw new Error("Unsupported gallery manifest");
    }
    // Only opaque, same-origin files published by the connector may be displayed.
    var pattern = /^photos\/onedrive\/[A-Za-z0-9_-]+\/[A-Za-z0-9_-]+\.(?:jpe?g|png|webp|gif|avif)$/i;
    for (var i = 0; i < manifest.images.length; i++) {
      if (typeof manifest.images[i] !== "string" || !pattern.test(manifest.images[i])) {
        throw new Error("Unsafe photo path in gallery manifest");
      }
    }
    return uniqueStrings(manifest.images);
  }

  function createController(options) {
    var fallback = uniqueStrings(options.fallback || []);
    if (!fallback.length) throw new Error("Bundled fallback photos are required");
    var schedule = options.schedule || function (callback, delay) { return setTimeout(callback, delay); };
    var cancel = options.cancel || function (timer) { clearTimeout(timer); };
    var refreshMs = options.refreshMs || 60000;
    var storage = options.storage;
    var cacheKey = options.cacheKey || "shiftingtiles-public-gallery-v1";
    var running = false, inFlight = false, timer = null, signature = null;
    var current = [], source = "bundled";

    function apply(images, nextSource) {
      var next = images.length ? images : fallback;
      var key = JSON.stringify(next);
      source = nextSource;
      current = next.slice();
      if (key !== signature) {
        signature = key;
        options.onImages(current.slice(), source);
      }
    }

    function readCache() {
      try {
        var cached = storage && storage.getItem(cacheKey);
        return cached ? manifestImages(JSON.parse(cached)) : [];
      } catch (error) { return []; }
    }

    function writeCache(manifest) {
      try { if (storage) storage.setItem(cacheKey, JSON.stringify(manifest)); }
      catch (error) { /* Storage restrictions must never prevent gallery playback. */ }
    }

    function finish() {
      inFlight = false;
      if (running) timer = schedule(refresh, refreshMs);
    }

    function refresh() {
      if (!running || inFlight) return;
      if (timer !== null) { cancel(timer); timer = null; }
      inFlight = true;
      function failure(error) {
        if (!inFlight) return;
        if (running && options.onError) options.onError(error);
        // Keep the current source on transient errors: never erase last good photos.
        finish();
      }
      try {
        options.request(function (manifest, notModified) {
          if (!inFlight) return;
          if (!running) { finish(); return; }
          if (notModified === true) { finish(); return; }
          var images;
          try { images = manifestImages(manifest); }
          catch (error) { failure(error); return; }
          writeCache(manifest);
          apply(images, images.length ? "onedrive" : "bundled");
          finish();
        }, failure);
      } catch (error) { failure(error); }
    }

    return {
      start: function () {
        if (running) return;
        running = true;
        var cached = readCache();
        apply(cached, cached.length ? "cached-onedrive" : "bundled");
        refresh();
      },
      refresh: refresh,
      stop: function () {
        running = false;
        if (timer !== null) cancel(timer);
        timer = null;
      },
      current: function () { return { images: current.slice(), source: source }; }
    };
  }

  function mount($gallery, fallback, config) {
    config = config || {};
    var $ = window.jQuery;
    var storage;
    try { storage = window.localStorage; } catch (error) { storage = null; }
    var initialized = false;
    var controller = createController({
      fallback: fallback,
      storage: storage,
      cacheKey: "shiftingtiles-public-gallery-v1:" + (config.manifestUrl || "gallery.json"),
      refreshMs: config.refreshMs || 60000,
      request: function (success, failure) {
        $.ajax({
          url: config.manifestUrl || "gallery.json",
          dataType: "json",
          cache: false,
          ifModified: true,
          timeout: config.requestTimeoutMs || 10000
        }).done(function (manifest, textStatus) {
          success(manifest, textStatus === "notmodified");
        }).fail(function () { failure(new Error("Photo manifest is unavailable")); });
      },
      onImages: function (images, source) {
        $gallery.attr("data-photo-source", source);
        if (!initialized) {
          initialized = true;
          $gallery.shiftingtiles(images.slice(), { fallbackPhotos: fallback.slice() });
        } else {
          // Replace the photo source in-place; do not duplicate animation timers/events.
          $gallery.trigger("st-photos-update", [images.slice()]);
        }
      }
    });
    $gallery.data("shiftingtiles-source", controller);
    controller.start();
    return controller;
  }

  return { uniqueStrings: uniqueStrings, manifestImages: manifestImages, createController: createController, mount: mount };
}));
