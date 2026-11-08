"""Progressive mode exercises the real engine and legacy transaction offline."""
import copy
import hashlib
import json

import pytest

import resumable
from config import Library, PublisherError
from helpers import FakeStore
from test_resumable import (
    BatchMemory, Clock, Crash, Pages, auth_offline, config as legacy_config,
    cursor, page, path, row, run as engine_run,
)
from test_sync import JPEG, seed


def config(**changes):
    return legacy_config(progressive=True, skip_invalid=True, **changes)


def run(store, graph=None, cfg=None, **kwargs):
    return engine_run(store, graph, cfg or config(), **kwargs)


class ProgressiveMemory(BatchMemory):
    """Only this fixture permits public batches while the job is still copying."""
    def __init__(self):
        super().__init__()
        self.published_prefixes = []
        self.publish_hook = None

    def publish(self, data):
        job = self.control["job"]
        assert job["phase"] in ("copy", "publish")
        assert job["next_cursor"] is None and job["pagecount"] > 0
        marker = job["publication"]
        assert marker is not None, "Private publication cursor BEFORE primary intent"
        assert marker["position"] == [job["copy_page"], job["copy_offset"]]
        pages, _, _ = resumable._load_pages(self, job, lambda: None)
        records = resumable._completions(self, job, pages, "spotify", lambda: None)
        successes = {rec["path"] for rec in records if "skip" not in rec}
        rejected = {rec["path"] for rec in records if "skip" in rec}
        previous = {item["path"] for library in self.durable["libraries"].values()
                    for item in library["items"].values()}
        retained = resumable._retained(self, self.control)
        initial = {item["path"] for library in retained.values() for item in library["items"].values()}
        assert set(data["images"]) <= successes | previous | initial
        assert not rejected.intersection(data["images"])
        assert not rejected.intersection(self.durable["managed"])
        assert marker["sha256"] == resumable._target_digest(self.durable["transaction"]["libraries"])
        if self.publish_hook:
            self.publish_hook(data)
        FakeStore.publish(self, data)
        self.publications += 1
        self.published_prefixes.append(set(data["images"]))


@pytest.fixture
def batch():
    return ProgressiveMemory()


def bad_signature(source):
    if source["id"].startswith("bad"):
        raise PublisherError("invalid_raster_signature")


def fixed_six(store):
    paths, items = [], {}
    for index in range(6):
        paths.append(seed(store, ident=f"initial-{index}", tag="fixed"))
        items.update(store.durable["libraries"]["spotify"]["items"])
    store.durable["libraries"]["spotify"]["items"] = items
    return set(paths)


