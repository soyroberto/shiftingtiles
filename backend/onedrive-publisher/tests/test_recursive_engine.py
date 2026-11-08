"""Offline recursion integration and exact-scope False->True migration."""
import copy
import hashlib
import json

import pytest

import resumable
from config import Library, PublisherError
from paged_graph import _tree_json
from test_progressive import ProgressiveMemory
from test_recursive_graph import ROOT, TreeGraph, folder, image, link, nested_pages
from test_resumable import (BatchMemory, Clock, Crash, Pages, auth_offline,
                            config, page, path, row, run)
from test_sync import JPEG, seed


class RecursiveMemory(ProgressiveMemory):
    def publish(self, data):
        if self.control["job"].get("publication") is not None:
            super().publish(data)
        else:
            BatchMemory.publish(self, data)


@pytest.fixture
def batch():
    return RecursiveMemory()


def recursive_config(**changes):
    return config(libraries=[Library("spotify", "Musica/Spotify", recursive=True)], **changes)


def recursive_row(ident="photo", parent="root", **kwargs):
    return dict(row(ident, **kwargs), parent_id=parent)


def tree_cursor(index):
    return _tree_json({"v": 1, "root": ROOT, "folders": [["root", None]],
                       "current": "root", "next": link(index=index), "pending": [],
                       "seen": [hashlib.sha256(link(index=index - 1).encode()).hexdigest()],
                       "pages": index})


def paged_rows(rows, recursive=False):
    pages = []
    for start in range(0, len(rows), 200):
        index = len(pages) + 1
        cursor = (tree_cursor(index) if recursive else link(index=index)) if start + 200 < len(rows) else None
        pages.append(page(rows[start:start + 200], cursor))
    return pages or [page([])]


class RecursivePages(Pages):
    """Engine-only large fixtures; the Graph proof itself has separate real tests."""
    def scan_page(self, root, next_cursor):
        self.scans.append(next_cursor)
        if self.scan_hook:
            self.scan_hook(next_cursor)
        if self.clock:
            self.clock.value += self.scan_cost
        index = 0 if next_cursor is None else json.loads(next_cursor[5:])["pages"]
        result = self.pages[index]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def download(self, source, ext):
        assert self.expected_parent == source["parent_id"]
        return super().download(source, ext)


def seed_six(batch):
    items, paths = {}, []
    for index in range(6):
        paths.append(seed(batch, ident=f"initial-{index}", tag="fixed"))
        items.update(batch.durable["libraries"]["spotify"]["items"])
    batch.durable["libraries"]["spotify"]["items"] = items
    return paths


def test_real_nested_graph_persists_parent_rows_and_finishes_scan_before_copies(batch):
    graph = TreeGraph(nested_pages())
    graph.download_hook = lambda: (
        pytest.fail("Copy before complete durable scan")
        if batch.control["job"]["phase"] == "scan" or batch.control["job"]["next_cursor"] is not None else None)
    result = run(batch, graph, recursive_config())
    assert result["status"] == "ok" and result["pages"] == 6 and result["images"] == 5
    scan_rows = [source for _, payload in batch.chunks.values() if "rows" in payload for source in payload["rows"]]
    assert {source["parent_id"] for source in scan_rows} == {"root", "a", "c"}
    assert batch.durable["libraries"]["spotify"]["recursive"] is True
    assert graph.download_session.get.call_count == 5 and batch.listings == 1 and batch.heads == 0
    assert all(content == JPEG for content in batch.blobs.values())
    text = json.dumps([batch.control, batch.chunks, batch.durable])
    assert all(secret not in text for secret in ("private original", "private folder", "downloadUrl", "secret=", "ephemeral-token"))


def test_recursive_scan_deadline_restores_tree_frontier_with_new_client(batch):
    clock = Clock()
    graph = TreeGraph(nested_pages(), clock=clock, scan_cost=171)
    assert run(batch, graph, recursive_config(), clock=clock)["status"] == "deferred"
    job = batch.control["job"]
    saved_cursor = job["next_cursor"]
    assert saved_cursor.startswith("tree:") and job["phase"] == "scan" and job["pagecount"] == 1
    assert batch.blobs == {} and batch.public_manifest is None
    graph = TreeGraph(nested_pages())
    assert run(batch, graph, recursive_config(), start_new=False)["status"] == "ok"
    assert graph.scans[0] == saved_cursor and None not in graph.scans
    assert graph.download_session.get.call_count == 5


