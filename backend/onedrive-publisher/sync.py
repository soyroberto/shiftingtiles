"""Upload intent -> durable complete publication intent -> manifest -> cleanup.

Every public image is journaled privately BEFORE upload; every deletion is durable
and runs only AFTER a complete manifest has been published and committed privately.
"""
import copy
import logging

from auth import silent_token
from config import PublisherError, TYPES, content_version, manifest, photo_path
from graph import GraphClient

LOG = logging.getLogger("onedrive_publisher")


def recover_publication(store, state):
    tx = state["transaction"]
    if tx is None:
        return
    # Idempotent after a crash between public publication and private commit.
    store.publish(tx["manifest"])
    state["libraries"] = tx["libraries"]
    active = set(tx["manifest"]["images"])
    state["pending_cleanup"] = sorted(set(state["managed"]) - active)
    state["transaction"] = None
    store.save(state)  # If this fails, DO NOT delete anything.


def commit_publication(store, state, libs):
    state["transaction"] = {"libraries": libs, "manifest": manifest(libs)}
    store.save(state)  # Durable publication intent before touching gallery.json.
    recover_publication(store, state)


def cleanup(store, state):
    active = set(manifest(state["libraries"])["images"])
    if state["transaction"] is not None:
        raise PublisherError("uncommitted_publication")
    for path in list(state["pending_cleanup"]):
        if path in active or path not in state["managed"]:
            raise PublisherError("unsafe_cleanup_state")
        try:
            store.delete(path)
        except Exception:
            LOG.warning("Managed copy cleanup deferred")  # No exception text/URLs.
            continue
        state["pending_cleanup"].remove(path)
        state["managed"].remove(path)
        store.save(state)  # Crash after delete is harmless: missing deletes are idempotent.


def sync_locked(store, state, config, graph_factory=GraphClient):
    if not config.enabled or not state["connected"]:
        return {"status": "disabled" if not config.enabled else "disconnected"}
    recover_publication(store, state)
    token = silent_token(store, state, config.client_id)
    graph = graph_factory(token)
    target = copy.deepcopy(state["libraries"])
    scans = {}
    failures = 0
    summary = {}
    try:
        # Exhaust every configured folder's pagination BEFORE any deletion is possible.
        for lib in config.libraries:
            if not lib.enabled:
                continue  # Disabled/removed library records deliberately retained.
            try:
                scans[lib.id] = graph.scan(lib)
            except Exception:
                failures += 1
                LOG.warning("Library scan failed; last good list retained")
        for lib in config.libraries:
            if lib.id not in scans:
                continue
            rows, skipped = scans[lib.id]
            items = {}
            try:
                for row, ext in rows:
                    version = content_version(row)
                    path = photo_path(lib.id, row["id"], version, ext)
                    previous = state["libraries"].get(lib.id, {}).get("items", {}).get(row["id"])
                    reusable = previous and previous["version"] == version and previous["path"] == path
                    if not reusable or not store.exists(path):
                        content = graph.download(row, ext)
                        if path not in state["managed"]:
                            if store.exists(path):
                                raise PublisherError("foreign_copy_collision")
                            state["managed"].append(path)
                            store.save(state)  # Upload intent survives upload or later scan failure.
                        store.upload(path, content, TYPES[ext])
                    items[row["id"]] = {"path": path, "version": version}
                target[lib.id] = {"items": items, "source_path": lib.path, "recursive": lib.recursive, "skipped": skipped}
                summary[lib.id] = {"images": len(items), "skipped": skipped}
            except Exception:
                failures += 1
                LOG.warning("Library download/upload failed; last good list retained")
                # Orphan intents remain managed and are queued by a later successful commit.
        if summary:
            commit_publication(store, state, target)
            cleanup(store, state)
        # If every library failed/no enabled libraries, do not publish or delete.
        return {"status": "partial" if failures else "ok", "libraries": summary, "failed": failures,
                "pending_cleanup": len(state["pending_cleanup"])}
    finally:
        graph.close()


def clear_published(store, state):
    # Call only after CLI --confirm validation. Also disconnect to prevent repopulation.
    # Journal all known intents, including an interrupted previous publication.
    state["connected"] = False
    state["cache"] = None
    state["client_id"] = None
    state["transaction"] = {"libraries": {}, "manifest": manifest({})}
    store.save(state)
    recover_publication(store, state)
    cleanup(store, state)
    return len(state["pending_cleanup"])
