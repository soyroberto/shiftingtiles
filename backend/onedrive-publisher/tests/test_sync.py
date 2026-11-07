import copy
from unittest.mock import Mock
import pytest

from config import Config, Library, PublisherError, content_version, photo_path
from sync import sync_locked, cleanup, clear_published, recover_publication

JPEG = b"\xff\xd8\xfforiginal EXIF GPS bytes unchanged"


def row(ident="photo", tag="one"):
    return {"id": ident, "name": "private title.JPG", "file": {"mimeType": "image/jpeg"},
            "size": len(JPEG), "cTag": tag, "eTag": tag}


def seed(store, lib="spotify", ident="old", tag="old"):
    source = row(ident, tag)
    version = content_version(source)
    path = photo_path(lib, ident, version, "jpg")
    store.durable["connected"] = True
    store.durable["cache"] = "{}"
    store.durable["client_id"] = "client"
    store.durable["owner"] = "owner"
    store.durable["managed"].append(path)
    store.durable["libraries"][lib] = {"items": {ident: {"path": path, "version": version}}}
    store.blobs[path] = JPEG
    store.public_manifest = {"version": 1, "images": list(store.blobs), "updatedAt": "2026-01-01T00:00:00Z"}
    return path


class FakeGraph:
    def __init__(self, scans, store=None):
        self.scans = scans
        self.store = store
        self.closed = False
        self.downloads = []
        self.fail_download = False

    def scan(self, lib):
        if self.store:
            self.store.events.append(("scan", lib.id))
        result = self.scans[lib.id]
        if isinstance(result, Exception):
            raise result
        return result, {}

    def download(self, source, ext):
        if self.fail_download:
            raise PublisherError("download_race")
        self.downloads.append(source["id"])
        return JPEG

    def close(self):
        self.closed = True


def run(monkeypatch, store, graph, libs=None, enabled=True):
    monkeypatch.setattr("sync.silent_token", lambda *_: "not-real-token")
    config = Config(enabled, libs or [Library("spotify", "Musica/Spotify")], "client")
    return sync_locked(store, store.load(), config, graph_factory=lambda token: graph)


def test_success_upload_manifest_then_delete(monkeypatch, store):
    old = seed(store)
    graph = FakeGraph({"spotify": [(row(), "jpg")]}, store)
    result = run(monkeypatch, store, graph)
    assert result["status"] == "ok"
    events = [event[0] for event in store.events]
    assert events.index("scan") < events.index("upload") < events.index("publish") < events.index("delete")
    assert old not in store.blobs
    assert list(store.blobs.values()) == [JPEG]
    public = str(store.public_manifest)
    assert "private title" not in public and "not-real-token" not in public and "photo'" not in public
    assert graph.closed


def test_failed_disabled_removed_libraries_retain_good_items(monkeypatch, store):
    failed = seed(store, "failed")
    disabled = seed(store, "disabled")
    removed = seed(store, "removed")
    empty = seed(store, "empty")
    graph = FakeGraph({"failed": PublisherError("http_404"), "empty": []}, store)
    libs = [Library("failed", "Missing"), Library("disabled", "Old", enabled=False), Library("empty", "Empty")]
    result = run(monkeypatch, store, graph, libs)
    assert result["status"] == "partial"
    assert set(store.public_manifest["images"]) == {failed, disabled, removed}
    assert empty not in store.blobs
    assert set(store.blobs) == {failed, disabled, removed}
    assert [e[0] for e in store.events].index("delete") > max(i for i, e in enumerate(store.events) if e[0] == "scan")


def test_all_scan_errors_do_not_publish_or_delete(monkeypatch, store):
    old = seed(store)
    before = copy.deepcopy(store.public_manifest)
    run(monkeypatch, store, FakeGraph({"spotify": PublisherError("partial_page")}))
    assert store.public_manifest == before and old in store.blobs
    assert not any(e[0] in ("publish", "delete") for e in store.events)


def test_empty_success_publishes_fallback(monkeypatch, store):
    seed(store)
    run(monkeypatch, store, FakeGraph({"spotify": []}))
    assert store.public_manifest["images"] == [] and not store.blobs


@pytest.mark.parametrize("enabled,connected", [(False, True), (True, False)])
def test_global_disable_or_disconnect_is_noop(monkeypatch, store, enabled, connected):
    seed(store)
    store.durable["connected"] = connected
    before = copy.deepcopy(store.durable)
    graph = Mock()
    run(monkeypatch, store, graph, enabled=enabled)
    assert store.durable == before and not store.events
    graph.scan.assert_not_called()


def test_resume_reconciles_missing_sources(monkeypatch, store):
    old = seed(store)
    run(monkeypatch, store, FakeGraph({"spotify": []}), enabled=False)
    assert old in store.blobs
    run(monkeypatch, store, FakeGraph({"spotify": []}))
    assert old not in store.blobs


