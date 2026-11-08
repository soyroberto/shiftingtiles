"""Single-root publisher: complete durable scan, then verified batch publication.

The caller holds BatchStore.locked(). Primary v1 state is saved only by auth and
legacy publication transactions; there are no automatic deletes. All source names/signed URLs
are ephemeral. Chunk slots are random create-only addresses, not progress indexes:
a put-before-checkpoint crash must never alias the next (possibly changed) page.
"""
import copy
from dataclasses import replace
import hashlib
import json
import math
import re
import secrets
import time
import uuid

from auth import silent_token
from config import MANAGED_PATH, TYPES, PublisherError, content_version, manifest, photo_path
from graph import MAX_BYTES
from paged_graph import PagedGraph
from sync import commit_publication, recover_publication

_HEX = re.compile(r"[a-f0-9]{64}\Z")
_JOB = re.compile(r"[a-f0-9]{32}\Z")
_REF = re.compile(r"jobs/([a-f0-9]{32})/(scan|copy|intent|snapshot)/[0-9]{6}\.json\Z")
_CONTROL = {"version", "config_fingerprint", "owner", "retained", "job", "completed"}
_JOB_FIELDS = {"id", "root", "phase", "scan_refs", "next_cursor", "entrycount",
               "pagecount", "candidatecount", "copy_page", "copy_offset", "copy_refs",
               "intent_ref", "retry_at", "skipped"}
_SKIP = {"remote", "folder", "nonfile", "heic", "heif", "tif", "tiff", "svg", "other", "mime", "oversize"}
_COPY_SKIP = {"invalid_raster_signature"}
_FATAL = {"duplicate_item_during_scan", "pagination_bound", "item_bound", "page_bound",
          "incomplete_page", "unsafe_graph_cursor", "invalid_nextlink", "invalid_graph_item",
          "invalid_graph_root", "invalid_graph_id", "source_not_owned_folder",
          "download_race_before", "download_race_after", "download_item_changed",
          "download_item_outside_root", "invalid_raster_signature", "file_size_bound",
          "missing_item_version", "invalid_item_version", "source_ancestry_changed", "folder_bound"}
_DEFER = {"engine_deadline", "scan_deadline", "retry_budget"}
# Reserve bounded writes/checkpoints inside the integral 180s invocation budget.
_CHECKPOINT_RESERVE = 10
_MAX_ARCHIVES = 128


def _require(condition, code="invalid_work_control"):
    if not condition:
        raise PublisherError(code)


def _integer(value, upper=100000, code="invalid_work_control"):
    _require(type(value) is int and 0 <= value <= upper, code)


def _ref(ref, job=None, kind=None):
    _require(isinstance(ref, dict) and set(ref) == {"name", "sha256"})
    name, digest = ref["name"], ref["sha256"]
    _require(isinstance(name, str) and isinstance(digest, str) and _HEX.fullmatch(digest))
    match = _REF.fullmatch(name)
    _require(match and (job is None or match[1] == job) and
             (kind is None or match[2] in (kind, "snapshot")))
    return match[1], match[2]


def _skipped(value, code="invalid_work_control"):
    _require(isinstance(value, dict) and not set(value) - _SKIP, code)
    for count in value.values():
        _integer(count, code=code)


def validate_control(control):
    """Strict schema, bounded catalogs and root/cursor validation before Graph IO."""
    _require(isinstance(control, dict) and set(control) in (_CONTROL, _CONTROL | {"recursive_upgrade"}))
    _require(type(control["version"]) is int and control["version"] == 1)
    _require(isinstance(control["config_fingerprint"], str) and
             _HEX.fullmatch(control["config_fingerprint"]))
    _require(isinstance(control["owner"], str) and 0 < len(control["owner"]) <= 1024)
    if "recursive_upgrade" in control:
        upgrade = control["recursive_upgrade"]
        _require(isinstance(upgrade, dict) and set(upgrade) == {"from", "root"} and
                 isinstance(upgrade["from"], str) and _HEX.fullmatch(upgrade["from"]))
        PagedGraph._checked_root(upgrade["root"])
    if control["retained"] is not None:
        _ref(control["retained"], kind="snapshot")
    metrics = control["completed"]
    fields = {"runs", "images", "ownership_refs"}
    _require(isinstance(metrics, dict) and fields <= set(metrics) <= fields | {"invalid_ref", "root"})
    if "root" in metrics:
        PagedGraph._checked_root(metrics["root"])
    if metrics.get("invalid_ref") is not None:
        _ref(metrics["invalid_ref"], kind="snapshot")
    _integer(metrics["runs"], 2 ** 53)
    _integer(metrics["images"], 100006)
    _require(isinstance(metrics["ownership_refs"], list) and len(metrics["ownership_refs"]) <= _MAX_ARCHIVES)
    for ref in metrics["ownership_refs"]:
        _ref(ref, kind="snapshot")
    job = control["job"]
    if job is None:
        return control
    _require(isinstance(job, dict) and set(job) in (_JOB_FIELDS, _JOB_FIELDS | {"publication"}))
    _require(isinstance(job["id"], str) and _JOB.fullmatch(job["id"]))
    PagedGraph._checked_root(job["root"])
    _require(job["phase"] in ("scan", "copy", "publish"))
    for field in ("entrycount", "candidatecount", "copy_offset"):
        _integer(job[field])
    for field in ("pagecount", "copy_page"):
        _integer(job[field], 500)
    _require(job["candidatecount"] <= job["entrycount"] and job["copy_page"] <= job["pagecount"])
    for field, kind in (("scan_refs", "scan"), ("copy_refs", "copy")):
        _require(isinstance(job[field], list) and len(job[field]) <= 128)
        for ref in job[field]:
            _ref(ref, job["id"], kind)
    if job["intent_ref"] is not None:
        _ref(job["intent_ref"], job["id"], "intent")
        _require(_ref(job["intent_ref"])[1] == "intent")
    retry = job["retry_at"]
    _require(retry is None or (type(retry) in (int, float) and math.isfinite(retry) and retry >= 0))
    if job["next_cursor"] is not None:
        PagedGraph._cursor_checked(job["root"], job["next_cursor"])
    if job["phase"] != "scan":
        _require(job["next_cursor"] is None and job["pagecount"] > 0)
    else:
        _require(not job["copy_refs"] and job["intent_ref"] is None and
                 job["copy_page"] == job["copy_offset"] == 0)
        _require(job["pagecount"] == 0 or job["next_cursor"] is not None)
    publication = job.get("publication")
    if publication is not None:
        _require(isinstance(publication, dict) and set(publication) == {"position", "final", "sha256"})
        _require(job["phase"] != "scan" and type(publication["final"]) is bool and
                 isinstance(publication["position"], list) and
                 len(publication["position"]) == 2 and
                 publication["position"] == [job["copy_page"], job["copy_offset"]] and
                 isinstance(publication["sha256"], str) and _HEX.fullmatch(publication["sha256"]))
        _integer(publication["position"][0], 500)
        _integer(publication["position"][1])
        _require(publication["final"] == (job["phase"] == "publish"))
    _skipped(job["skipped"])
    return control