def replace_chunk(store, ref, payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    ref["sha256"] = digest
    store.chunks[ref["name"]] = (digest, copy.deepcopy(payload))


def test_first_batch_published_before_next_copy_and_fixed_six_retained(batch):
    initial = fixed_six(batch)
    sources = [row("a"), row("b"), row("c"), row("d")]
    graph = Pages([page(sources)])
    def inspect(source):
        if source["id"] == "c":
            assert set(batch.public_manifest["images"]) == initial | {path(sources[0]), path(sources[1])}
            assert batch.control["job"]["copy_offset"] == 2
            assert batch.durable["transaction"] is None
    graph.download_hook = inspect
    result = run(batch, graph, config(batch_size=2, retain_initial=True))
    assert result["status"] == "ok" and result["phase"] == "complete"
    assert result["copied"] == 4 and result["images"] == 10
    assert batch.publications == 2 and batch.saves == 5
    assert batch.published_prefixes[-1] == initial | {path(source) for source in sources}
    assert graph.skip_invalid is True


def test_deadline_publishes_verified_prefix_then_resumes_without_redownload(batch):
    sources = [row(ident) for ident in "abcd"]
    clock = Clock()
    graph = Pages([page(sources)], clock, download_cost=55)
    result = run(batch, graph, clock=clock)
    assert result["status"] == "deferred" and result["phase"] == "copy"
    assert result["copied"] == result["images"] == 2
    assert result["bytes"] == 2 * len(JPEG)
    assert set(batch.public_manifest["images"]) == {path(source) for source in sources[:2]}
    assert path(sources[2]) not in batch.blobs
    assert batch.control["job"]["copy_offset"] == 2
    assert batch.control["job"]["intent_ref"] is not None
    graph2 = Pages([page(sources)])
    assert run(batch, graph2, start_new=False)["status"] == "ok"
    assert graph2.scans == [] and graph2.downloads == ["c", "d"]
    assert len(batch.uploads) == 4 and batch.control["job"] is None


def test_partial_union_keeps_not_yet_processed_old_versions_and_foreign_library(batch):
    old = seed(batch, ident="a", tag="old")
    waiting = seed(batch, ident="waiting", tag="old")
    batch.durable["libraries"]["spotify"]["items"]["a"] = {
        "path": old, "version": resumable.content_version(row("a", "old"))}
    foreign = seed(batch, lib="foreign", ident="foreign")
    sources = [row("a", "new"), row("b"), row("c")]
    graph = Pages([page(sources)])
    def observe(source):
        if source["id"] == "c":
            assert {old, waiting, foreign, path(sources[0]), path(sources[1])} <= set(batch.public_manifest["images"])
    graph.download_hook = observe
    assert run(batch, graph, config(batch_size=2))["status"] == "ok"
    assert set(batch.public_manifest["images"]) == {foreign} | {path(source) for source in sources}
    assert {old, waiting} <= set(batch.durable["pending_cleanup"])
    assert old in batch.blobs and waiting in batch.blobs


@pytest.mark.parametrize("bad", [
    PublisherError("incomplete_page"),
    page([row("a")]),
    page([row("z")], "https://evil.invalid/?secret=yes"),
])
def test_unsafe_or_incomplete_scan_never_copies_or_publishes(batch, bad):
    old = seed(batch)
    before = copy.deepcopy(batch.public_manifest)
    graph = Pages([page([row("a")], cursor(1)), bad])
    assert run(batch, graph)["status"] == "aborted"
    assert batch.public_manifest == before and old in batch.blobs
    assert graph.downloads == [] and batch.publications == 0


def test_scan_deadline_no_partial_manifest_before_complete_inventory(batch):
    clock = Clock()
    pages = [page([row("a")], cursor(1)), page([row("b")])]
    result = run(batch, Pages(pages, clock, scan_cost=161), clock=clock)
    assert result["status"] == "deferred" and result["phase"] == "scan"
    assert result["scanned"] == 1 and result["copied"] == 0
    assert batch.public_manifest is None and batch.uploads == []
    graph = Pages(pages)
    assert run(batch, graph, start_new=False)["status"] == "ok"
    assert graph.scans == [cursor(1)]


def test_invalid_signature_is_durable_and_excluded_from_target_and_ownership(batch):
    sources = [row("bad-one"), row("good")]
    graph = Pages([page(sources)])
    graph.download_hook = bad_signature
    result = run(batch, graph)
    assert result["status"] == "ok" and result["copied"] == result["images"] == 1
    assert result["omitted"] == 1 and result["reasons"] == {"invalid_raster_signature": 1}
    assert batch.public_manifest["images"] == [path(sources[1])]
    assert path(sources[0]) not in batch.durable["managed"]
    chunks = [payload for _, payload in batch.chunks.values() if "records" in payload and
              any("skip" in rec for rec in payload["records"])]
    assert len(chunks) == 1 and chunks[0]["records"][0]["sha256"] is None
    assert chunks[0]["records"][0]["skip"] == "invalid_raster_signature"
    assert batch.control["completed"]["invalid_ref"] is not None


def test_all_bad_prefix_keeps_last_good_until_later_good_copy(batch):
    old = seed(batch)
    sources = [row("bad-a"), row("bad-b"), row("good")]
    graph = Pages([page(sources)])
    def hook(source):
        if source["id"] == "good":
            assert batch.public_manifest["images"] == [old]
            assert batch.publications == 0
        bad_signature(source)
    graph.download_hook = hook
    result = run(batch, graph, config(batch_size=2))
    assert result["status"] == "ok" and result["omitted"] == 2
    assert batch.publications == 1 and batch.public_manifest["images"] == [path(sources[-1])]
    assert old in batch.blobs


def test_all_invalid_prefix_does_not_create_fake_empty_first_manifest(batch):
    sources = [row("bad-a"), row("bad-b"), row("good")]
    def crash(kind, ref, payload):
        if kind == "intent" and payload["records"][0]["id"] == "good":
            raise Crash()
    batch.chunk_hook = crash
    graph = Pages([page(sources)])
    graph.download_hook = bad_signature
    with pytest.raises(Crash):
        run(batch, graph, config(batch_size=2))
    assert batch.public_manifest is None and batch.publications == 0
    assert batch.control["job"]["copy_offset"] == 2
    batch.chunk_hook = None
    graph2 = Pages([page(sources)])
    assert run(batch, graph2, config(batch_size=2), start_new=False)["status"] == "ok"
    assert graph2.downloads == ["good"]


def test_complete_all_invalid_final_rule_keeps_initial_six(batch):
    initial = fixed_six(batch)
    graph = Pages([page([row("bad-a"), row("bad-b")])])
    graph.download_hook = bad_signature
    result = run(batch, graph, config(retain_initial=True, batch_size=1))
    assert result["status"] == "unchanged"
    assert result["images"] == 6 and result["omitted"] == 2 and result["copied"] == 0
    assert set(batch.public_manifest["images"]) == initial and batch.publications == 0


def test_complete_all_invalid_without_initial_can_publish_safe_final_empty(batch):
    old = seed(batch)
    graph = Pages([page([row("bad-a")])])
    graph.download_hook = bad_signature
    result = run(batch, graph)
    assert result["status"] == "ok" and result["phase"] == "complete"
    assert batch.public_manifest["images"] == [] and batch.durable["pending_cleanup"] == [old]
    assert old in batch.blobs


def test_unchanged_valid_and_invalid_versions_do_not_redownload_or_republish(batch):
    sources = [row("bad-a"), row("good")]
    graph = Pages([page(sources)])
    graph.download_hook = bad_signature
    assert run(batch, graph)["status"] == "ok"
    previous = copy.deepcopy(batch.public_manifest)
    graph2 = Pages([page(sources)])
    graph2.download_hook = bad_signature
    result = run(batch, graph2)
    assert result["status"] == "unchanged" and result["omitted"] == 1
    assert graph2.downloads == [] and batch.publications == 1
    assert batch.public_manifest == previous
    changed = Pages([page([row("bad-a", "new"), row("good")])])
    changed.download_hook = bad_signature
    assert run(batch, changed)["status"] == "unchanged"
    assert changed.downloads == ["bad-a"]


@pytest.mark.parametrize("code", [
    "transport_error", "download_transport_error", "http_403", "http_404", "http_429",
    "quota_exceeded", "download_race_before", "download_race_after", "download_item_changed",
    "download_item_outside_root", "download_size_bound", "lease_lost", "invalid_original_digest",
])
def test_non_validation_errors_are_never_skipped(batch, code):
    old = seed(batch)
    graph = Pages([page([row("photo")])])
    graph.download_hook = lambda source: (_ for _ in ()).throw(PublisherError(code))
    result = run(batch, graph)
    assert result["status"] in ("failed", "aborted")
    assert result["omitted"] == 0 and result["copied"] == 0
    assert batch.public_manifest["images"] == [old] and batch.uploads == []


def test_generic_exception_never_skipped_or_leaked(batch, caplog):
    graph = Pages([page([row("photo")])])
    graph.download_hook = lambda source: (_ for _ in ()).throw(
        RuntimeError("https://signed.invalid/private-title?token=secret"))
    result = run(batch, graph)
    assert result["status"] == "failed" and result["omitted"] == 0
    assert "secret" not in str(result) + caplog.text + json.dumps([batch.control, batch.chunks])
    assert batch.public_manifest is None


@pytest.mark.parametrize("mutation", [
    lambda rec: rec.update(skip="http_404"),
    lambda rec: rec.update(skip="invalid_raster_signature", sha256="f" * 64),
    lambda rec: rec.update(skip=None),
    lambda rec: rec.update(extra="private title"),
    lambda rec: rec.update(bytes=-1),
    lambda rec: rec.update(version="different"),
])
def test_malformed_skip_completion_fails_closed_before_more_downloads(batch, mutation):
    sources = [row("bad-a"), row("good")]
    def crash(kind, ref, payload):
        if kind == "intent" and payload["records"][0]["id"] == "good":
            raise Crash()
    batch.chunk_hook = crash
    graph = Pages([page(sources)])
    graph.download_hook = bad_signature
    with pytest.raises(Crash):
        run(batch, graph, config(batch_size=1))
    batch.chunk_hook = None
    ref = batch.control["job"]["copy_refs"][0]
    payload = batch.read_chunk(ref)
    mutation(payload["records"][0])
    replace_chunk(batch, ref, payload)
    graph2 = Pages([page(sources)])
    assert run(batch, graph2, config(batch_size=1), start_new=False)["status"] == "failed"
    assert graph2.downloads == [] and batch.public_manifest is None


def test_foreign_copy_metadata_never_adopted_or_skipped(batch):
    source = row("bad-a")
    batch.blobs[path(source)] = JPEG
    batch.metadata[path(source)] = hashlib.sha256(JPEG).hexdigest()
    graph = Pages([page([source])])
    graph.download_hook = bad_signature
    result = run(batch, graph)
    assert result["status"] == "failed" and result["reason"] == "foreign_copy_collision"
    assert result["omitted"] == 0 and graph.downloads == []
    assert batch.durable["managed"] == [] and batch.public_manifest is None


@pytest.mark.parametrize("boundary", ["primary_intent", "public_manifest", "primary_commit"])
def test_partial_transaction_recovered_before_more_downloads(batch, boundary):
    sources = [row(ident) for ident in "abcd"]
    cfg = config(batch_size=2)
    if boundary == "public_manifest":
        batch.fail_publish = True
    else:
        batch.fail_save_at = 2 if boundary == "primary_intent" else 3
    first = Pages([page(sources)])
    result = run(batch, first, cfg)
    assert result["status"] == "failed" and first.downloads == ["a", "b"]
    job = batch.control["job"]
    assert job["phase"] == "copy" and job["copy_offset"] == 2
    assert job["publication"] is not None and not job["publication"]["final"]
    batch.fail_publish = False
    batch.fail_save_at = None
    graph = Pages([page(sources)])
    def before_download(source):
        assert batch.durable["transaction"] is None
        if source["id"] == "c":
            assert {path(source) for source in sources[:2]} <= set(batch.public_manifest["images"])
    graph.download_hook = before_download
    assert run(batch, graph, cfg, start_new=False)["status"] == "ok"
    assert graph.downloads == ["c", "d"] and graph.scans == []
    assert set(batch.public_manifest["images"]) == {path(source) for source in sources}


@pytest.mark.parametrize("boundary", ["completion", "marker", "after_commit"])
def test_crashes_around_partial_publication_preserve_completed_prefix(batch, boundary):
    sources = [row(ident) for ident in "abcd"]
    fired = False
    def crash(control):
        nonlocal fired
        job = control["job"]
        if fired or not job or job["copy_offset"] != 2:
            return
        publication = job.get("publication")
        if ((boundary == "completion" and publication is not None) or
                (boundary == "marker" and publication is None and batch.publications) or
                (boundary == "after_commit" and publication is None and batch.publications)):
            fired = True
            raise Crash()
    if boundary == "marker":
        batch.publish_hook = lambda data: (_ for _ in ()).throw(Crash())
    else:
        batch.control_hook = crash
    with pytest.raises(Crash):
        run(batch, Pages([page(sources)]), config(batch_size=2))
    assert batch.control["job"]["copy_offset"] == 2
    batch.control_hook = batch.publish_hook = None
    graph = Pages([page(sources)])
    assert run(batch, graph, config(batch_size=2), start_new=False)["status"] == "ok"
    assert graph.downloads == ["c", "d"]
    assert len(batch.uploads) == 4
    if boundary == "after_commit":
        assert batch.publications == 2


@pytest.mark.parametrize("tamper", ["target", "marker", "cursor", "ledger"])
def test_partial_pending_transaction_validated_before_recovery_or_copies(batch, tamper):
    sources = [row(ident) for ident in "abcd"]
    batch.fail_publish = True
    assert run(batch, Pages([page(sources)]), config(batch_size=2))["status"] == "failed"
    batch.fail_publish = False
    if tamper == "target":
        batch.durable["transaction"]["libraries"]["spotify"]["items"].pop("a")
    elif tamper == "marker":
        batch.control["job"]["publication"]["sha256"] = "f" * 64
    elif tamper == "cursor":
        batch.control["job"]["publication"]["position"] = [0, 1]
    else:
        ref = batch.control["job"]["copy_refs"][0]
        batch.chunks[ref["name"]] = ("f" * 64, {})
    before = copy.deepcopy(batch.public_manifest)
    graph = Pages([page(sources)])
    assert run(batch, graph, config(batch_size=2), start_new=False)["status"] == "failed"
    assert graph.downloads == [] and batch.public_manifest == before
    assert batch.durable["transaction"] is not None


@pytest.mark.parametrize("change", ["owner", "config", "root"])
def test_pending_partial_never_recovered_under_changed_ownership(batch, change):
    sources = [row(ident) for ident in "abcd"]
    cfg = config(batch_size=2)
    batch.fail_publish = True
    assert run(batch, Pages([page(sources)]), cfg)["status"] == "failed"
    durable_control = copy.deepcopy(batch.control)
    batch.fail_publish = False
    graph = Pages([page(sources)])
    if change == "owner":
        batch.durable["owner"] = "other-owner"
    elif change == "config":
        cfg.libraries = [Library("spotify", "Other")]
    else:
        graph.root_value = {"id": "different", "drive_id": "drive"}
    assert run(batch, graph, cfg, start_new=False)["status"] == "aborted"
    assert graph.downloads == [] and batch.publications == 0
    assert batch.control == durable_control and batch.durable["transaction"] is not None


def test_source_race_aborts_and_keeps_already_published_safe_batch(batch):
    sources = [row(ident) for ident in "abcd"]
    graph = Pages([page(sources)])
    graph.download_hook = lambda source: (_ for _ in ()).throw(
        PublisherError("download_race_after")) if source["id"] == "c" else None
    result = run(batch, graph, config(batch_size=2))
    assert result["status"] == "aborted" and result["copied"] == 2
    assert set(batch.public_manifest["images"]) == {path(source) for source in sources[:2]}
    assert batch.control["job"] is None and batch.control["completed"]["ownership_refs"]
    graph2 = Pages([page(sources)])
    assert run(batch, graph2, config(batch_size=2))["status"] == "ok"
    assert graph2.downloads == ["c", "d"]


def test_skip_records_not_archived_as_upload_proof(batch):
    sources = [row("bad-a"), row("good"), row("race")]
    graph = Pages([page(sources)])
    def hook(source):
        bad_signature(source)
        if source["id"] == "race":
            raise PublisherError("download_item_outside_root")
    graph.download_hook = hook
    assert run(batch, graph, config(batch_size=2))["status"] == "aborted"
    proof = resumable._archived_proof(batch, batch.control, config().libraries[0], graph.root_value, lambda: None)
    assert path(sources[0]) not in proof and path(sources[1]) in proof
    batch.blobs[path(sources[0])] = JPEG
    batch.metadata[path(sources[0])] = hashlib.sha256(JPEG).hexdigest()
    next_graph = Pages([page(sources)])
    assert run(batch, next_graph, config(batch_size=2))["reason"] == "foreign_copy_collision"
    assert next_graph.downloads == []


def test_progressive_caps_batches_at_64_even_if_legacy_config_allows_100(batch):
    sources = [row(f"item-{index}") for index in range(65)]
    graph = Pages([page(sources)])
    def hook(source):
        if source["id"] == "item-64":
            assert batch.publications == 1 and len(batch.public_manifest["images"]) == 64
    graph.download_hook = hook
    assert run(batch, graph, config(batch_size=100))["status"] == "ok"
    assert batch.publications == 2
    assert all(len(payload["records"]) <= 64 for _, payload in batch.chunks.values() if "records" in payload)


def test_oversize_scan_label_allowed_but_candidate_bounds_fail_closed(batch):
    result = run(batch, Pages([page([row("good")], extra_ids=["oversize"], skipped={"oversize": 1})]))
    assert result["status"] == "ok" and result["reasons"] == {"oversize": 1}
    bad = row("bad")
    bad["size"] = resumable.MAX_BYTES + 1
    graph = Pages([page([bad])])
    before = copy.deepcopy(batch.public_manifest)
    assert run(batch, graph)["status"] == "aborted"
    assert graph.downloads == [] and batch.public_manifest == before


@pytest.mark.parametrize("size", [-1, True, "123", None])
def test_malformed_candidate_metadata_never_skipped(batch, size):
    source = row("bad")
    source["size"] = size
    graph = Pages([page([source])])
    result = run(batch, graph)
    assert result["status"] == "aborted" and result["omitted"] == 0
    assert graph.downloads == [] and batch.public_manifest is None


def test_flags_do_not_change_existing_fingerprint_or_reset_legacy_job():
    store = BatchMemory()
    clock = Clock()
    sources = [row(ident) for ident in "abcd"]
    assert engine_run(store, Pages([page(sources)], clock, download_cost=60), clock=clock)["status"] == "deferred"
    fingerprint = store.control["config_fingerprint"]
    progressive = ProgressiveMemory()
    progressive.__dict__.update(store.__dict__)
    progressive.published_prefixes = []
    progressive.publish_hook = None
    graph = Pages([page(sources)])
    assert run(progressive, graph, start_new=False)["status"] == "ok"
    assert graph.downloads == ["c", "d"]
    assert progressive.control["config_fingerprint"] == fingerprint
    assert len(progressive.uploads) == 4


def test_disabled_and_legacy_results_unchanged(batch):
    assert run(batch, cfg=config(enabled=False)) == {"status": "disabled"}
    assert batch.loads == batch.saves == batch.control_saves == 0
    legacy = BatchMemory()
    result = engine_run(legacy, Pages([page([row()])]), legacy_config())
    assert result == {"status": "ok", "images": 1, "entries": 1, "pages": 1, "pending_cleanup": 0}
    assert legacy.publications == 1 and legacy.saves == 3


def test_skip_disabled_retains_legacy_invalid_signature_abort():
    store = BatchMemory()
    graph = Pages([page([row("bad")])])
    graph.download_hook = bad_signature
    assert engine_run(store, graph, legacy_config(skip_invalid=False))["status"] == "aborted"
    assert store.public_manifest is None


def test_enabling_progressive_recovers_existing_legacy_final_transaction():
    store = BatchMemory()
    store.fail_publish = True
    sources = [row("photo")]
    assert engine_run(store, Pages([page(sources)]))["status"] == "failed"
    assert "publication" not in store.control["job"]
    progressive = ProgressiveMemory()
    progressive.__dict__.update(store.__dict__)
    progressive.published_prefixes = []
    progressive.publish_hook = None
    progressive.fail_publish = False
    graph = Pages([page(sources)])
    assert run(progressive, graph, start_new=False)["status"] == "ok"
    assert graph.downloads == graph.scans == []
    assert progressive.durable["transaction"] is None


def test_final_private_control_crash_does_not_republish_or_download(batch):
    fired = False
    def crash(control):
        nonlocal fired
        if not fired and control["job"] is None and batch.publications:
            fired = True
            raise Crash()
    batch.control_hook = crash
    with pytest.raises(Crash):
        run(batch, Pages([page([row("photo")])]))
    batch.control_hook = None
    graph = Pages([page([row("photo")])])
    assert run(batch, graph, start_new=False)["status"] == "unchanged"
    assert graph.downloads == graph.scans == [] and batch.publications == 1


def test_crash_after_public_manifest_recovers_before_next_batch(batch):
    sources = [row(ident) for ident in "abcd"]
    original_save = batch.save
    def crash_save(state):
        if batch.publications == 1 and state["transaction"] is None:
            raise Crash()
        original_save(state)
    batch.save = crash_save
    with pytest.raises(Crash):
        run(batch, Pages([page(sources)]), config(batch_size=2))
    assert batch.durable["transaction"] is not None
    batch.save = original_save
    graph = Pages([page(sources)])
    graph.download_hook = lambda source: pytest.fail("Copy before primary commit") if batch.durable["transaction"] else None
    assert run(batch, graph, config(batch_size=2), start_new=False)["status"] == "ok"
    assert graph.downloads == ["c", "d"] and len(batch.uploads) == 4


def test_exhausted_integral_budget_defers_publication_but_keeps_durable_prefix(batch):
    sources = [row(ident) for ident in "abcd"]
    clock = Clock()
    graph = Pages([page(sources)], clock, download_cost=60)
    assert run(batch, graph, clock=clock)["status"] == "deferred"
    assert batch.control["job"]["copy_offset"] == 2 and batch.public_manifest is None
    graph2 = Pages([page(sources)])
    def observe(source):
        assert {path(source) for source in sources[:2]} <= set(batch.public_manifest["images"])
    graph2.download_hook = observe
    assert run(batch, graph2, start_new=False)["status"] == "ok"
    assert graph2.downloads == ["c", "d"]


def test_boolean_publication_cursor_is_rejected_before_primary_load(batch):
    sources = [row(ident) for ident in "abcd"]
    batch.fail_publish = True
    assert run(batch, Pages([page(sources)]), config(batch_size=2))["status"] == "failed"
    batch.control["job"]["publication"]["position"][0] = False
    before = batch.loads
    assert run(batch, Pages([page(sources)]), start_new=False)["status"] == "failed"
    assert batch.loads == before


def test_pending_partial_is_recovered_even_when_flag_is_switched_off(batch):
    sources = [row(ident) for ident in "abcd"]
    batch.fail_publish = True
    assert run(batch, Pages([page(sources)]), config(batch_size=2))["status"] == "failed"
    batch.fail_publish = False
    # Progressive fixture is required only for its already-durable partial tx;
    # subsequent disabled-mode final publication follows the legacy phase rule.
    original_publish = batch.publish
    def publish(data):
        if batch.control["job"].get("publication") is None:
            BatchMemory.publish(batch, data)
        else:
            original_publish(data)
    batch.publish = publish
    graph = Pages([page(sources)])
    result = engine_run(batch, graph, legacy_config(), start_new=False)
    assert result["status"] == "ok" and "phase" not in result
    assert graph.downloads == ["c", "d"] and batch.durable["transaction"] is None


def test_corrupt_previous_invalid_reason_cannot_silently_reject_new_sources(batch):
    graph = Pages([page([row("bad-a"), row("good")])])
    graph.download_hook = bad_signature
    assert run(batch, graph)["status"] == "ok"
    snapshot = batch.read_chunk(batch.control["completed"]["invalid_ref"])
    copy_ref = snapshot["copy_refs"][0]
    payload = batch.read_chunk(copy_ref)
    payload["records"][0]["skip"] = "http_403"
    replace_chunk(batch, copy_ref, payload)
    replace_chunk(batch, batch.control["completed"]["invalid_ref"], snapshot)
    before = copy.deepcopy(batch.public_manifest)
    graph2 = Pages([page([row("bad-a"), row("good")])])
    assert run(batch, graph2)["status"] == "failed"
    assert graph2.downloads == [] and batch.public_manifest == before


def test_copy_completion_control_failure_never_exposes_uncheckpointed_originals(batch):
    sources = [row(ident) for ident in "abcd"]
    def fail(control):
        if control["job"] and control["job"]["copy_offset"]:
            raise PublisherError("simulated_durability_error")
    batch.control_hook = fail
    graph = Pages([page(sources)])
    assert run(batch, graph, config(batch_size=2))["status"] == "failed"
    assert batch.public_manifest is None and batch.control["job"]["copy_refs"] == []
    assert len(batch.uploads) == 2
    batch.control_hook = None
    assert run(batch, Pages([page(sources)]), config(batch_size=2), start_new=False)["status"] == "ok"
    assert len(batch.uploads) == 4


def test_existing_legacy_100_row_intent_is_split_into_progressive_completions():
    store = BatchMemory()
    sources = [row(f"item-{index}") for index in range(100)]
    graph = Pages([page(sources)])
    graph.download_hook = lambda source: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        engine_run(store, graph, legacy_config(batch_size=100))
    assert len(store.read_chunk(store.control["job"]["intent_ref"])["records"]) == 100
    progressive = ProgressiveMemory()
    progressive.__dict__.update(store.__dict__)
    progressive.published_prefixes = []
    progressive.publish_hook = None
    graph2 = Pages([page(sources)])
    def observe(source):
        if source["id"] == "item-64":
            assert len(progressive.public_manifest["images"]) == 64
            assert progressive.control["job"]["intent_ref"] is not None
    graph2.download_hook = observe
    assert run(progressive, graph2, config(batch_size=100), start_new=False)["status"] == "ok"
    copies = [payload for name, (_, payload) in progressive.chunks.items() if "/copy/" in name]
    assert [len(payload["records"]) for payload in copies] == [64, 36]
    assert progressive.publications == 2 and graph2.scans == []
