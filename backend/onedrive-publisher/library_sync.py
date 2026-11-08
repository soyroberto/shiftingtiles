"""Isolated library publications and fair native-timer coordination.

One primary lease serializes all writers. Credentials remain in the shared primary
state; each engine sees only its own images/transaction and durable work control.
The coordinator starts hourly reconciliations and advances one ready library per
invocation. No deletion, state reset or source writes are performed here.
"""
import copy
from dataclasses import replace
import time

from config import LIBRARY_ID, PublisherError, manifest
from resumable import run_resumable, validate_control
from storage import validate_state

COORDINATOR = "publisher-coordinator.json"
_SHARED = ("version", "owner", "connected", "client_id", "cache")


class LibraryStore:
    def __init__(self, base, library_id):
        if not isinstance(library_id, str) or not LIBRARY_ID.fullmatch(library_id):
            raise PublisherError("unsafe_library_id")
        self.base = base
        self.library_id = library_id
        self.prefix = "photos/onedrive/" + library_id + "/"
        self.manifest_name = self.prefix + "gallery.json"
        # Keep all existing Spotify checkpoints and chunk refs in place.
        self.control_name = ("publisher-work.json" if library_id == "spotify" else
                             "libraries/" + library_id + "/publisher-work.json")
        self._global = None
        self._legacy_transaction = False

    def __getattr__(self, name):
        return getattr(self.base, name)

    def load(self):
        shared = validate_state(self.base.load())
        self._global = copy.deepcopy(shared)
        legacy = shared["transaction"]
        tx = shared.get("library_transactions", {}).get(self.library_id)
        if legacy is not None:
            if set(legacy["libraries"]) != {self.library_id} or tx is not None:
                raise PublisherError("pending_legacy_publication")
            tx = legacy
            self._legacy_transaction = True
        else:
            self._legacy_transaction = False
        state = {field: copy.deepcopy(shared[field]) for field in _SHARED}
        state["libraries"] = ({self.library_id: copy.deepcopy(shared["libraries"][self.library_id])}
                              if self.library_id in shared["libraries"] else {})
        for field in ("managed", "pending_cleanup"):
            state[field] = [path for path in shared[field] if path.startswith(self.prefix)]
        state["transaction"] = copy.deepcopy(tx)
        return validate_state(state)

    def save(self, state):
        validate_state(state)
        if self._global is None:
            raise PublisherError("library_state_not_loaded")
        if (set(state["libraries"]) - {self.library_id} or
                any(not path.startswith(self.prefix) for field in ("managed", "pending_cleanup")
                    for path in state[field]) or
                (state["transaction"] is not None and
                 set(state["transaction"]["libraries"]) != {self.library_id})):
            raise PublisherError("cross_library_state")
        shared = copy.deepcopy(self._global)
        if shared["owner"] != state["owner"]:
            raise PublisherError("library_owner_changed")
        for field in _SHARED:
            shared[field] = copy.deepcopy(state[field])
        if self.library_id in state["libraries"]:
            shared["libraries"][self.library_id] = copy.deepcopy(state["libraries"][self.library_id])
        else:
            shared["libraries"].pop(self.library_id, None)
        for field in ("managed", "pending_cleanup"):
            others = [path for path in shared[field] if not path.startswith(self.prefix)]
            shared[field] = sorted(set(others) | set(state[field]))
        transactions = shared.setdefault("library_transactions", {})
        if state["transaction"] is None:
            transactions.pop(self.library_id, None)
        else:
            transactions[self.library_id] = copy.deepcopy(state["transaction"])
        if self._legacy_transaction:
            shared["transaction"] = None  # Exact legacy tx is now in its named journal.
        validate_state(shared)
        self.base.save(shared)
        self._global = shared
        self._legacy_transaction = False

    def load_control(self):
        return self.base.load_control(name=self.control_name)

    def save_control(self, data):
        self.base.save_control(data, name=self.control_name)

    def public_inventory(self, library_id):
        if library_id != self.library_id:
            raise PublisherError("cross_library_inventory")
        return self.base.public_inventory(library_id)

    def upload_original(self, path, *args, **kwargs):
        if not isinstance(path, str) or not path.startswith(self.prefix):
            raise PublisherError("cross_library_upload")
        return self.base.upload_original(path, *args, **kwargs)

    def publish(self, data):
        if (not isinstance(data, dict) or not isinstance(data.get("images"), list) or
                any(not isinstance(path, str) or not path.startswith(self.prefix)
                    for path in data["images"])):
            raise PublisherError("cross_library_manifest")
        self.base.publish(data, name=self.manifest_name)
        if self.library_id == "spotify":
            # Compatibility alias is exclusively Spotify, never a combined list.
            self.base.publish(data, name="gallery.json")

    def ensure_manifest(self):
        state = self.load()
        if state["transaction"] is not None or self.library_id not in state["libraries"]:
            return
        self.base.guard()
        exists = self.base.public.get_blob_client(self.manifest_name).exists()
        if not exists:
            # Backfill only already-committed images, never a guessed staged prefix.
            self.publish(manifest(state["libraries"]))


