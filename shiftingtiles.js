(function($) {
  "use strict";
  var instanceNumber = 0;

  $.fn.shiftingtiles = function(images, options) {
    var settings = $.extend({photosource: images, duration: 5000, fallbackPhotos: []}, options);
    return this.each(function() {
      var where = $(this), previous = where.data("shiftingtiles-playback");
      if(previous) return;
      var queue = window.ShiftingTilesAlbumQueue.create(images);
      var namespace = ".shiftingtiles" + (++instanceNumber);
      var timeout = null, pending = null, active = null, destroyed = false;
      var initialReady = false, generation = 0, members = Object.create(null);
      var photoSelector = ".single, .dual > div";
      var duration = Number(settings.duration) > 0 ? Number(settings.duration) : 5000;

      function updateMembers(nextImages) {
        members = Object.create(null);
        (Array.isArray(nextImages) ? nextImages : []).forEach(function(url) {
          if(typeof url === "string" && url.trim()) members[url.trim()] = true;
        });
      }
      updateMembers(images);

      function visibleURLs() {
        var urls = [];
        // Outgoing tiles remain excluded until their animation actually finishes.
        where.find(photoSelector).each(function() {
          var url = $(this).data("st-photo-url");
          if(url) urls.push(url);
        });
        return urls;
      }

      function reserve(count, extraExcluded) {
        var before = queue.stats().cycle;
        var urls = queue.reserve(count, visibleURLs().concat(extraExcluded || []));
        if(queue.stats().cycle !== before) where.trigger("st-galleryrestart");
        return urls;
      }

      function clearTimer() {
        if(timeout !== null) clearTimeout(timeout);
        timeout = null;
      }

      function schedule() {
        if(destroyed || document.hidden || queue.stats().albumSize < 8 || timeout !== null) return;
        timeout = setTimeout(function() {
          timeout = null;
          frame();
          schedule();
        }, duration);
      }

      function cleanRequest(slot) {
        if(slot.deadline !== null) clearTimeout(slot.deadline);
        slot.deadline = null;
        if(slot.image) slot.image.onload = slot.image.onerror = null;
      }

      function cancelPending() {
        if(!pending) return;
        var transaction = pending;
        pending = null;
        transaction.cancelled = true;
        transaction.slots.forEach(function(slot) {
          cleanRequest(slot);
          queue.release(slot.url);
        });
      }

      function current(transaction) {
        return !destroyed && !transaction.cancelled && pending === transaction &&
          transaction.generation === generation;
      }

      function setPhoto(element, slot) {
        element.css("background-image", "url(" + JSON.stringify(slot.url) + ")");
        element.data("st-photo-url", slot.url);
        element.data("st-natural-width", slot.width);
        element.data("st-natural-height", slot.height);
        element.data("st-image-request", slot.marker);
        element.data("st-image-loaded-request", slot.marker);
      }

      function imageReady(element, slot) {
        where.trigger("st-image-ready", [element, slot.url]);
      }

      function completeInitial(transaction) {
        if(!current(transaction) || !transaction.slots.every(function(slot) { return slot.settled; })) return;
        pending = null;
        var loaded = where.find(photoSelector).filter(function() {
          return !!$(this).data("st-photo-url");
        }).length;
        if(loaded === 8 && !initialReady) {
          initialReady = true;
          where.find(".loading").remove();
          where.css("visibility", "");
          where.trigger("st-initial-ready");
        }
        schedule();
      }

      function activate(transaction) {
        if(!current(transaction) || transaction.kind !== "transition" ||
          !transaction.slots.every(function(slot) { return slot.loaded; }) || document.hidden || active) return;
        // Clone only after preload. Its old backgrounds are overwritten while detached.
        var outgoing = transaction.outgoing;
        if(!outgoing.parent().length) {
          cancelPending();
          schedule();
          return;
        }
        where.trigger("st-animate-before", outgoing);
        if(!current(transaction) || document.hidden) return;
        var incoming = outgoing.clone();
        var photos = incoming.hasClass("single") ? incoming : incoming.children();
        photos.each(function(index) { setPhoto($(this), transaction.slots[index]); });
        transaction.slots.forEach(function(slot) { queue.commit(slot.url); });
        pending = null;
        active = {outgoing: outgoing, incoming: incoming};
        outgoing.parent().append(incoming);
        outgoing.addClass("disappear");
        photos.each(function(index) { imageReady($(this), transaction.slots[index]); });
        where.trigger("st-animate", outgoing);
        schedule();
      }

      function loadSlot(transaction, slot) {
        if(!current(transaction) || !slot.url) return;
        slot.attempts++;
        slot.marker = {url: slot.url, generation: generation, attempt: slot.attempts};
        var marker = slot.marker, image = new Image(), finished = false;
        slot.image = image;
        slot.deadline = setTimeout(failed, 20000);

        function valid() {
          return !finished && current(transaction) && slot.marker === marker && !!members[slot.url];
        }
        function cleanup() {
          finished = true;
          cleanRequest(slot);
        }
        function failed() {
          if(!valid()) return;
          cleanup();
          queue.fail(slot.url);
          transaction.failedURLs.push(slot.url);
          if(slot.attempts < 3) {
            var replacement = reserve(1, transaction.failedURLs);
            if(replacement.length) {
              slot.url = replacement[0];
              loadSlot(transaction, slot);
              return;
            }
          }
          slot.settled = true;
          if(transaction.kind === "initial") {
            completeInitial(transaction);
          } else {
            // No partial Duo and no foreign-library fallback: keep the previous block.
            cancelPending();
            schedule();
          }
        }
        image.onload = function() {
          if(!valid()) return;
          cleanup();
          slot.width = Number(image.naturalWidth || image.width) || 1;
          slot.height = Number(image.naturalHeight || image.height) || 1;
          slot.loaded = slot.settled = true;
          if(transaction.kind === "initial") {
            setPhoto(slot.element, slot);
            queue.commit(slot.url);
            imageReady(slot.element, slot);
            completeInitial(transaction);
          } else {
            activate(transaction);
          }
        };
        image.onerror = failed;
        image.src = slot.url;
      }

      function startTransaction(kind, urls, elements, outgoing) {
        var transaction = {
          kind: kind, generation: generation, cancelled: false, outgoing: outgoing,
          failedURLs: [],
          slots: urls.map(function(url, index) {
            return {url: url, element: elements ? $(elements[index]) : null,
              attempts: 0, deadline: null, image: null, loaded: false, settled: false};
          })
        };
        pending = transaction;
        transaction.slots.forEach(function(slot) { loadSlot(transaction, slot); });
      }

      function setupRows() {
        if(where.children(".row").length) return;
        where.addClass("shiftingtiles");
        for(var i = 0; i < 2; i++) {
          where.prepend("<div class='row'><div class='single'></div><div class='single'></div><div class='dual'><div></div><div></div></div></div>");
        }
        where.append("<div class='loading'>Loading Photos...</div>");
      }

      function fillInitial() {
        if(pending || active || queue.stats().albumSize < 8) return false;
        setupRows();
        var empty = where.find(photoSelector).filter(function() { return !$(this).data("st-photo-url"); });
        if(!empty.length) return false;
        var urls = reserve(empty.length);
        if(!urls.length) return false;
        where.css("visibility", "hidden");
        startTransaction("initial", urls, empty.toArray());
        return true;
      }

      function frame() {
        if(destroyed || document.hidden || pending || active || queue.stats().albumSize < 8) return false;
        if(!initialReady) {
          var filling = fillInitial();
          schedule();
          return filling;
        }
        var boxes = where.find(".single:not(:last-child), .dual:not(:last-child)");
        if(!boxes.length) return false;
        var outgoing = $(boxes.get(Math.floor(Math.random() * boxes.length)));
        var count = outgoing.hasClass("dual") ? 2 : 1;
        var urls = reserve(count);
        if(!urls.length && count === 2) {
          // Nine photos, or the last unused URL of a cycle, require a Single.
          var singles = boxes.filter(".single");
          if(!singles.length) return false;
          outgoing = $(singles.get(Math.floor(Math.random() * singles.length)));
          urls = reserve(1);
        }
        if(!urls.length) return false;
        clearTimer();
        startTransaction("transition", urls, null, outgoing);
        schedule();
        return true;
      }

      function finishAnimation() {
        if(!active) return;
        var outgoing = active.outgoing;
        active = null;
        outgoing.css("display", "none").remove();
        where.trigger("st-animate-after");
        schedule();
      }

      function clearPhotos() {
        finishAnimation();
        where.find(photoSelector).each(function() {
          $(this).css("background-image", "none")
            .removeData("st-photo-url").removeData("st-natural-width").removeData("st-natural-height")
            .removeData("st-image-request").removeData("st-image-loaded-request");
        });
        initialReady = false;
      }

      function update(event, nextImages) {
        nextImages = Array.isArray(nextImages) ? nextImages : [];
        queue.setImages(nextImages);
        updateMembers(nextImages);
        if(!queue.stats().albumSize) {
          generation++;
          cancelPending();
          clearTimer();
          clearPhotos();
          return;
        }
        if(pending && pending.slots.some(function(slot) { return !members[slot.url]; })) cancelPending();
        if(!initialReady && !pending && !active) fillInitial();
        schedule();
      }

      function destroy() {
        if(destroyed) return;
        destroyed = true;
        generation++;
        clearTimer();
        cancelPending();
        finishAnimation();
        where.off(namespace);
        $(document).off(namespace);
        $(document.body).off(namespace);
        where.removeData("shiftingtiles-playback");
      }

      where.data("shiftingtiles-playback", {
        stats: function() {
          var state = queue.stats();
          state.visibleURLs = visibleURLs();
          state.loading = !!pending;
          state.animating = !!active;
          return state;
        },
        frame: frame,
        destroy: destroy
      });
      where.on("st-photos-update" + namespace, update);
      where.on("animationend" + namespace + " webkitAnimationEnd" + namespace + " oAnimationEnd" + namespace,
        ".leave > .row", function() { $(this).parent().addClass("left").removeClass("leave"); });
      where.on("animationend" + namespace + " webkitAnimationEnd" + namespace + " oAnimationEnd" + namespace,
        ".disappear", function(event) {
          if(active && active.outgoing[0] === this) {
            finishAnimation();
            event.stopPropagation();
          }
        });
      $(document.body).on("keydown" + namespace, function(event) {
        if(event.keyCode === 32) {
          frame();
          event.preventDefault();
          return false;
        }
        if(event.keyCode === 38) where.toggleClass("leave");
      });
      $(document).on("visibilitychange" + namespace, function() {
        if(document.hidden) {
          clearTimer();
          finishAnimation();
        } else {
          if(pending && pending.kind === "transition") activate(pending);
          schedule();
        }
      });
      fillInitial();
      schedule();
    });
  };
})(jQuery);

if (!Array.prototype.reduce) {
  Array.prototype.reduce = function reduce(accumulator) {
    if(this === null || this === undefined) throw new TypeError("Object is null or undefined");
    var i = 0, l = this.length >> 0, curr;
    if(typeof accumulator !== "function") throw new TypeError("First argument is not callable");
    if(arguments.length < 2) {
      if(l === 0) throw new TypeError("Array length is 0 and no second argument");
      curr = this[0];
      i = 1;
    } else {
      curr = arguments[1];
    }
    while(i < l) {
      if(i in this) curr = accumulator.call(undefined, curr, this[i], i, this);
      ++i;
    }
    return curr;
  };
}