def test_manually_deleted_public_blob_is_redownloaded(monkeypatch, store):
    path = seed(store, ident="photo", tag="one")
    del store.blobs[path]
    graph = FakeGraph({"spotify": [(row(), "jpg")]})
    run(monkeypatch, store, graph)
    assert graph.downloads == ["photo"] and store.blobs[path] == JPEG


def test_unchanged_existing_copy_avoids_download(monkeypatch, store):
    seed(store, ident="photo", tag="one")
    graph = FakeGraph({"spotify": [(row(), "jpg")]})
    run(monkeypatch, store, graph)
    assert graph.downloads == []


def test_download_error_retains_previous_library(monkeypatch, store):
    old = seed(store)
    graph = FakeGraph({"spotify": [(row(), "jpg")]})
    graph.fail_download = True
    run(monkeypatch, store, graph)
    assert store.public_manifest["images"] == [old] and old in store.blobs
    assert not any(e[0] == "delete" for e in store.events)


def test_durable_upload_intent_survives_upload_failure(monkeypatch, store):
    old = seed(store)
    store.fail_upload = True
    run(monkeypatch, store, FakeGraph({"spotify": [(row(), "jpg")]}))
    assert len(store.durable["managed"]) == 2
    assert store.public_manifest["images"] == [old]
    store.fail_upload = False
    run(monkeypatch, store, FakeGraph({"spotify": []}))
    assert not store.durable["managed"] and not store.blobs


def test_save_failure_before_upload_never_creates_orphan(monkeypatch, store):
    old = seed(store)
    store.fail_save_at = 1
    run(monkeypatch, store, FakeGraph({"spotify": [(row(), "jpg")]}))
    assert set(store.blobs) == {old}
    assert not any(e[0] == "upload" for e in store.events)


def test_manifest_failure_retains_intent_and_old_blobs(monkeypatch, store):
    old = seed(store)
    store.fail_publish = True
    with pytest.raises(PublisherError):
        run(monkeypatch, store, FakeGraph({"spotify": [(row(), "jpg")]}))
    assert store.durable["transaction"] is not None and old in store.blobs
    assert not any(e[0] == "delete" for e in store.events)
    store.fail_publish = False
    state = store.load()
    recover_publication(store, state)
    cleanup(store, state)
    assert old not in store.blobs and store.durable["transaction"] is None


def test_post_manifest_private_commit_failure_never_deletes(monkeypatch, store):
    old = seed(store)
    store.fail_save_at = 3  # image intent, transaction intent, committed state
    with pytest.raises(PublisherError):
        run(monkeypatch, store, FakeGraph({"spotify": [(row(), "jpg")]}))
    assert store.durable["transaction"] is not None and old in store.blobs
    assert not any(e[0] == "delete" for e in store.events)
    store.fail_save_at = None
    state = store.load()
    recover_publication(store, state)
    cleanup(store, state)
    assert old not in store.blobs


def test_delete_failure_keeps_durable_queue(monkeypatch, store):
    old = seed(store)
    store.fail_delete = True
    run(monkeypatch, store, FakeGraph({"spotify": []}))
    assert store.public_manifest["images"] == []
    assert store.durable["pending_cleanup"] == [old] and old in store.blobs
    store.fail_delete = False
    assert clear_published(store, store.load()) == 0
    assert not store.blobs


def test_confirmed_clear_disconnects_and_keeps_original_foreign_files(store):
    old = seed(store)
    store.blobs["f1.jpeg"] = JPEG
    store.blobs["photos/onedrive/foreign/not-a-managed-hash.jpg"] = b"foreign"
    assert clear_published(store, store.load()) == 0
    assert store.public_manifest["images"] == []
    assert old not in store.blobs and "f1.jpeg" in store.blobs
    assert "photos/onedrive/foreign/not-a-managed-hash.jpg" in store.blobs
    assert not store.durable["connected"] and store.durable["cache"] is None
    assert store.durable["owner"] == "owner"


def test_clear_replaces_pending_publication_before_cleanup(monkeypatch, store):
    old = seed(store)
    store.fail_publish = True
    with pytest.raises(PublisherError):
        run(monkeypatch, store, FakeGraph({"spotify": [(row(), "jpg")]}))
    store.fail_publish = False
    clear_published(store, store.load())
    assert not store.blobs and store.public_manifest["images"] == []
    assert not store.durable["transaction"]


def test_pending_transaction_blocks_cleanup(store):
    seed(store)
    store.durable["transaction"] = {"libraries": {}, "manifest": {"images": []}}
    with pytest.raises(PublisherError, match="uncommitted"):
        cleanup(store, store.load())