def _save(store, control):
    store.save_control(validate_control(control))


def _put(store, job_id, kind, payload):
    # Retry the rare random collision, including orphan slots from a previous crash.
    for _ in range(16):
        try:
            return store.put_chunk(job_id, kind, secrets.randbelow(1000000), payload)
        except PublisherError as exc:
            if str(exc) != "private_chunk_collision":
                raise
    raise PublisherError("private_chunk_collision")


def _append(store, job, kind, ref):
    field = kind + "_refs"
    job[field].append(ref)
    if len(job[field]) < 128:
        return
    # Radix-32 immutable catalog. Merge the trailing full group, then carry a
    # full group at that level. Even batch_size=1 / 100000 images stays shallow
    # and bounded; a chain of 128-way checkpoints would grow linearly instead.
    entries = []
    for existing in job[field]:
        if _ref(existing, job["id"], kind)[1] == "snapshot":
            node = store.read_chunk(existing)
            _require(isinstance(node, dict) and set(node) == {"kind", "refs", "level"} and
                     node["kind"] == kind, "invalid_private_ledger")
            _integer(node["level"], 8, "invalid_private_ledger")
            _require(node["level"] > 0, "invalid_private_ledger")
            entries.append((existing, node["level"]))
        else:
            entries.append((existing, 0))
    for level in range(8):
        indexes = [index for index, (_, value) in enumerate(entries) if value == level]
        while len(indexes) >= 32:
            start = indexes[0]
            _require(indexes[:32] == list(range(start, start + 32)), "invalid_private_ledger")
            node = _put(store, job["id"], "snapshot", {
                "kind": kind, "refs": [value[0] for value in entries[start:start + 32]],
                "level": level + 1})
            entries[start:start + 32] = [(node, level + 1)]
            indexes = [index for index, (_, value) in enumerate(entries) if value == level]
    job[field] = [value[0] for value in entries]
    _require(len(job[field]) < 128, "invalid_private_ledger")


def _expand(store, refs, kind, job_id, check, depth=0, visited=None):
    _require(depth <= 32, "invalid_private_ledger")
    if visited is None:
        visited = set()
    for ref in refs:
        check()
        _, actual = _ref(ref, job_id, kind)
        _require(ref["name"] not in visited, "invalid_private_ledger")
        visited.add(ref["name"])
        payload = store.read_chunk(ref)
        if actual == "snapshot":
            _require(isinstance(payload, dict) and set(payload) == {"kind", "refs", "level"} and
                     payload["kind"] == kind and isinstance(payload["refs"], list) and
                     len(payload["refs"]) == 32 and type(payload["level"]) is int and
                     1 <= payload["level"] <= 8, "invalid_private_ledger")
            yield from _expand(store, payload["refs"], kind, job_id, check, depth + 1, visited)
        else:
            yield payload


def _page(page, root, recursive=None):
    _require(isinstance(page, dict) and set(page) == {"rows", "ids", "next", "entries", "skipped"},
             "incomplete_page")
    _require(isinstance(page["rows"], list) and isinstance(page["ids"], list), "incomplete_page")
    _integer(page["entries"], 200, "page_bound")
    _require(len(page["ids"]) == page["entries"] and len(page["rows"]) <= page["entries"], "incomplete_page")
    ids = set()
    for ident in page["ids"]:
        PagedGraph._identifier(ident)
        _require(ident not in ids, "duplicate_item_during_scan")
        ids.add(ident)
    rows = set()
    for row in page["rows"]:
        fields = {"id", "size", "cTag", "eTag", "file", "ext"}
        _require(isinstance(row, dict) and set(row) in (fields, fields | {"parent_id"}),
                 "invalid_graph_item")
        _require(recursive is None or ("parent_id" in row) == recursive, "invalid_graph_item")
        if "parent_id" in row:
            PagedGraph._identifier(row["parent_id"])
        _require(row["id"] in ids and row["id"] not in rows, "duplicate_item_during_scan")
        rows.add(row["id"])
        _require(row["ext"] in TYPES and isinstance(row["file"], dict) and
                 set(row["file"]) == {"mimeType"} and
                 row["file"]["mimeType"] in (TYPES[row["ext"]], "application/octet-stream"),
                 "invalid_graph_item")
        _integer(row["size"], MAX_BYTES, "file_size_bound")
        _require(all(tag is None or isinstance(tag, str) and len(tag) <= 4096
                     for tag in (row["cTag"], row["eTag"])), "invalid_item_version")
        content_version(row)
    if page["next"] is not None:
        _require(isinstance(page["next"], str), "unsafe_graph_cursor")
        _require(recursive is None or page["next"].startswith("tree:") == recursive, "unsafe_graph_cursor")
        PagedGraph._cursor_checked(root, page["next"])
    _skipped(page["skipped"], "invalid_graph_item")
    _require(sum(page["skipped"].values()) + len(rows) == len(ids), "incomplete_page")
    return page


