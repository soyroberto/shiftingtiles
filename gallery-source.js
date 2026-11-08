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

  function checkedLibrary(value) {
    return typeof value === "string" && /^[a-z0-9][a-z0-9_-]{0,63}$(?![\s\S])/.test(value) ? value : null;
  }

  function libraryFromSearch(search) {
    var parts = String(search || "").replace(/^\?/, "").split("&");
    var found = false, library = "spotify";
    for (var i = 0; i < parts.length; i++) {
      var pair = parts[i].split("="), name;
      try { name = decodeURIComponent(pair.shift().replace(/\+/g, " ")); }
      catch (error) { return null; }
      if (name !== "library") continue;
      // An explicit invalid or ambiguous selection must never become Spotify.
      if (found) return null;
      found = true;
      try { library = checkedLibrary(decodeURIComponent(pair.join("=").replace(/\+/g, " "))); }
      catch (error) { return null; }
    }
    return library;
  }

  function manifestUrlFor(library) {
    library = checkedLibrary(library);
    return library === null ? null : "photos/onedrive/" + library + "/gallery.json";
  }

  function manifestImages(manifest, library) {
    library = arguments.length < 2 ? "spotify" : checkedLibrary(library);
    if (library === null || !manifest || manifest.version !== 1 || !Array.isArray(manifest.images)) {
      throw new Error("Unsupported gallery manifest");
    }
    // Every path, including cached paths, must belong to this exact library.
    var prefix = "photos/onedrive/" + library + "/";
    var filename = /^[A-Za-z0-9_-]+\.(?:jpe?g|png|webp|gif|avif)$(?![\s\S])/i;
    for (var i = 0; i < manifest.images.length; i++) {
      var image = manifest.images[i];
      if (typeof image !== "string" || image.indexOf(prefix) !== 0 || !filename.test(image.slice(prefix.length))) {
        throw new Error("Unsafe or foreign photo path in gallery manifest");
      }
    }
    return uniqueStrings(manifest.images);
  }

  function createController(options) {
    var library = Object.prototype.hasOwnProperty.call(options, "libraryId") ? checkedLibrary(options.libraryId) : "spotify";
    var fallback = library === "spotify" ? uniqueStrings(options.fallback || []) : [];
    var schedule = options.schedule || function (callback, delay) { return setTimeout(callback, delay); };
    var cancel = options.cancel || function (timer) { clearTimeout(timer); };
    var refreshMs = options.refreshMs || 60000;
    var storage = options.storage;
    var cacheKey = "shiftingtiles-public-gallery-v1:" + manifestUrlFor(library);
    var running = false, activeRequest = null, timer = null, signature = null;
    var current = [], source = "empty";

    function apply(images, nextSource) {
      var next = images.length ? images : fallback;
      var key = JSON.stringify(next);
      source = next.length ? nextSource : "empty";
      current = next.slice();
      if (key !== signature) {
        signature = key;
        options.onImages(current.slice(), source);
      }
    }

    function readCache() {
      if (library === null) return [];
      try {
        var cached = storage && storage.getItem(cacheKey);
        return cached ? manifestImages(JSON.parse(cached), library) : [];
      } catch (error) { return []; }
    }

    function writeCache(manifest) {
      try { if (storage) storage.setItem(cacheKey, JSON.stringify(manifest)); }
      catch (error) { /* Storage restrictions must never prevent gallery playback. */ }
    }

    function finish(request) {
      if (activeRequest !== request) return;
      activeRequest = null;
      if (running) timer = schedule(refresh, refreshMs);
    }

    function refresh() {
      if (!running || activeRequest !== null || library === null) return;
      if (timer !== null) { cancel(timer); timer = null; }
      var request = {};
      activeRequest = request;
      function failure(error) {
        if (activeRequest !== request) return;
        if (running && options.onError) options.onError(error);
        // Keep only this library's last good photos on transient errors.
        finish(request);
      }
      try {
        options.request(function (manifest, notModified) {
          if (activeRequest !== request) return;
          if (!running) { finish(request); return; }
          if (notModified === true) { finish(request); return; }
          var images;
          try { images = manifestImages(manifest, library); }
          catch (error) { failure(error); return; }
          writeCache(manifest);
          apply(images, images.length ? "onedrive" : "bundled");
          finish(request);
        }, failure);
      } catch (error) { failure(error); }
    }

    return {
      libraryId: library,
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
        activeRequest = null;
        if (timer !== null) cancel(timer);
        timer = null;
      },
      current: function () { return { images: current.slice(), source: source }; }
    };
  }

  function mount($gallery, fallback, config) {
    config = config || {};
    var $ = window.jQuery;
    var library = Object.prototype.hasOwnProperty.call(config, "libraryId") ? checkedLibrary(config.libraryId) : libraryFromSearch(window.location.search);
    var existing = $gallery.data("shiftingtiles-source");
    if (existing) {
      if (existing.libraryId !== library) throw new Error("Library changes require a new page");
      return existing;
    }
    var manifestUrl = manifestUrlFor(library);
    // The bundled collection (including per-image retries) is Spotify-only.
    fallback = library === "spotify" ? uniqueStrings(fallback || []) : [];
    var storage;
    try { storage = window.localStorage; } catch (error) { storage = null; }
    var initialized = false;
    var controller = createController({
      libraryId: library,
      fallback: fallback,
      storage: storage,
      refreshMs: config.refreshMs || 60000,
      request: function (success, failure) {
        $.ajax({
          url: manifestUrl,
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
        $gallery.attr("data-photo-library", library === null ? "" : library);
        $gallery.css("visibility", images.length ? "" : "hidden");
        if (!images.length) {
          if (initialized) {
            // The legacy plugin ignores []; a null source makes its next frame inert.
            // Invalidate pending image callbacks and erase previously visible photos.
            $gallery.trigger("st-photos-update", [[null]]);
            $gallery.find(".single, .dual > div").each(function () {
              $(this).data("st-image-request", {}).css("background-image", "none");
            });
          }
          return; // Never create animation timers before the first real image.
        }
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

  return {
    uniqueStrings: uniqueStrings,
    libraryFromSearch: libraryFromSearch,
    manifestUrlFor: manifestUrlFor,
    manifestImages: manifestImages,
    createController: createController,
    mount: mount
  };
}));