def _coordinator(value):
    if value is None:
        return {"version": 1, "last_library": None, "pending_starts": []}
    if (not isinstance(value, dict) or set(value) != {"version", "last_library", "pending_starts"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise PublisherError("invalid_coordinator")
    last, pending = value["last_library"], value["pending_starts"]
    if (last is not None and (not isinstance(last, str) or not LIBRARY_ID.fullmatch(last)) or
            not isinstance(pending, list) or len(pending) > 20 or
            any(not isinstance(ident, str) or not LIBRARY_ID.fullmatch(ident) for ident in pending) or
            len(pending) != len(set(pending))):
        raise PublisherError("invalid_coordinator")
    return value


def run_libraries(store, config, start_new=True, deadline=None, engine=run_resumable,
                  clock=time.monotonic, now=time.time):
    if not config.enabled:
        return {"status": "disabled"}
    libraries = [lib for lib in config.libraries if lib.enabled]
    if not libraries:
        return {"status": "idle"}
    if len(libraries) > 20 or len({lib.id for lib in libraries}) != len(libraries):
        raise PublisherError("invalid_library_configuration")
    end = min(clock() + config.run_seconds, deadline if deadline is not None else clock() + config.run_seconds)
    coordinator = _coordinator(store.load_control(name=COORDINATOR))
    active = {lib.id for lib in libraries}
    coordinator["pending_starts"] = [ident for ident in coordinator["pending_starts"] if ident in active]
    if start_new:
        coordinator["pending_starts"] = list(dict.fromkeys(coordinator["pending_starts"] +
                                                         [lib.id for lib in libraries]))
        store.save_control(coordinator, name=COORDINATOR)
    ready, uninitialized = {}, []
    for lib in libraries:
        if clock() >= end - 25:
            return {"status": "deferred"}
        view = LibraryStore(store, lib.id)
        control = view.load_control()
        if control is not None:
            validate_control(control)
        job = control["job"] if control is not None else None
        pending_start = lib.id in coordinator["pending_starts"]
        if job is None and not pending_start:
            continue
        if job is not None and job["retry_at"] is not None and now() < job["retry_at"]:
            continue
        ready[lib.id] = (lib, view, pending_start)
        if control is None:
            uninitialized.append(lib.id)
    if not ready:
        return {"status": "idle"}
    ids = [lib.id for lib in libraries]
    last = coordinator["last_library"]
    offset = ids.index(last) + 1 if last in ids else 0
    order = ids[offset:] + ids[:offset]
    selected = next((ident for ident in order if ident in uninitialized),
                    next(ident for ident in order if ident in ready))
    lib, view, pending_start = ready[selected]
    # Rotate durably before work; one failed/long library cannot starve the rest.
    coordinator["last_library"] = selected
    store.save_control(coordinator, name=COORDINATOR)
    view.ensure_manifest()
    scoped_config = replace(config, libraries=[lib])
    result = engine(view, scoped_config, start_new=pending_start, deadline=end)
    if result.get("status") in ("ok", "unchanged"):
        coordinator["pending_starts"] = [ident for ident in coordinator["pending_starts"] if ident != selected]
        store.save_control(coordinator, name=COORDINATOR)
    return dict(result, library=selected)