def _load_pages(store, job, check, recursive=None):
    pages, ids, cursors, skipped = [], set(), set(), {}
    for page in _expand(store, job["scan_refs"], "scan", job["id"], check):
        _page(page, job["root"], recursive)
        _require(not ids.intersection(page["ids"]), "duplicate_item_during_scan")
        ids.update(page["ids"])
        if pages:
            _require(pages[-1]["next"] is not None, "invalid_private_ledger")
        if page["next"] is not None:
            _require(page["next"] not in cursors, "pagination_bound")
            cursors.add(page["next"])
        pages.append(page)
        for label, count in page["skipped"].items():
            skipped[label] = skipped.get(label, 0) + count
        _require(len(pages) <= 500 and len(ids) <= 100000, "invalid_private_ledger")
    _require(len(pages) == job["pagecount"] and len(ids) == job["entrycount"] and
             sum(len(p["rows"]) for p in pages) == job["candidatecount"] and skipped == job["skipped"] and
             (pages[-1]["next"] if pages else None) == job["next_cursor"], "invalid_private_ledger")
    return pages, ids, cursors


def _position(pages, position):
    p, offset = position
    _integer(p, len(pages), "invalid_private_ledger")
    if p == len(pages):
        _require(offset == 0, "invalid_private_ledger")
    else:
        _integer(offset, len(pages[p]["rows"]), "invalid_private_ledger")
    while p < len(pages) and offset == len(pages[p]["rows"]):
        p, offset = p + 1, 0
    return [p, offset]


def _slice(pages, start, count):
    p, offset = _position(pages, start)
    result = []
    while p < len(pages) and len(result) < count:
        result.append(pages[p]["rows"][offset])
        p, offset = _position(pages, [p, offset + 1])
    return result, [p, offset]


def _record(row, library_id, digest=None):
    version = content_version(row)
    return {"id": row["id"], "version": version,
            "path": photo_path(library_id, row["id"], version, row["ext"]),
            "bytes": row["size"], "sha256": digest}


def _records(records, library_id):
    _require(isinstance(records, list) and 1 <= len(records) <= 100, "invalid_private_ledger")
    for rec in records:
        fields = {"id", "version", "path", "bytes", "sha256"}
        _require(isinstance(rec, dict) and set(rec) in (fields, fields | {"skip"}),
                 "invalid_private_ledger")
        PagedGraph._identifier(rec["id"])
        _require(isinstance(rec["version"], str) and len(rec["version"]) <= 16384 and
                 isinstance(rec["path"], str) and MANAGED_PATH.fullmatch(rec["path"]), "invalid_private_ledger")
        ext = rec["path"].rsplit(".", 1)[1]
        _require(rec["path"] == photo_path(library_id, rec["id"], rec["version"], ext), "invalid_private_ledger")
        _integer(rec["bytes"], MAX_BYTES, "invalid_private_ledger")
        _require(rec["sha256"] is None or isinstance(rec["sha256"], str) and _HEX.fullmatch(rec["sha256"]),
                 "invalid_private_ledger")
        if "skip" in rec:
            _require(isinstance(rec["skip"], str) and rec["skip"] in _COPY_SKIP and
                     rec["sha256"] is None, "invalid_private_ledger")
    return records


def _batch_payload(payload, pages, library_id, allow_skips=False):
    _require(isinstance(payload, dict) and set(payload) == {"start", "end", "records"}, "invalid_private_ledger")
    for field in ("start", "end"):
        _require(isinstance(payload[field], list) and len(payload[field]) == 2, "invalid_private_ledger")
        _require(_position(pages, payload[field]) == payload[field], "invalid_private_ledger")
    records = _records(payload["records"], library_id)
    rows, end = _slice(pages, payload["start"], len(records))
    _require(end == payload["end"] and len(rows) == len(records), "invalid_private_ledger")
    for row, rec in zip(rows, records):
        if "parent_id" in row:
            PagedGraph._identifier(row["parent_id"])
        expected = _record(row, library_id, rec["sha256"])
        if "skip" in rec:
            _require(allow_skips, "invalid_private_ledger")
            expected["skip"] = rec["skip"]
        _require(expected == rec, "invalid_private_ledger")
    return rows


def _completions(store, job, pages, lib_id, check):
    position, records = _position(pages, [0, 0]), []
    for payload in _expand(store, job["copy_refs"], "copy", job["id"], check):
        _batch_payload(payload, pages, lib_id, allow_skips=True)
        _require(payload["start"] == position, "invalid_private_ledger")
        position = payload["end"]
        records.extend(payload["records"])
    _require(position == _position(pages, [job["copy_page"], job["copy_offset"]]), "invalid_private_ledger")
    return records


