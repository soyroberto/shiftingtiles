"""Offline engine fixtures exercise real protocol, without production adapter stubs."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

import resumable
from config import Library, PublisherError, content_version, photo_path
from helpers import FakeStore
from test_sync import JPEG, seed


class Crash(BaseException):
    """Process termination: must bypass normal checkpoint/error handling."""


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value


def config(**changes):
    values = dict(enabled=True, libraries=[Library("spotify", "Musica/Spotify")],
                  client_id="client", account_url="https://example.invalid", state_container="private",
                  batch_size=64, max_items=100000, max_pages=500, run_seconds=180,
                  retain_initial=False)
    values.update(changes)
    return SimpleNamespace(**values)


def row(ident="photo", tag="one", ext="jpg"):
    from config import TYPES
    return {"id": ident, "size": len(JPEG), "cTag": tag, "eTag": tag,
            "file": {"mimeType": TYPES[ext]}, "ext": ext}


def path(source, lib="spotify"):
    return photo_path(lib, source["id"], content_version(source), source["ext"])


def cursor(index, root="root"):
    return f"https://graph.microsoft.com/v1.0/me/drive/items/{root}/children?$skiptoken={index}"


def page(rows, next_link=None, extra_ids=None, skipped=None):
    ids = [source["id"] for source in rows] + (extra_ids or [])
    return {"rows": copy.deepcopy(rows), "ids": ids, "next": next_link,
            "entries": len(ids), "skipped": skipped or {}}


class BatchMemory(FakeStore):
    def __init__(self):
        super().__init__()
        self.control = None
        self.chunks = {}
        self.metadata = {}
        self.loads = 0
        self.control_saves = 0
        self.listings = 0
        self.heads = 0
        self.uploads = []
        self.chunk_hook = None
        self.control_hook = None
        self.upload_hook = None
        self.publications = 0
        self.durable.update(connected=True, cache="{}", client_id="client", owner="owner")

    def load(self):
        self.loads += 1
        return super().load()

    def load_control(self):
        return copy.deepcopy(self.control)

    def save_control(self, control):
        resumable.validate_control(control)
        assert len(json.dumps(control).encode()) < 256 * 1024
        self.control_saves += 1
        if self.control_hook:
            self.control_hook(control)
        self.control = copy.deepcopy(control)

    def put_chunk(self, job_id, kind, index, payload):
        assert 0 <= index <= 999999
        name = f"jobs/{job_id}/{kind}/{index:06d}.json"
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        if name in self.chunks and self.chunks[name][0] != digest:
            raise PublisherError("private_chunk_collision")
        self.chunks[name] = (digest, copy.deepcopy(payload))
        ref = {"name": name, "sha256": digest}
        if self.chunk_hook:
            self.chunk_hook(kind, ref, payload)
        return ref

    def read_chunk(self, ref):
        digest, payload = self.chunks[ref["name"]]
        if digest != ref["sha256"]:
            raise PublisherError("private_chunk_digest_mismatch")
        return copy.deepcopy(payload)

    def public_inventory(self, library_id):
        self.listings += 1
        return {name: {"bytes": len(content), "sha256": self.metadata.get(name)}
                for name, content in self.blobs.items()
                if name.startswith(f"photos/onedrive/{library_id}/")}

    def inspect_original(self, name):
        self.heads += 1
        raise AssertionError("No per-photo HEAD permitted")

    def upload_original(self, name, content, mime, digest):
        job = self.control["job"]
        assert job["intent_ref"] is not None
        intent = self.read_chunk(job["intent_ref"])
        assert name in [rec["path"] for rec in intent["records"]], "Intent durable BEFORE original copy"
        # A previously published, manually missing path may already be managed;
        # a new path must not trigger any primary-state save per photo.
        if name not in self.durable["managed"]:
            assert self.durable["transaction"] is None
        assert digest == hashlib.sha256(content).hexdigest()
        if name in self.blobs:
            assert self.metadata[name] == digest
            assert self.blobs[name] == content
        self.blobs[name] = content
        self.metadata[name] = digest
        self.uploads.append(name)
        if self.upload_hook:
            self.upload_hook(name)

    def publish(self, data):
        assert self.control["job"]["phase"] == "publish"
        super().publish(data)
        self.publications += 1

    def delete(self, name):
        raise AssertionError("Resumable jobs must never delete")


class Pages:
    def __init__(self, pages, clock=None, scan_cost=0, download_cost=0, root=None):
        self.pages = pages
        self.clock = clock
        self.scan_cost = scan_cost
        self.download_cost = download_cost
        self.root_value = root or {"id": "root", "drive_id": "drive"}
        self.scans = []
        self.downloads = []
        self.closed = False
        self.retry_not_before = None
        self.expected_root = None
        self.download_hook = None
        self.scan_hook = None
        self.deadline = None

    def root(self, library):
        return self.root_value

    def scan_page(self, root, next_cursor):
        self.scans.append(next_cursor)
        if self.scan_hook:
            self.scan_hook(next_cursor)
        if self.clock:
            self.clock.value += self.scan_cost
        index = 0 if next_cursor is None else int(next_cursor.split("=")[-1])
        result = self.pages[index]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def download(self, source, ext):
        assert self.expected_root == self.root_value
        self.downloads.append(source["id"])
        if self.clock:
            self.clock.value += self.download_cost
        if self.download_hook:
            self.download_hook(source)
        return JPEG

    def close(self):
        self.closed = True


@pytest.fixture
def batch():
    return BatchMemory()


@pytest.fixture(autouse=True)
def auth_offline(monkeypatch):
    def auth(store, state, client):
        assert client == state["client_id"]
        store.save(state)  # Real auth saves once per invocation, even if Graph fails.
        return "ephemeral-token-never-persisted"
    monkeypatch.setattr(resumable, "silent_token", auth)


def run(batch, graph=None, cfg=None, clock=None, **kwargs):
    clock = clock or Clock()
    def factory(token, deadline=None):
        assert token == "ephemeral-token-never-persisted"
        graph.deadline = deadline
        return graph
    return resumable.run_resumable(batch, cfg or config(), graph_factory=factory,
                                   clock=clock, now=lambda: 1000 + clock.value, **kwargs)


def test_large_nonrecursive_inventory_over_10000_originals(batch):
    rows = [row(f"item-{index}") for index in range(10037)]
    pages = []
    for start in range(0, len(rows), 200):
        index = len(pages)
        more = cursor(index + 1) if start + 200 < len(rows) else None
        pages.append(page(rows[start:start + 200], more))
    graph = Pages(pages)
    result = run(batch, graph)
    assert result == {"status": "ok", "images": 10037, "entries": 10037,
                      "pages": 51, "pending_cleanup": 0}
    assert len(graph.downloads) == len(batch.uploads) == 10037
    assert len(batch.durable["managed"]) == 10037
    assert batch.saves == 3  # Auth, final transaction intent, final commit; NEVER per photo.
    assert batch.listings == 1 and batch.heads == 0
    assert batch.control["job"] is None and graph.closed
    assert all(content == JPEG for content in batch.blobs.values())
    assert batch.publications == 1


def test_unchanged_hourly_run_no_download_head_or_publish(batch):
    graph = Pages([page([row()])])
    assert run(batch, graph)["status"] == "ok"
    first_manifest = copy.deepcopy(batch.public_manifest)
    graph = Pages([page([row()])])
    assert run(batch, graph)["status"] == "unchanged"
    assert graph.downloads == [] and batch.listings == 2 and batch.heads == 0
    assert batch.publications == 1 and batch.public_manifest == first_manifest
    assert batch.saves == 4


@pytest.mark.parametrize("initial", [False, True])
def test_idle_continuation_never_loads_primary_auth_or_graph(batch, monkeypatch, initial):
    if initial:
        run(batch, Pages([page([])]))
    before = (batch.loads, batch.saves, batch.control_saves)
    monkeypatch.setattr(resumable, "silent_token", lambda *_: pytest.fail("No idle auth"))
    result = run(batch, start_new=False)
    assert result == {"status": "idle"}
    assert (batch.loads, batch.saves, batch.control_saves) == before


def test_scan_deadline_checkpoint_then_resume_without_repeating_page(batch):
    clock = Clock()
    pages = [page([row("first")], cursor(1)), page([row("last")])]
    graph = Pages(pages, clock, scan_cost=171)
    assert run(batch, graph, clock=clock)["status"] == "deferred"
    assert batch.control["job"]["pagecount"] == 1
    assert batch.control["job"]["phase"] == "scan"
    assert batch.uploads == [] and batch.public_manifest is None
    graph2 = Pages(pages)
    assert run(batch, graph2, start_new=False)["status"] == "ok"
    assert graph2.scans == [cursor(1)] and graph2.downloads == ["first", "last"]


def test_copy_budget_flushes_partial_prefix_without_primary_photo_saves(batch):
    clock = Clock()
    sources = [row("a"), row("b"), row("c"), row("d")]
    graph = Pages([page(sources)], clock, download_cost=60)
    assert run(batch, graph, clock=clock)["status"] == "deferred"
    job = batch.control["job"]
    assert job["copy_offset"] == 2 and job["intent_ref"] is not None
    assert len(batch.uploads) == 2 and batch.saves == 1
    assert batch.public_manifest is None
    graph2 = Pages([page(sources)])
    assert run(batch, graph2, start_new=False)["status"] == "ok"
    assert graph2.scans == [] and graph2.downloads == ["c", "d"]
    assert len(batch.uploads) == 4 and batch.saves == 4


def test_crash_after_upload_reconciles_ambiguous_sha_once(batch):
    sources = [row("a"), row("b")]
    batch.upload_hook = lambda _: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        run(batch, Pages([page(sources)]))
    assert len(batch.blobs) == 1 and batch.control["job"]["copy_refs"] == []
    batch.upload_hook = None
    graph = Pages([page(sources)])
    assert run(batch, graph, start_new=False)["status"] == "ok"
    assert graph.downloads == ["a", "b"]
    assert len(batch.uploads) == 2  # The first existing upload is proven, not overwritten.
    assert batch.heads == 0


def test_completed_batches_survive_process_crash_without_redownload(batch):
    cfg = config(batch_size=2)
    sources = [row("a"), row("b"), row("c")]
    def crash_intent(kind, ref, payload):
        if kind == "intent" and payload["records"][0]["id"] == "c":
            raise Crash()
    batch.chunk_hook = crash_intent
    with pytest.raises(Crash):
        run(batch, Pages([page(sources)]), cfg)
    assert batch.control["job"]["copy_offset"] == 2
    batch.chunk_hook = None
    graph = Pages([page(sources)])
    assert run(batch, graph, cfg, start_new=False)["status"] == "ok"
    assert graph.downloads == ["c"]


def test_put_before_checkpoint_crash_changed_page_uses_new_immutable_slot(batch, monkeypatch):
    slots = iter([7, 7, 7, 8, 9, 10, 11, 12])
    monkeypatch.setattr(resumable.secrets, "randbelow", lambda _: next(slots))
    def crash(kind, ref, payload):
        if kind == "scan":
            raise Crash()
    batch.chunk_hook = crash
    with pytest.raises(Crash):
        run(batch, Pages([page([row("old")])]))
    orphan = [name for name in batch.chunks if "/scan/" in name][0]
    old_digest = batch.chunks[orphan][0]
    assert batch.control["job"]["pagecount"] == 0
    batch.chunk_hook = None
    graph = Pages([page([row("changed")])])
    assert run(batch, graph, start_new=False)["status"] == "ok"
    assert batch.chunks[orphan][0] == old_digest
    assert len([name for name in batch.chunks if "/scan/" in name]) == 2
    assert graph.downloads == ["changed"]


def test_random_last_valid_chunk_slot(batch, monkeypatch):
    monkeypatch.setattr(resumable.secrets, "randbelow", lambda _: 999999)
    assert run(batch, Pages([page([row()])]))["status"] == "ok"
    assert all(name.endswith("/999999.json") for name in batch.chunks)


def test_publication_crash_recovery_does_not_download_or_republish_after_private_commit(batch):
    def crash(control):
        if control["job"] is None and batch.publications:
            raise Crash()
    batch.control_hook = crash
    with pytest.raises(Crash):
        run(batch, Pages([page([row()])]))
    assert batch.control["job"]["phase"] == "publish"
    batch.control_hook = None
    graph = Pages([page([row()])])
    assert run(batch, graph, start_new=False)["status"] == "unchanged"
    assert graph.scans == graph.downloads == []
    assert batch.publications == 1


def test_publication_failure_uses_legacy_durable_transaction_and_recovers(batch):
    old = seed(batch)
    batch.fail_publish = True
    assert run(batch, Pages([page([row()])]))["status"] == "failed"
    assert batch.public_manifest["images"] == [old]
    assert batch.durable["transaction"] is not None
    assert len(batch.durable["managed"]) == 2
    batch.fail_publish = False
    graph = Pages([page([row()])])
    assert run(batch, graph, start_new=False)["status"] == "ok"
    assert graph.downloads == [] and graph.scans == []
    assert batch.durable["pending_cleanup"] == [old]
    assert old in batch.blobs


@pytest.mark.parametrize("bad", [PublisherError("incomplete_page"),
    page([row("repeat")], cursor(1)), page([row("z")], "https://evil.invalid/?secret=abc")])
def test_incomplete_duplicate_and_unsafe_pages_preserve_last_good(batch, bad):
    old = seed(batch)
    before = copy.deepcopy(batch.public_manifest)
    pages = [page([row("repeat")], cursor(1)), bad]
    result = run(batch, Pages(pages))
    assert result["status"] == "aborted"
    assert batch.public_manifest == before and old in batch.blobs
    assert batch.uploads == []


def test_duplicate_skipped_entry_global_guard(batch):
    graph = Pages([page([], cursor(1), ["folder"], {"folder": 1}),
                   page([], None, ["folder"], {"other": 1})])
    assert run(batch, graph)["status"] == "aborted"
    assert batch.public_manifest is None


@pytest.mark.parametrize("cfg,pages", [
    (config(max_items=1), [page([row("a"), row("b")])]),
    (config(max_pages=1), [page([row("a")], cursor(1)), page([])]),
])
def test_bounds_never_publish_or_copy_partial_scan(batch, cfg, pages):
    assert run(batch, Pages(pages), cfg)["status"] == "aborted"
    assert batch.uploads == [] and batch.public_manifest is None


@pytest.mark.parametrize("cfg", [config(libraries=[]), config(libraries=[Library("a", "A"), Library("b", "B")]),
    config(libraries=[Library("a", "A", recursive=True)]), config(batch_size=101),
    config(max_items=100001), config(max_pages=501), config(run_seconds=181)])
def test_invalid_configuration_fails_closed_before_primary_or_graph(batch, cfg):
    assert run(batch, cfg=cfg)["status"] == "failed"
    assert batch.loads == batch.saves == 0


@pytest.mark.parametrize("field", ["owner", "path", "library", "root"])
def test_owner_config_or_root_changes_never_adopt_or_publish(batch, field):
    clock = Clock()
    graph = Pages([page([row("a"), row("b")])], clock, download_cost=90)
    assert run(batch, graph, clock=clock)["status"] == "deferred"
    assert len(batch.blobs) == 1
    cfg = config()
    next_graph = Pages([page([row("a"), row("b")])])
    if field == "owner":
        batch.durable["owner"] = "different-owner"
    elif field == "path":
        cfg.libraries = [Library("spotify", "Other")]
    elif field == "library":
        cfg.libraries = [Library("different", "Musica/Spotify")]
    else:
        next_graph.root_value = {"id": "different-root", "drive_id": "drive"}
    assert run(batch, next_graph, cfg, start_new=False)["status"] == "aborted"
    assert batch.control["job"] is None
    assert len(batch.control["completed"]["ownership_refs"]) == 1
    assert batch.public_manifest is None and len(batch.uploads) == 1


def test_foreign_existing_safe_path_never_adopted_even_with_matching_sha(batch):
    source = row()
    batch.blobs[path(source)] = JPEG
    batch.metadata[path(source)] = hashlib.sha256(JPEG).hexdigest()
    graph = Pages([page([source])])
    result = run(batch, graph)
    assert result == {"status": "failed", "reason": "foreign_copy_collision"}
    assert graph.downloads == [] and batch.uploads == [] and batch.public_manifest is None
    assert batch.durable["managed"] == []


def test_staged_copy_metadata_mismatch_cannot_be_adopted(batch):
    batch.upload_hook = lambda _: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        run(batch, Pages([page([row()])]))
    batch.upload_hook = None
    batch.metadata[path(row())] = "f" * 64
    graph = Pages([page([row()])])
    assert run(batch, graph, start_new=False)["reason"] == "foreign_copy_collision"
    assert batch.public_manifest is None


def test_download_race_aborts_but_preserves_private_staged_orphan_proof(batch):
    sources = [row("a"), row("b")]
    graph = Pages([page(sources)])
    def race(source):
        if source["id"] == "b":
            raise PublisherError("download_race_after")
    graph.download_hook = race
    assert run(batch, graph)["status"] == "aborted"
    assert len(batch.uploads) == 1 and batch.control["job"] is None
    assert len(batch.control["completed"]["ownership_refs"]) == 1
    graph2 = Pages([page(sources)])
    assert run(batch, graph2)["status"] == "ok"
    assert graph2.downloads == ["b"]  # Aborted completed copy has private SHA proof.
    assert len(batch.uploads) == 2


def test_retry_after_utc_honored_without_primary_auth_or_graph(batch, monkeypatch):
    graph = Pages([page([row()])])
    def defer(_):
        graph.retry_not_before = 5000
        raise PublisherError("retry_budget")
    graph.scan_hook = defer
    assert run(batch, graph) == {"status": "deferred", "retry_at": 5000}
    before = batch.loads, batch.saves
    monkeypatch.setattr(resumable, "silent_token", lambda *_: pytest.fail("early retry auth"))
    assert run(batch, start_new=False) == {"status": "deferred", "retry_at": 5000}
    assert (batch.loads, batch.saves) == before


def test_expired_integral_budget_never_loads_any_state(batch):
    assert run(batch, deadline=0) == {"status": "deferred"}
    assert batch.loads == batch.control_saves == 0


def test_initial_six_retained_once_not_subfolder_scanned_or_growing(batch):
    retained, items = [], {}
    for index in range(6):
        retained.append(seed(batch, ident=f"initial-{index}", tag="fixed"))
        items.update(batch.durable["libraries"]["spotify"]["items"])
    batch.durable["libraries"]["spotify"]["items"] = items
    cfg = config(retain_initial=True)
    graph = Pages([page([row("root-photo")], extra_ids=["nested"], skipped={"folder": 1})])
    assert run(batch, graph, cfg)["images"] == 7
    bootstrap = copy.deepcopy(batch.control["retained"])
    assert set(batch.public_manifest["images"]) == set(retained + [path(row("root-photo"))])
    assert graph.scans == [None] and graph.downloads == ["root-photo"]
    graph = Pages([page([row("new-root-photo")])])
    assert run(batch, graph, cfg)["images"] == 7
    assert batch.control["retained"] == bootstrap
    assert set(batch.public_manifest["images"]) == set(retained + [path(row("new-root-photo"))])
    assert path(row("root-photo")) in batch.durable["pending_cleanup"]
    assert len(batch.blobs) == 8  # Old copies deferred; no implicit deletion.


def test_retained_same_id_new_version_keeps_both_fixed_and_current(batch):
    original = seed(batch, ident="photo", tag="one")
    graph = Pages([page([row("photo", "two")])])
    assert run(batch, graph, config(retain_initial=True))["status"] == "ok"
    assert set(batch.public_manifest["images"]) == {original, path(row("photo", "two"))}


def test_unsupported_sources_skipped_without_ext_or_source_names_persisted(batch):
    graph = Pages([page([row(ext=ext, ident=ext) for ext in ("jpg", "jpeg", "png", "gif", "webp", "avif")],
                       extra_ids=["unsupported", "nested"], skipped={"other": 1, "folder": 1})])
    assert run(batch, graph)["images"] == 6
    serialized = json.dumps([batch.control, batch.chunks, batch.durable])
    assert "name\": \"private" not in serialized and "downloadUrl" not in serialized
    assert "ephemeral-token" not in serialized
    assert set(name.rsplit(".", 1)[1] for name in batch.blobs) == {"jpg", "jpeg", "png", "gif", "webp", "avif"}


def test_exception_urls_names_and_tokens_never_returned_or_logged(batch, caplog):
    graph = Pages([PublisherError("https://signed.invalid/?token=secret private title.JPG")])
    result = run(batch, graph)
    assert result == {"status": "failed"}
    assert "secret" not in str(result) + caplog.text
    assert "private title" not in json.dumps([batch.control, batch.chunks])


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(extra="private name"),
    lambda c: c.update(version=True),
    lambda c: c["job"].update(next_cursor="https://evil.invalid/?secret=yes"),
    lambda c: c["job"].update(root={"id": "../bad", "drive_id": "drive"}),
    lambda c: c["job"].update(retry_at=float("nan")),
    lambda c: c["job"].update(copy_page=True),
    lambda c: c["job"]["scan_refs"][0].update(name="jobs/" + "f" * 32 + "/scan/000000.json"),
])
def test_strict_corrupt_control_fails_before_primary_or_graph(batch, mutation):
    clock = Clock()
    run(batch, Pages([page([row()], cursor(1)), page([])], clock, scan_cost=171), clock=clock)
    mutation(batch.control)
    before = batch.loads, batch.saves
    assert run(batch, start_new=False)["status"] == "failed"
    assert (batch.loads, batch.saves) == before


def test_scan_chunk_hash_tampering_is_fail_safe(batch):
    clock = Clock()
    run(batch, Pages([page([row()], cursor(1)), page([])], clock, scan_cost=171), clock=clock)
    ref = batch.control["job"]["scan_refs"][0]
    batch.chunks[ref["name"]] = ("f" * 64, {})
    before = copy.deepcopy(batch.public_manifest)
    assert run(batch, Pages([page([])]), start_new=False)["status"] == "failed"
    assert batch.public_manifest == before and batch.uploads == []


def test_intent_put_before_control_crash_never_uploads_and_can_resume(batch):
    def crash(kind, ref, payload):
        if kind == "intent":
            raise Crash()
    batch.chunk_hook = crash
    with pytest.raises(Crash):
        run(batch, Pages([page([row()])]))
    assert batch.uploads == [] and batch.control["job"]["intent_ref"] is None
    batch.chunk_hook = None
    graph = Pages([page([row()])])
    assert run(batch, graph, start_new=False)["status"] == "ok"
    assert graph.scans == [] and graph.downloads == ["photo"]


def test_intent_checkpoint_failure_blocks_all_uploads(batch):
    def fail(control):
        if control["job"] and control["job"]["intent_ref"] is not None:
            raise PublisherError("simulated_durability_error")
    batch.control_hook = fail
    assert run(batch, Pages([page([row()])]))["status"] == "failed"
    assert batch.uploads == [] and batch.public_manifest is None
    assert batch.control["job"]["intent_ref"] is None


def test_copy_chunk_put_before_checkpoint_crash_uses_existing_intent_not_orphan_ref(batch):
    def crash(kind, ref, payload):
        if kind == "copy":
            raise Crash()
    batch.chunk_hook = crash
    with pytest.raises(Crash):
        run(batch, Pages([page([row()])]))
    assert batch.control["job"]["copy_refs"] == []
    assert batch.control["job"]["intent_ref"] is not None
    batch.chunk_hook = None
    graph = Pages([page([row()])])
    assert run(batch, graph, start_new=False)["status"] == "ok"
    assert len(batch.uploads) == 1 and graph.downloads == ["photo"]


def test_final_private_commit_crash_reuses_durable_manifest_transaction(batch):
    batch.fail_save_at = 3  # Auth, publication intent, final primary commit.
    assert run(batch, Pages([page([row()])]))["status"] == "failed"
    assert batch.public_manifest["images"] == [path(row())]
    assert batch.durable["transaction"] is not None
    batch.fail_save_at = None
    graph = Pages([page([row()])])
    assert run(batch, graph, start_new=False)["status"] == "ok"
    assert graph.downloads == graph.scans == []
    assert batch.durable["transaction"] is None


def test_manually_missing_known_copy_restored_without_manifest_republication(batch):
    run(batch, Pages([page([row()])]))
    del batch.blobs[path(row())]
    graph = Pages([page([row()])])
    assert run(batch, graph)["status"] == "unchanged"
    assert graph.downloads == ["photo"] and batch.publications == 1
    assert batch.blobs[path(row())] == JPEG


def test_empty_complete_scan_defers_cleanup_without_deleting_existing_originals(batch):
    old = seed(batch)
    assert run(batch, Pages([page([])]))["status"] == "ok"
    assert batch.public_manifest["images"] == []
    assert batch.durable["pending_cleanup"] == [old] and old in batch.blobs


def test_control_copy_index_bounded_for_100000_tiny_batches(batch):
    # The index itself is the scale fixture here: no 100000 needless remote IOs.
    # Its radix structure must cover 100000 distinct refs without a linear chain.
    job = {"id": "a" * 32, "copy_refs": []}
    for index in range(100000):
        ref = {"name": f"jobs/{job['id']}/copy/{index:06d}.json", "sha256": "b" * 64}
        resumable._append(batch, job, "copy", ref)
    assert len(job["copy_refs"]) < 128
    assert len(json.dumps(job).encode()) < 256 * 1024
    levels = [payload["level"] for _, payload in batch.chunks.values()]
    assert max(levels) <= 3
    count = 0
    def walk(ref):
        nonlocal count
        if "/copy/" in ref["name"]:
            count += 1
            return
        node = batch.read_chunk(ref)
        for child in node["refs"]:
            walk(child)
    for ref in job["copy_refs"]:
        walk(ref)
    assert count == 100000


def test_ownership_archive_capacity_fails_closed_without_discarding_orphans(batch):
    clock = Clock()
    graph = Pages([page([row("a"), row("b")])], clock, download_cost=90)
    run(batch, graph, clock=clock)
    batch.control["completed"]["ownership_refs"] = [
        {"name": f"jobs/{index:032x}/snapshot/000000.json", "sha256": "a" * 64}
        for index in range(128)]
    cfg = config(libraries=[Library("spotify", "Changed")])
    before = copy.deepcopy(batch.control)
    assert run(batch, Pages([page([])]), cfg, start_new=False)["status"] == "failed"
    assert batch.control == before and len(batch.blobs) == 1


def test_download_failure_safe_summary_keeps_last_good_and_completed_prefix(batch):
    old = seed(batch)
    graph = Pages([page([row("a"), row("b")])])
    def fail(source):
        if source["id"] == "b":
            raise RuntimeError("https://signed.invalid/private-title?access_token=secret")
    graph.download_hook = fail
    assert run(batch, graph) == {"status": "failed"}
    assert batch.public_manifest["images"] == [old]
    assert batch.control["job"]["copy_offset"] == 1
    graph2 = Pages([page([row("a"), row("b")])])
    assert run(batch, graph2, start_new=False)["status"] == "ok"
    assert graph2.downloads == ["b"] and old in batch.blobs


def test_full_500_pages_100000_skipped_entries_and_scan_catalog_compaction(batch):
    pages = []
    for index in range(500):
        ids = [f"ignored-{index}-{offset}" for offset in range(200)]
        pages.append(page([], cursor(index + 1) if index < 499 else None,
                          extra_ids=ids, skipped={"other": 200}))
    graph = Pages(pages)
    result = run(batch, graph)
    assert result == {"status": "ok", "images": 0, "entries": 100000,
                      "pages": 500, "pending_cleanup": 0}
    assert graph.downloads == [] and batch.uploads == []
    assert batch.public_manifest["images"] == []
    assert any(payload.get("kind") == "scan" for _, payload in batch.chunks.values()
               if isinstance(payload, dict))


def test_partial_scan_catalog_checkpoint_recovers_after_128_pages(batch):
    pages = [page([], cursor(index + 1) if index < 149 else None,
                  extra_ids=[f"ignored-{index}"], skipped={"folder": 1})
             for index in range(150)]
    clock = Clock()
    graph = Pages(pages, clock, scan_cost=1.3)
    assert run(batch, graph, clock=clock)["status"] == "deferred"
    completed = batch.control["job"]["pagecount"]
    assert completed > 128
    graph2 = Pages(pages)
    assert run(batch, graph2, start_new=False)["status"] == "ok"
    assert graph2.scans[0] == cursor(completed)


def test_completed_sha_ledger_cross_job_reference_is_rejected_before_copy(batch):
    cfg = config(batch_size=1)
    clock = Clock()
    graph = Pages([page([row("a"), row("b")])], clock, download_cost=90)
    run(batch, graph, cfg, clock=clock)
    ref = batch.control["job"]["copy_refs"][0]
    # Repointing private work to another job is never an ownership proof.
    ref["name"] = "jobs/" + "f" * 32 + "/copy/000000.json"
    before = len(batch.uploads), batch.loads
    assert run(batch, Pages([page([])]), cfg, start_new=False)["status"] == "failed"
    assert (len(batch.uploads), batch.loads) == before