@pytest.mark.parametrize("parent", [None, "", "a/b", "a%2Fb", True, "x" * 513])
def test_recursive_parent_ids_rejected_before_any_copy(batch, parent):
    graph = RecursivePages([page([dict(row(), parent_id=parent)])])
    assert run(batch, graph, recursive_config())["status"] == "aborted"
    assert graph.downloads == batch.uploads == [] and batch.public_manifest is None


@pytest.mark.parametrize("recursive,source", [(True, row()), (False, recursive_row())])
def test_row_parent_schema_cannot_cross_recursive_and_legacy_modes(batch, recursive, source):
    cfg = recursive_config() if recursive else config()
    assert run(batch, RecursivePages([page([source])]), cfg)["status"] == "aborted"
    assert batch.uploads == []


def test_batch_payload_rechecks_parent_id_in_durable_row():
    source = recursive_row(parent="unsafe/path")
    record = resumable._record(source, "spotify")
    with pytest.raises(PublisherError, match="invalid_graph_id"):
        resumable._batch_payload({"start": [0, 0], "end": [1, 0], "records": [record]},
                                 [page([source])], "spotify")


def test_global_duplicates_across_recursive_folders_abort_entire_scan(batch):
    pages = {"root": [{"value": [image("same"), folder("a")]}],
             "a": [{"value": [image("same", "a")]}]}
    graph = TreeGraph(pages)
    assert run(batch, graph, recursive_config())["status"] == "aborted"
    assert graph.download_session.get.call_count == 0 and batch.public_manifest is None


def test_moved_descendant_during_resumed_scan_preserves_last_good_and_never_skips(batch):
    old = seed(batch)
    clock = Clock()
    pages = {"root": [{"value": [folder("a")]}], "a": [{"value": [image("nested", "a")]}]}
    assert run(batch, TreeGraph(pages, clock=clock, scan_cost=171), recursive_config(skip_invalid=True), clock=clock)["status"] == "deferred"
    graph = TreeGraph(pages)
    graph.metadata["a"]["parentReference"]["id"] = "outer"
    assert run(batch, graph, recursive_config(skip_invalid=True), start_new=False)["status"] == "aborted"
    assert batch.public_manifest["images"] == [old] and batch.uploads == []


def test_progressive_recursive_partial_is_verified_prefix_and_failure_recovers(batch):
    old = seed(batch)
    sources = [recursive_row("a", parent="nested"), recursive_row("b", parent="nested"), recursive_row("c", parent="nested")]
    cfg = recursive_config(progressive=True)
    clock = Clock()
    graph = RecursivePages([page(sources)], clock=clock, download_cost=81)
    result = run(batch, graph, cfg, clock=clock)
    assert result["status"] == "deferred" and result["copied"] == 1
    assert set(batch.public_manifest["images"]) == {old, path(sources[0])}
    assert batch.control["job"]["next_cursor"] is None and batch.control["job"]["copy_offset"] == 1
    batch.fail_publish = True
    graph = RecursivePages([page(sources)])
    assert run(batch, graph, cfg, start_new=False)["status"] == "failed"
    assert batch.durable["transaction"] is not None
    batch.fail_publish = False
    graph = RecursivePages([page(sources)])
    assert run(batch, graph, cfg, start_new=False)["status"] == "ok"
    assert graph.scans == graph.downloads == []
    assert set(batch.public_manifest["images"]) == {path(source) for source in sources}
    assert old in batch.blobs and batch.heads == 0


def test_recursive_download_ancestry_race_is_fatal_not_cached_invalid(batch):
    old = seed(batch)
    graph = TreeGraph({"root": [{"value": [folder("a")]}], "a": [{"value": [image("nested", "a")]}]})
    graph.download_hook = lambda: graph.metadata["a"]["parentReference"].update(id="outer")
    assert run(batch, graph, recursive_config(progressive=True, skip_invalid=True))["status"] == "aborted"
    assert batch.public_manifest["images"] == [old] and batch.uploads == []
    assert batch.control["job"] is None
    assert batch.control["completed"].get("invalid_ref") is None