def _fingerprint(config, lib, owner):
    value = [lib.id, lib.path, lib.recursive, owner, config.client_id, config.account_url,
             config.state_container, bool(getattr(config, "retain_initial", False))]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _limits(config):
    batch = getattr(config, "batch_size", 64)
    items = getattr(config, "max_items", 100000)
    pages = getattr(config, "max_pages", 500)
    seconds = getattr(config, "run_seconds", 180)
    _require(type(batch) is int and 1 <= batch <= 100 and type(items) is int and 1 <= items <= 100000 and
             type(pages) is int and 1 <= pages <= 500 and type(seconds) in (int, float) and
             math.isfinite(seconds) and 15 <= seconds <= 180, "invalid_resumable_config")
    libs = [lib for lib in config.libraries if lib.enabled]
    _require(len(libs) == 1 and type(libs[0].recursive) is bool, "invalid_resumable_config")
    return libs[0], batch, items, pages, seconds


def _archive(store, control, lib_id, save=True):
    job = control["job"]
    if job and (job["copy_refs"] or job["intent_ref"]):
        refs = control["completed"]["ownership_refs"]
        _require(len(refs) < _MAX_ARCHIVES, "ownership_archive_limit")
        # A changed configuration may now name a different library. Preserve the
        # old boundary from its durable copy path, never label it with new config.
        if job["intent_ref"] is not None:
            sample = store.read_chunk(job["intent_ref"])
        else:
            sample = next(_expand(store, job["copy_refs"], "copy", job["id"], lambda: None))
        _require(isinstance(sample, dict) and isinstance(sample.get("records"), list) and
                 sample["records"], "invalid_private_ledger")
        original_path = sample["records"][0].get("path")
        _require(isinstance(original_path, str) and MANAGED_PATH.fullmatch(original_path), "invalid_private_ledger")
        lib_id = original_path.split("/")[2]
        refs.append(_put(store, job["id"], "snapshot", {
            "owner": control["owner"], "config_fingerprint": control["config_fingerprint"],
            "library_id": lib_id, "root": job["root"], "job_id": job["id"],
            "copy_refs": job["copy_refs"], "intent_ref": job["intent_ref"]}))
    control["job"] = None
    if save:
        _save(store, control)


def _upgrade_root(store, control, lib, check):
    """The old durable scope must prove the SAME resolved root before widening.

    Completed legacy controls without any root proof cannot be safely migrated.
    A successful new run saves its root; an active legacy job already has it.
    """
    if control["job"] is not None:
        return control["job"]["root"]
    if "root" in control["completed"]:
        return control["completed"]["root"]
    refs = control["completed"]["ownership_refs"][:]
    if control["completed"].get("invalid_ref") is not None:
        refs.append(control["completed"]["invalid_ref"])
    roots = []
    for ref in refs:
        check()
        value = store.read_chunk(ref)
        fields = {"owner", "config_fingerprint", "library_id", "root", "job_id", "copy_refs"}
        _require(isinstance(value, dict) and set(value) in (fields, fields | {"intent_ref"}),
                 "invalid_private_ledger")
        _require(_ref(ref)[0] == value["job_id"], "invalid_private_ledger")
        PagedGraph._checked_root(value["root"])
        if (value["owner"] == control["owner"] and value["library_id"] == lib.id and
                value["config_fingerprint"] == control["config_fingerprint"]):
            roots.append(value["root"])
    return roots[0] if roots and all(root == roots[0] for root in roots) else None


def _archived_proof(store, control, lib, root, check, old_fingerprint=None, rejected=None):
    proof = {}
    for ref in control["completed"]["ownership_refs"]:
        check()
        archive = store.read_chunk(ref)
        fields = {"owner", "config_fingerprint", "library_id", "root", "job_id", "copy_refs", "intent_ref"}
        _require(isinstance(archive, dict) and set(archive) == fields, "invalid_private_ledger")
        _require(_ref(ref)[0] == archive["job_id"], "invalid_private_ledger")
        PagedGraph._checked_root(archive["root"])
        if (archive["owner"] != control["owner"] or
                archive["config_fingerprint"] not in (control["config_fingerprint"], old_fingerprint) or
                archive["library_id"] != lib.id or archive["root"] != root):
            continue  # Old-account/config/root ledgers preserve ownership but never authorize adoption.
        _require(isinstance(archive["copy_refs"], list) and len(archive["copy_refs"]) <= 128, "invalid_private_ledger")
        payloads = list(_expand(store, archive["copy_refs"], "copy", archive["job_id"], check))
        if archive["intent_ref"] is not None:
            _ref(archive["intent_ref"], archive["job_id"], "intent")
            payloads.append(store.read_chunk(archive["intent_ref"]))
        for payload in payloads:
            _require(isinstance(payload, dict) and set(payload) == {"start", "end", "records"}, "invalid_private_ledger")
            for rec in _records(payload["records"], lib.id):
                if "skip" in rec:
                    if rejected is not None and archive["config_fingerprint"] == old_fingerprint:
                        rejected[(rec["id"], rec["version"])] = rec
                    continue  # A rejected source is never ownership/upload proof.
                previous = proof.get(rec["path"])
                if previous is None or rec["sha256"] is not None:
                    proof[rec["path"]] = rec
    return proof


def _previous_invalid(store, control, lib, root, check, old_fingerprint=None):
    """Only the last complete, same-owner/root scan authorizes a cached rejection."""
    ref = control["completed"].get("invalid_ref")
    if ref is None:
        return {}
    value = store.read_chunk(ref)
    fields = {"owner", "config_fingerprint", "library_id", "root", "job_id", "copy_refs"}
    _require(isinstance(value, dict) and set(value) == fields, "invalid_private_ledger")
    _require(_ref(ref)[0] == value["job_id"], "invalid_private_ledger")
    PagedGraph._checked_root(value["root"])
    if (value["owner"] != control["owner"] or
            value["config_fingerprint"] not in (control["config_fingerprint"], old_fingerprint) or
            value["library_id"] != lib.id or value["root"] != root):
        return {}
    _require(isinstance(value["copy_refs"], list) and len(value["copy_refs"]) <= 128, "invalid_private_ledger")
    rejected = {}
    count = 0
    for payload in _expand(store, value["copy_refs"], "copy", value["job_id"], check):
        _require(isinstance(payload, dict) and set(payload) == {"start", "end", "records"}, "invalid_private_ledger")
        for rec in _records(payload["records"], lib.id):
            count += 1
            _require(count <= 100000, "invalid_private_ledger")
            if "skip" in rec:
                key = (rec["id"], rec["version"])
                _require(key not in rejected, "invalid_private_ledger")
                rejected[key] = rec
    return rejected


def _retained(store, control):
    if control["retained"] is None:
        return {}
    value = store.read_chunk(control["retained"])
    _require(isinstance(value, dict) and set(value) == {"owner", "libraries"} and
             value["owner"] == control["owner"] and isinstance(value["libraries"], dict), "invalid_private_ledger")
    count = 0
    for ident, lib in value["libraries"].items():
        _require(isinstance(lib, dict) and set(lib) == {"items"} and isinstance(lib["items"], dict), "invalid_private_ledger")
        for item in lib["items"].values():
            _require(isinstance(item, dict) and set(item) == {"path", "version"} and
                     isinstance(item["version"], str) and isinstance(item["path"], str) and
                     MANAGED_PATH.fullmatch(item["path"]) and
                     item["path"].startswith("photos/onedrive/" + ident + "/"), "invalid_private_ledger")
            count += 1
    _require(count <= 6, "invalid_private_ledger")
    return value["libraries"]


def _target(state, retained, lib, job, records, partial=False):
    target = copy.deepcopy(state["libraries"])
    items = copy.deepcopy(target.get(lib.id, {}).get("items", {})) if partial else {}
    skipped = dict(job["skipped"])
    for rec in records:
        if "skip" in rec:
            skipped[rec["skip"]] = skipped.get(rec["skip"], 0) + 1
            continue
        previous = items.get(rec["id"])
        if previous and previous["path"] != rec["path"]:
            items["previous:" + hashlib.sha256(previous["path"].encode()).hexdigest()] = previous
        items[rec["id"]] = {"path": rec["path"], "version": rec["version"]}
    target[lib.id] = {"items": items, "source_path": lib.path, "recursive": lib.recursive, "skipped": skipped}
    for ident, original in retained.items():
        dest = target.setdefault(ident, {"items": {}})["items"]
        paths = {item["path"] for item in dest.values()}
        for key, item in original["items"].items():
            if item["path"] in paths:
                continue
            if key in dest:
                key = "retained:" + hashlib.sha256(item["path"].encode()).hexdigest()
            dest[key] = copy.deepcopy(item)
            paths.add(item["path"])
    return target