def test_controlled_widening_preserves_10037_verified_six_initials_97_skips_and_50_unpublished(batch):
    initial = seed_six(batch)
    cfg = config(retain_initial=True, skip_invalid=True)
    verified = [row(f"verified-{index}") for index in range(10037)]
    invalid = [row(f"invalid-{index}") for index in range(97)]
    graph = Pages(paged_rows(verified + invalid))
    graph.download_hook = lambda source: (
        (_ for _ in ()).throw(PublisherError("invalid_raster_signature"))
        if source["id"].startswith("invalid-") else None)
    assert run(batch, graph, cfg)["status"] == "ok"
    retained = copy.deepcopy(batch.control["retained"])
    invalid_ref = copy.deepcopy(batch.control["completed"]["invalid_ref"])
    metrics = copy.deepcopy(batch.control["completed"])
    unpublished = [row(f"unpublished-{index}") for index in range(50)]
    graph = Pages(paged_rows(verified + invalid + unpublished + [row("failure")]))
    graph.download_hook = lambda source: (
        (_ for _ in ()).throw(RuntimeError("ephemeral failure")) if source["id"] == "failure" else None)
    assert run(batch, graph, cfg)["status"] == "failed"
    assert graph.downloads == [source["id"] for source in unpublished] + ["failure"]
    old_job = copy.deepcopy(batch.control["job"])
    old_fingerprint = batch.control["config_fingerprint"]
    old_chunk_names = set(batch.chunks)
    uploaded = len(batch.uploads)
    recursive_sources = [dict(source, parent_id="root") for source in verified + invalid + unpublished]
    recursive_sources.append(recursive_row("new-descendant", parent="owned-child"))
    cfg = recursive_config(retain_initial=True, skip_invalid=True, progressive=True)
    clock = Clock()
    graph = RecursivePages(paged_rows(recursive_sources, recursive=True), clock=clock, scan_cost=171)
    assert run(batch, graph, cfg, clock=clock)["status"] == "deferred"
    assert graph.scans == [None] and graph.downloads == []
    assert batch.control["retained"] == retained and batch.control["completed"]["invalid_ref"] == invalid_ref
    assert batch.control["completed"]["runs"] == metrics["runs"]
    assert batch.control["completed"]["images"] == metrics["images"]
    assert batch.control["recursive_upgrade"] == {"from": old_fingerprint, "root": ROOT}
    assert batch.control["config_fingerprint"] == resumable._fingerprint(cfg, cfg.libraries[0], "owner")
    archive = batch.read_chunk(batch.control["completed"]["ownership_refs"][-1])
    assert archive["job_id"] == old_job["id"] and archive["copy_refs"] == old_job["copy_refs"]
    assert archive["intent_ref"] == old_job["intent_ref"] and archive["config_fingerprint"] == old_fingerprint
    assert old_chunk_names <= set(batch.chunks)
    graph = RecursivePages(paged_rows(recursive_sources, recursive=True))
    result = run(batch, graph, cfg, start_new=False)
    assert result["status"] == "ok" and graph.downloads == ["new-descendant"]
    assert len(batch.uploads) == uploaded + 1
    assert set(batch.public_manifest["images"]) == set(initial + [path(source) for source in verified + unpublished] + [path(recursive_sources[-1])])
    assert result["reasons"]["invalid_raster_signature"] == len(invalid)
    assert batch.control["retained"] == retained and old_chunk_names <= set(batch.chunks)
    assert batch.heads == 0 and all(content == JPEG for content in batch.blobs.values())


@pytest.mark.parametrize("field", ["path", "library", "owner", "client", "account", "container", "retain", "root"])
def test_widening_with_any_other_scope_change_fails_closed_without_adoption(batch, field):
    clock = Clock()
    assert run(batch, Pages([page([row("a"), row("b")])], clock, download_cost=90), clock=clock)["status"] == "deferred"
    old_fingerprint = batch.control["config_fingerprint"]
    cfg = recursive_config()
    graph = RecursivePages([page([recursive_row("a"), recursive_row("b")])])
    if field == "path":
        cfg.libraries = [Library("spotify", "Changed", recursive=True)]
    elif field == "library":
        cfg.libraries = [Library("other", "Musica/Spotify", recursive=True)]
    elif field == "owner":
        batch.durable["owner"] = "other-owner"
    elif field == "client":
        cfg.client_id = "other-client"
    elif field == "account":
        cfg.account_url = "https://foreign.invalid"
    elif field == "container":
        cfg.state_container = "other-private"
    elif field == "retain":
        cfg.retain_initial = True
    else:
        graph.root_value = {"id": "other-root", "drive_id": "drive"}
    result = run(batch, graph, cfg, start_new=False)
    assert result["status"] == "aborted"
    assert batch.control["config_fingerprint"] == old_fingerprint
    assert "recursive_upgrade" not in batch.control
    assert graph.downloads == [] and len(batch.uploads) == 1 and batch.public_manifest is None