def _target_digest(target):
    return hashlib.sha256(json.dumps(target, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _items(target):
    return {key: value["items"] for key, value in target.items()}


def _transaction_target(tx, target):
    _require(isinstance(tx, dict) and set(tx) == {"libraries", "manifest"} and
             tx["libraries"] == target, "pending_publication")
    value = tx["manifest"]
    _require(isinstance(value, dict) and set(value) == {"version", "images", "updatedAt"} and
             type(value["version"]) is int and value["version"] == 1 and
             isinstance(value["updatedAt"], str) and
             value["images"] == manifest(target)["images"], "pending_publication")


def run_resumable(store, config, start_new=True, deadline=None, graph_factory=PagedGraph,
                  clock=time.monotonic, now=time.time):
    """Return only safe status/counters; caller owns the primary lease and lifecycle.

    Idle continuation reads only small private control (never primary/cache/Graph).
    Deadline is monotonic; persisted retry_at is UTC Unix seconds, not monotonic.
    """
    graph = None
    control = None
    job = None
    lib = None
    checkpoint = None
    records = []
    state = None
    progressive = bool(getattr(config, "progressive", False))
    publishing = False
    published_any = False
    publish_prefix = None
    result = {"status": "failed"}

    def summary(value):
        if not progressive or job is None:
            return value
        reasons = dict(job["skipped"])
        copied, bytecount = 0, 0
        for rec in records:
            if "skip" in rec:
                reasons[rec["skip"]] = reasons.get(rec["skip"], 0) + 1
            else:
                copied += 1
                bytecount += rec["bytes"]
        images = len({item["path"] for library in (state or {}).get("libraries", {}).values()
                      for item in library["items"].values()})
        return dict(value, phase="complete" if control["job"] is None else job["phase"],
                    scanned=job["entrycount"], copied=copied, bytes=bytecount,
                    images=images, omitted=sum(reasons.values()), reasons=reasons)

    try:
        started = clock()
        seconds = getattr(config, "run_seconds", 180)
        # Validate before accepting any untrusted persisted work or source settings.
        if not config.enabled:
            return {"status": "disabled"}
        lib, batch_size, max_items, max_pages, seconds = _limits(config)
        if progressive:
            batch_size = min(batch_size, 64)
        end = min(started + seconds, deadline if deadline is not None else started + seconds)
        work_end = end - (20 if progressive else _CHECKPOINT_RESERVE)

        def check():
            if clock() >= work_end:
                raise PublisherError("engine_deadline")

        check()
        control = store.load_control()
        if control is not None:
            validate_control(control)
        if not start_new and (control is None or control["job"] is None):
            return {"status": "idle"}
        if control and control["job"] and control["job"]["retry_at"] is not None:
            if now() < control["job"]["retry_at"]:
                return {"status": "deferred", "retry_at": control["job"]["retry_at"]}
        check()
        state = store.load()
        if not state["connected"]:
            return {"status": "disconnected"}
        fingerprint = _fingerprint(config, lib, state["owner"])
        old_fingerprint = _fingerprint(config, replace(lib, recursive=False), state["owner"])
        widening = (control is not None and lib.recursive is True and
                    control["owner"] == state["owner"] and
                    control["config_fingerprint"] == old_fingerprint and
                    state["transaction"] is None and
                    not (control["job"] and control["job"].get("publication") is not None))
        if control is not None and (control["config_fingerprint"] != fingerprint or control["owner"] != state["owner"]):
            # Keep bootstrap/old-account proof intact; require deliberate private recovery
            # instead of silently resetting its ownership boundary.
            if not widening:
                if not (state["transaction"] is not None or
                        (control["job"] and control["job"].get("publication") is not None)):
                    _archive(store, control, lib.id)
                return {"status": "aborted", "reason": "ownership_or_config_changed"}
        if control is None:
            control = {"version": 1, "config_fingerprint": fingerprint, "owner": state["owner"],
                       "retained": None, "job": None,
                       "completed": {"runs": 0, "images": 0, "ownership_refs": []}}
            if getattr(config, "retain_initial", False):
                originals = {ident: {"items": copy.deepcopy(value["items"])}
                             for ident, value in state["libraries"].items()}
                _require(sum(len(value["items"]) for value in originals.values()) <= 6, "retained_initial_bound")
                control["retained"] = _put(store, uuid.uuid4().hex, "snapshot",
                                           {"owner": state["owner"], "libraries": originals})
            _save(store, control)  # Bootstrap is captured once, never from later scans.
        retained = _retained(store, control)
        managed_paths = set(state["managed"])
        _require(all(item["path"] in managed_paths for value in retained.values()
                     for item in value["items"].values()), "invalid_private_ledger")
        check()
        token = silent_token(store, state, config.client_id)
        check()
        graph = graph_factory(token, deadline=work_end)
        graph.skip_invalid = bool(getattr(config, "skip_invalid", False))
        root = graph.root(lib)
        PagedGraph._checked_root(root)
        check()
        if widening:
            previous_root = _upgrade_root(store, control, lib, check)
            if previous_root != root:
                return {"status": "aborted", "reason": "source_root_changed"}
            # One atomic checkpoint: old immutable SHA/intent proof, retained ref
            # and counters survive; cursor MUST restart to discover descendants.
            _archive(store, control, lib.id, save=False)
            control["recursive_upgrade"] = {"from": old_fingerprint, "root": root}
            control["config_fingerprint"] = fingerprint
            _save(store, control)
        upgrade = control.get("recursive_upgrade")
        if upgrade is not None:
            _require(lib.recursive is True and upgrade["from"] == old_fingerprint and
                     control["config_fingerprint"] == fingerprint, "invalid_work_control")
        allowed_old = old_fingerprint if upgrade is not None and upgrade["root"] == root else None
        job = control["job"]
        if job is not None and job["root"] != root:
            if state["transaction"] is None and job.get("publication") is None:
                _archive(store, control, lib.id)
            return summary({"status": "aborted", "reason": "source_root_changed"})
        if job is None:
            _require(state["transaction"] is None, "pending_publication")
            job = {"id": uuid.uuid4().hex, "root": root, "phase": "scan", "scan_refs": [],
                   "next_cursor": None, "entrycount": 0, "pagecount": 0, "candidatecount": 0,
                   "copy_page": 0, "copy_offset": 0, "copy_refs": [], "intent_ref": None,
                   "retry_at": None, "skipped": {}}
            control["job"] = job
            _save(store, control)
        # Optional marker: old controls remain valid, flags never alter ownership.
        if progressive:
            job.setdefault("publication", None)
        job["retry_at"] = None
        graph.expected_root = root
        graph.recursive = lib.recursive
        pages, seen, cursors = _load_pages(store, job, check, lib.recursive)
        _require(job["entrycount"] <= max_items and job["pagecount"] <= max_pages, "item_bound")
        if job["phase"] == "scan":
            while True:
                check()
                _require(job["pagecount"] < max_pages, "pagination_bound")
                page = _page(graph.scan_page(root, job["next_cursor"]), root, lib.recursive)
                _require(not seen.intersection(page["ids"]), "duplicate_item_during_scan")
                _require(job["entrycount"] + page["entries"] <= max_items, "item_bound")
                if page["next"] is not None:
                    _require(page["next"] not in cursors, "pagination_bound")
                    cursors.add(page["next"])
                ref = _put(store, job["id"], "scan", page)
                _append(store, job, "scan", ref)
                job["entrycount"] += page["entries"]
                job["pagecount"] += 1
                job["candidatecount"] += len(page["rows"])
                job["next_cursor"] = page["next"]
                for label, count in page["skipped"].items():
                    job["skipped"][label] = job["skipped"].get(label, 0) + count
                seen.update(page["ids"])
                pages.append(page)
                if page["next"] is None:
                    job["phase"] = "copy"
                    job["copy_page"], job["copy_offset"] = _position(pages, [0, 0])
                _save(store, control)  # Page contents durable before cursor advancement.
                if page["next"] is None:
                    break
        records = _completions(store, job, pages, lib.id, check)
        position = [job["copy_page"], job["copy_offset"]]
        _require(position == _position(pages, position), "invalid_private_ledger")
        pending = []
        pending_start = position[:]

        def publish_completed(final=False):
            nonlocal publishing, published_any
            marker = job.get("publication")
            if marker is None and not final and not any("skip" not in rec for rec in records):
                _require(state["transaction"] is None, "pending_publication")
                return  # All-invalid prefixes never create an empty first manifest.
            if marker is not None:
                _require(marker["position"] == position and
                         marker["final"] == (position == [len(pages), 0]), "invalid_private_ledger")
                final = marker["final"]
            target = _target(state, retained, lib, job, records, partial=not final)
            digest = _target_digest(target)
            changed = _items(target) != _items(state["libraries"])
            if marker is not None:
                _require(marker["sha256"] == digest, "invalid_private_ledger")
            else:
                if state["transaction"] is not None:
                    # A pre-flag legacy final transaction has no partial marker.
                    # Only a complete ledger and its exact final target allow it.
                    _require(final and job["phase"] == "publish" and
                             position == [len(pages), 0], "pending_publication")
                    _transaction_target(state["transaction"], target)
                elif not changed:
                    return
                if final:
                    job["phase"] = "publish"
                job["publication"] = {"position": position[:], "final": final, "sha256": digest}
                _save(store, control)  # Exact durable ledger/cursor BEFORE primary transaction.
            if clock() >= end:
                raise PublisherError("engine_deadline")
            publishing = True
            state["managed"] = sorted(set(state["managed"]) |
                                      {rec["path"] for rec in records if "skip" not in rec})
            if state["transaction"] is not None:
                tx = state["transaction"]
                _transaction_target(tx, target)
                recover_publication(store, state)
                published_any = True
            elif changed:
                commit_publication(store, state, target)
                published_any = True
            # If the primary commit succeeded but this checkpoint crashed, the
            # same marker validates the already-committed target without republish.
            job["publication"] = None
            _save(store, control)
            publishing = False

        publish_prefix = publish_completed

        def flush():
            nonlocal pending, pending_start
            if pending:
                payload = {"start": pending_start, "end": position[:], "records": pending}
                ref = _put(store, job["id"], "copy", payload)
                _append(store, job, "copy", ref)
                job["copy_page"], job["copy_offset"] = position
                records.extend(pending)
                pending, pending_start = [], position[:]
            _save(store, control)

        checkpoint = flush
        # Recover a partial transaction (or a durable unpublished prefix) BEFORE
        # inventory/downloads. A partial primary tx is never the final snapshot.
        if job.get("publication") is not None or (progressive and records):
            publish_completed(final=position == [len(pages), 0])
        elif progressive and not (job["phase"] == "publish" and position == [len(pages), 0]):
            _require(state["transaction"] is None, "pending_publication")
        if job["phase"] == "copy" and position[0] < len(pages):
            check()
            inventory = store.public_inventory(lib.id)  # Exactly once per copy invocation; no per-photo HEAD.
            check()
            published = state["libraries"].get(lib.id, {}).get("items", {})
            published_proof = {item["path"]: item for item in published.values()}
            rejected = (_previous_invalid(store, control, lib, root, check, allowed_old)
                        if getattr(config, "skip_invalid", False) else {})
            archived = _archived_proof(store, control, lib, root, check, allowed_old,
                                       rejected if getattr(config, "skip_invalid", False) else None)
            while position[0] < len(pages):
                check()
                if job["intent_ref"] is None:
                    rows, batch_end = _slice(pages, position, batch_size)
                    intents = []
                    for row in rows:
                        rec = _record(row, lib.id)
                        info = inventory.get(rec["path"])
                        known = published_proof.get(rec["path"])
                        owned = archived.get(rec["path"])
                        if info is not None:
                            _require((known and known["version"] == rec["version"]) or owned,
                                     "foreign_copy_collision")
                            _require(info["bytes"] == rec["bytes"], "foreign_copy_collision")
                            if owned and owned["sha256"] is not None:
                                _require(info["sha256"] == owned["sha256"], "foreign_copy_collision")
                            rec["sha256"] = info["sha256"] if known else owned["sha256"]
                        intents.append(rec)
                    job["intent_ref"] = _put(store, job["id"], "intent",
                                             {"start": position[:], "end": batch_end, "records": intents})
                    _save(store, control)  # All copy ownership intents BEFORE any upload.
                intent = store.read_chunk(job["intent_ref"])
                rows = _batch_payload(intent, pages, lib.id)
                _require(intent["start"] <= position < intent["end"], "invalid_private_ledger")
                cursor = intent["start"][:]
                for row, intent_rec in zip(rows, intent["records"]):
                    if progressive and len(pending) >= 64:
                        break  # A pre-flag legacy intent may contain up to 100 rows.
                    _, next_position = _slice(pages, cursor, 1)
                    if cursor < position:
                        cursor = next_position
                        continue
                    check()
                    rec = copy.deepcopy(intent_rec)
                    info = inventory.get(rec["path"])
                    known = published_proof.get(rec["path"])
                    if info is not None and known and known["version"] == rec["version"]:
                        _require(info["bytes"] == rec["bytes"], "foreign_copy_collision")
                        rec["sha256"] = info["sha256"]
                    elif info is not None and rec["sha256"] is not None:
                        _require(info == {"bytes": rec["bytes"], "sha256": rec["sha256"]}, "foreign_copy_collision")
                    else:
                        # SHA-ambiguous staged intents may re-download ONCE to prove
                        # bytes against stored metadata. Completed prefix never does.
                        previous_invalid = rejected.get((rec["id"], rec["version"]))
                        if info is None and previous_invalid is not None:
                            expected = dict(rec, sha256=None, skip=previous_invalid["skip"])
                            _require(previous_invalid == expected, "download_item_changed")
                            pending.append(expected)
                            position = cursor = next_position
                            continue
                        try:
                            # The inherited Graph download keeps its byte/version,
                            # redirect and raster guarantees. BOTH item() reads use
                            # this validated durable row parent and fresh ancestry.
                            graph.expected_parent = row.get("parent_id", root["id"])
                            content = graph.download(row, row["ext"])
                        except PublisherError as exc:
                            if (not getattr(config, "skip_invalid", False) or
                                    str(exc) not in _COPY_SKIP or info is not None):
                                raise
                            rec["sha256"] = None
                            rec["skip"] = str(exc)
                            pending.append(rec)
                            position = cursor = next_position
                            continue
                        _require(isinstance(content, bytes) and len(content) == rec["bytes"], "download_item_changed")
                        rec["sha256"] = hashlib.sha256(content).hexdigest()
                        check()
                        if info is not None:
                            _require(info == {"bytes": len(content), "sha256": rec["sha256"]}, "foreign_copy_collision")
                        else:
                            store.upload_original(rec["path"], content, TYPES[row["ext"]], rec["sha256"])
                            inventory[rec["path"]] = {"bytes": len(content), "sha256": rec["sha256"]}
                    pending.append(rec)
                    position = next_position
                    cursor = next_position
                if position == intent["end"]:
                    job["intent_ref"] = None
                flush()  # One completion chunk/checkpoint per batch (or partial budget).
                if progressive:
                    publish_completed(final=position == [len(pages), 0])
        check()
        _require(position == [len(pages), 0] and len(records) == job["candidatecount"], "invalid_private_ledger")
        if job["phase"] != "publish":
            job["phase"] = "publish"
            _save(store, control)
        checkpoint = None
        target = _target(state, retained, lib, job, records)
        managed = set(state["managed"])
        managed.update(rec["path"] for rec in records if "skip" not in rec)
        state["managed"] = sorted(managed)
        check()
        if progressive:
            publish_completed(final=True)
            changed = published_any
        elif state["transaction"] is not None:
            _require(state["transaction"]["libraries"] == target, "pending_publication")
            recover_publication(store, state)
            changed = True
        else:
            changed = ({key: value["items"] for key, value in target.items()} !=
                       {key: value["items"] for key, value in state["libraries"].items()})
            if changed:
                commit_publication(store, state, target)  # BatchStore fences gallery's own lease.
        control["completed"]["runs"] += 1
        control["completed"]["images"] = len({item["path"] for value in target.values() for item in value["items"].values()})
        control["completed"]["root"] = root
        if any("skip" in rec for rec in records):
            control["completed"]["invalid_ref"] = _put(store, job["id"], "snapshot", {
                "owner": control["owner"], "config_fingerprint": control["config_fingerprint"],
                "library_id": lib.id, "root": root, "job_id": job["id"], "copy_refs": job["copy_refs"]})
        elif "invalid_ref" in control["completed"]:
            control["completed"]["invalid_ref"] = None
        control["job"] = None
        _save(store, control)
        return summary({"status": "ok" if changed else "unchanged", "images": control["completed"]["images"],
                        "entries": job["entrycount"], "pages": job["pagecount"],
                        "pending_cleanup": len(state["pending_cleanup"])})
    except Exception as exc:
        code = str(exc) if isinstance(exc, PublisherError) else "operation_failed"
        if control is not None and job is not None and control.get("job") is job:
            try:
                if checkpoint is not None:
                    checkpoint()  # Durable completed prefix even when budget/error interrupted a batch.
                if (progressive and code in _DEFER and not publishing and
                        publish_prefix is not None and clock() < end):
                    publish_prefix(final=[job["copy_page"], job["copy_offset"]] == [len(pages), 0])
                retry = getattr(graph, "retry_not_before", None)
                if type(retry) in (int, float) and math.isfinite(retry) and retry > now():
                    job["retry_at"] = retry
                if code in _FATAL:
                    _archive(store, control, lib.id)
                    result = {"status": "aborted", "reason": "source_or_scan_changed"}
                else:
                    _save(store, control)
                    result = {"status": "deferred" if code in _DEFER else "failed"}
                    if job["retry_at"] is not None:
                        result["retry_at"] = job["retry_at"]
                    if code == "foreign_copy_collision":
                        result["reason"] = code
            except Exception:
                result = {"status": "failed"}  # Never leak exception strings/responses/URLs.
        elif code in _DEFER:
            result = {"status": "deferred"}
        return summary(result)
    finally:
        if graph is not None:
            try:
                graph.close()
            except Exception:
                pass