def test_pending_legacy_publication_blocks_widening_and_preserves_transaction(batch):
    batch.fail_publish = True
    cfg = config(progressive=True)
    assert run(batch, Pages([page([row()])]), cfg)["status"] == "failed"
    before = copy.deepcopy(batch.control), copy.deepcopy(batch.durable["transaction"])
    graph = RecursivePages([page([recursive_row()])])
    assert run(batch, graph, recursive_config(progressive=True), start_new=False)["status"] == "aborted"
    assert (batch.control, batch.durable["transaction"]) == before
    assert graph.scans == graph.downloads == []


def test_widening_archive_checkpoint_crash_keeps_old_proof_and_retries_safely(batch):
    clock = Clock()
    assert run(batch, Pages([page([row("a"), row("b")])], clock, download_cost=90), clock=clock)["status"] == "deferred"
    before = copy.deepcopy(batch.control)
    batch.control_hook = lambda control: (
        (_ for _ in ()).throw(Crash()) if "recursive_upgrade" in control else None)
    with pytest.raises(Crash):
        run(batch, RecursivePages([page([recursive_row("a"), recursive_row("b")])]), recursive_config(), start_new=False)
    assert batch.control == before and len(batch.uploads) == 1
    batch.control_hook = None
    graph = RecursivePages([page([recursive_row("a"), recursive_row("b")])])
    assert run(batch, graph, recursive_config(), start_new=False)["status"] == "ok"
    assert graph.scans == [None] and graph.downloads == ["b"]


def test_completed_same_root_widening_reuses_published_copy_and_initial_ref(batch):
    initial = seed_six(batch)
    assert run(batch, Pages([page([row()])]), config(retain_initial=True))["status"] == "ok"
    retained = copy.deepcopy(batch.control["retained"])
    graph = RecursivePages([page([recursive_row()])])
    assert run(batch, graph, recursive_config(retain_initial=True))["status"] == "unchanged"
    assert graph.scans == [None] and graph.downloads == []
    assert batch.control["retained"] == retained
    assert set(batch.public_manifest["images"]) == set(initial + [path(row())])


def test_completed_legacy_control_without_any_root_proof_is_not_guessed(batch):
    assert run(batch, Pages([page([row()])]))["status"] == "ok"
    del batch.control["completed"]["root"]
    before = copy.deepcopy(batch.control)
    graph = RecursivePages([page([recursive_row()])])
    assert run(batch, graph, recursive_config())["status"] == "aborted"
    assert batch.control == before and graph.downloads == graph.scans == []


def test_corrupt_upgrade_fingerprint_cannot_authorize_archived_adoption(batch):
    assert run(batch, Pages([page([row()])]))["status"] == "ok"
    assert run(batch, RecursivePages([page([recursive_row()])]), recursive_config())["status"] == "unchanged"
    batch.control["recursive_upgrade"]["from"] = "f" * 64
    graph = RecursivePages([page([recursive_row()])])
    assert run(batch, graph, recursive_config())["status"] == "failed"
    assert graph.scans == graph.downloads == []


def test_false_after_true_is_not_an_automatic_downgrade(batch):
    assert run(batch, RecursivePages([page([recursive_row()])]), recursive_config())["status"] == "ok"
    fingerprint = batch.control["config_fingerprint"]
    graph = Pages([page([row()])])
    assert run(batch, graph, config())["status"] == "aborted"
    assert batch.control["config_fingerprint"] == fingerprint and graph.scans == graph.downloads == []


@pytest.mark.parametrize("cursor", [1, {}, []])
def test_non_string_recursive_page_cursor_aborts_without_copy(batch, cursor):
    graph = RecursivePages([page([recursive_row()], cursor)])
    assert run(batch, graph, recursive_config())["status"] == "aborted"
    assert graph.downloads == batch.uploads == [] and batch.public_manifest is None
