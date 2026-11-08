"""Unit tests use real Azure exception classes and explicit API mocks, not engine shims."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import (HttpResponseError, ResourceExistsError,
                                   ResourceModifiedError, ResourceNotFoundError)

import batch_storage
from batch_storage import (BatchStore, CONTROL_BLOB, MAX_CHUNK_BYTES,
                           MAX_CONTROL_BYTES)
from config import Config, PublisherError, photo_path
from storage import initial_state, validate_state

JOB = "a" * 32
DIGEST = hashlib.sha256(b"original").hexdigest()
PATH = photo_path("spotify", "photo", "version", "jpg")
MANIFEST = {"version": 1, "images": [PATH], "updatedAt": "2026-11-08T00:00:00Z"}


def blob_mock():
    return Mock(spec=["get_blob_properties", "download_blob", "upload_blob", "acquire_lease",
                      "exists", "delete_blob"])


@pytest.fixture
def azure_store():
    private = Mock(spec=["get_blob_client", "get_container_properties", "create_container"])
    public = Mock(spec=["get_blob_client", "get_container_properties", "list_blobs", "create_container"])
    state, work, original, gallery = [blob_mock() for _ in range(4)]
    chunks = {}

    def private_blob(name):
        if name == "publisher-state.json":
            return state
        if name == CONTROL_BLOB:
            return work
        return chunks.setdefault(name, blob_mock())

    private.get_blob_client.side_effect = private_blob
    public.get_blob_client.side_effect = lambda name: gallery if name == "gallery.json" else original
    service = Mock(spec=["get_container_client", "close"])
    service.get_container_client.side_effect = lambda name: public if name == "$web" else private
    store = BatchStore(Config(False, [], ""), service=service)
    primary = Mock(spec=["renew", "release"])
    store.lease = primary
    own_lease = Mock(spec=["renew", "release"])
    gallery.acquire_lease.return_value = own_lease
    yield SimpleNamespace(store=store, private=private, public=public, state=state,
                          work=work, original=original, gallery=gallery, chunks=chunks,
                          primary=primary, own_lease=own_lease)
    # None of the new methods may create resources, delete blobs, or write primary state.
    private.create_container.assert_not_called()
    public.create_container.assert_not_called()
    state.upload_blob.assert_not_called()
    for blob in [state, work, original, gallery, *chunks.values()]:
        blob.delete_blob.assert_not_called()


def configure_read(blob, raw, *, size=None, etag='"version-1"'):
    blob.get_blob_properties.return_value = {"size": len(raw) if size is None else size, "etag": etag}
    blob.download_blob.return_value.readall.return_value = raw


def chunk_ref(raw=b"{}", name=None):
    return {"name": name or f"jobs/{JOB}/scan/000000.json",
            "sha256": hashlib.sha256(raw).hexdigest()}


def listing(azure, pages):
    azure.public.list_blobs.return_value.by_page.return_value = iter(pages)


def test_missing_control_is_none_not_initialization(azure_store):
    a = azure_store
    a.work.get_blob_properties.side_effect = ResourceNotFoundError("missing")
    assert a.store.load_control() is None
    a.work.upload_blob.assert_not_called()
    a.private.get_blob_client.assert_called_with(CONTROL_BLOB)


def test_load_control_is_bounded_and_etag_fenced(azure_store):
    a = azure_store
    configure_read(a.work, b'{"phase":"scan"}')
    assert a.store.load_control() == {"phase": "scan"}
    a.work.download_blob.assert_called_once_with(length=MAX_CONTROL_BYTES + 1,
        etag='"version-1"', match_condition=MatchConditions.IfNotModified)


@pytest.mark.parametrize("raw", [b"[]", b"null", b'"scalar"', b'{"a":1,"a":2}',
                                    b'{"a":NaN}', b'{"a":Infinity}', b"{", b"\xff"])
def test_invalid_control_json_is_safe_error(azure_store, raw):
    configure_read(azure_store.work, raw)
    with pytest.raises(PublisherError, match="^invalid_work_control$"):
        azure_store.store.load_control()


@pytest.mark.parametrize("size", [MAX_CONTROL_BYTES + 1, -1, True, "1", None])
def test_control_head_bounds_before_download(azure_store, size):
    configure_read(azure_store.work, b"{}")
    azure_store.work.get_blob_properties.return_value["size"] = size
    with pytest.raises(PublisherError, match="invalid_work_control"):
        azure_store.store.load_control()
    azure_store.work.download_blob.assert_not_called()


@pytest.mark.parametrize("raw", [b"x" * (MAX_CONTROL_BYTES + 1), b"{\"different\":1}"])
def test_control_read_enforces_actual_bounds_and_size(azure_store, raw):
    configure_read(azure_store.work, raw, size=2)
    with pytest.raises(PublisherError, match="invalid_work_control"):
        azure_store.store.load_control()


def test_control_rejects_missing_etag(azure_store):
    configure_read(azure_store.work, b"{}", etag=None)
    with pytest.raises(PublisherError, match="invalid_work_control"):
        azure_store.store.load_control()
    azure_store.work.download_blob.assert_not_called()


def test_control_accepts_exact_limit_and_save_checks_same_limit(azure_store):
    data = {"x": "a" * (MAX_CONTROL_BYTES - 8)}
    raw = json.dumps(data, separators=(",", ":")).encode()
    assert len(raw) == MAX_CONTROL_BYTES
    configure_read(azure_store.work, raw)
    assert azure_store.store.load_control() == data
    azure_store.work.get_blob_properties.side_effect = ResourceNotFoundError("missing")
    azure_store.store.save_control(data)
    assert len(azure_store.work.upload_blob.call_args.args[0]) == MAX_CONTROL_BYTES
    with pytest.raises(PublisherError, match="invalid_work_control"):
        azure_store.store.save_control({"x": "a" * (MAX_CONTROL_BYTES - 7)})


@pytest.mark.parametrize("data", [[], None, {"nan": float("nan")}, {"x": object()},
                                     {"large": "a" * MAX_CONTROL_BYTES}])
def test_bad_control_save_cannot_call_azure(azure_store, data):
    a = azure_store
    with pytest.raises(PublisherError, match="invalid_work_control"):
        a.store.save_control(data)
    a.primary.renew.assert_not_called()
    a.work.upload_blob.assert_not_called()


def test_save_control_create_only_and_conditional_overwrite(azure_store):
    a = azure_store
    a.work.get_blob_properties.side_effect = ResourceNotFoundError("missing")
    a.store.save_control({"job": None})
    assert a.work.upload_blob.call_args.kwargs["overwrite"] is False
    assert a.work.upload_blob.call_args.kwargs["content_settings"].content_type == "application/json"
    a.work.get_blob_properties.side_effect = None
    a.work.get_blob_properties.return_value = {"etag": '"previous"'}
    a.store.save_control({"job": {"phase": "scan"}})
    args = a.work.upload_blob.call_args.kwargs
    assert args["overwrite"] is True
    assert args["etag"] == '"previous"'
    assert args["match_condition"] is MatchConditions.IfNotModified
    assert "lease" not in args  # Primary lease is not a lease on publisher-work.json.
    assert a.primary.renew.call_count == 6


@pytest.mark.parametrize("existing,error", [
    (False, ResourceExistsError("raced creation")),
    (True, ResourceModifiedError("etag changed")),
    (True, HttpResponseError("forbidden")),
])
def test_control_conflicts_and_azure_errors_never_reset(azure_store, existing, error):
    a = azure_store
    if existing:
        a.work.get_blob_properties.return_value = {"etag": '"old"'}
    else:
        a.work.get_blob_properties.side_effect = ResourceNotFoundError("missing")
    a.work.upload_blob.side_effect = error
    with pytest.raises(type(error)):
        a.store.save_control({"job": None})
    assert a.work.upload_blob.call_count == 1


@pytest.mark.parametrize("kind", ["scan", "copy", "intent", "snapshot"])
def test_put_chunk_is_deterministic_create_only_and_private(azure_store, kind):
    a = azure_store
    payload = {"z": [1, 2], "a": "value"}
    ref = a.store.put_chunk(JOB, kind, 42, payload)
    assert ref["name"] == f"jobs/{JOB}/{kind}/000042.json"
    blob = a.chunks[ref["name"]]
    raw = blob.upload_blob.call_args.args[0]
    assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
    assert blob.upload_blob.call_args.kwargs["overwrite"] is False
    configure_read(blob, raw)
    blob.upload_blob.side_effect = ResourceExistsError("already exists")
    assert a.store.put_chunk(JOB, kind, 42, {"a": "value", "z": [1, 2]}) == ref
    a.public.get_blob_client.assert_not_called()


@pytest.mark.parametrize("job,kind,index,payload", [
    ("../escape", "scan", 0, {}), ("A" * 32, "scan", 0, {}),
    (JOB, "unknown", 0, {}), (JOB, "scan/../copy", 0, {}),
    (JOB, "scan", -1, {}), (JOB, "scan", 1000000, {}),
    (JOB, "scan", True, {}), (JOB, "scan", 1.0, {}),
    (JOB, "scan", 0, "text"), (JOB, "scan", 0, {"x": float("inf")}),
    (JOB, "scan", 0, {"large": "x" * MAX_CHUNK_BYTES}),
])
def test_chunk_inputs_fail_before_azure(azure_store, job, kind, index, payload):
    with pytest.raises(PublisherError, match="invalid_chunk"):
        azure_store.store.put_chunk(job, kind, index, payload)
    azure_store.primary.renew.assert_not_called()
    assert not azure_store.chunks


def test_chunk_boundaries_exact_size_and_last_index(azure_store):
    a = azure_store
    payload = {"x": "a" * (MAX_CHUNK_BYTES - 8)}
    ref = a.store.put_chunk(JOB, "snapshot", 999999, payload)
    blob = a.chunks[ref["name"]]
    raw = blob.upload_blob.call_args.args[0]
    assert len(raw) == MAX_CHUNK_BYTES
    configure_read(blob, raw)
    assert a.store.read_chunk(ref) == payload


def test_existing_chunk_different_digest_fails_without_overwrite(azure_store):
    a = azure_store
    name = f"jobs/{JOB}/scan/000000.json"
    blob = a.private.get_blob_client(name)
    blob.upload_blob.side_effect = ResourceExistsError("exists")
    configure_read(blob, b'{"different":true}')
    with pytest.raises(PublisherError, match="^private_chunk_collision$"):
        a.store.put_chunk(JOB, "scan", 0, {})
    assert blob.upload_blob.call_count == 1
    assert blob.upload_blob.call_args.kwargs["overwrite"] is False


@pytest.mark.parametrize("ref", [
    None, {}, {"name": PATH, "sha256": DIGEST},
    chunk_ref(name=f"jobs/{JOB}/scan/../000000.json"),
    chunk_ref(name=f"jobs/{JOB}/scan/1000000.json"),
    {**chunk_ref(), "sha256": "A" * 64}, {**chunk_ref(), "extra": True},
    chunk_ref(name="https://example.test/secret?token=private"),
])
def test_chunk_ref_is_strict_and_cannot_read_public_or_foreign(azure_store, ref):
    with pytest.raises(PublisherError, match="^invalid_chunk_ref$"):
        azure_store.store.read_chunk(ref)
    assert not azure_store.chunks
    azure_store.public.get_blob_client.assert_not_called()
    azure_store.primary.renew.assert_not_called()


@pytest.mark.parametrize("raw,value", [(b"{}", {}), (b"[]", []), (b'[1,{"ok":true}]', [1, {"ok": True}])])
def test_read_chunk_verifies_hash_and_json_shape(azure_store, raw, value):
    ref = chunk_ref(raw)
    blob = azure_store.private.get_blob_client(ref["name"])
    configure_read(blob, raw)
    assert azure_store.store.read_chunk(ref) == value
    blob.download_blob.assert_called_once_with(length=MAX_CHUNK_BYTES + 1,
        etag='"version-1"', match_condition=MatchConditions.IfNotModified)


@pytest.mark.parametrize("raw", [b"null", b"12", b'"text"', b"NaN", b'{"x":1,"x":2}', b"\xff"])
def test_chunk_json_invalid_even_with_matching_hash(azure_store, raw):
    ref = chunk_ref(raw)
    blob = azure_store.private.get_blob_client(ref["name"])
    configure_read(blob, raw)
    with pytest.raises(PublisherError, match="^invalid_chunk$"):
        azure_store.store.read_chunk(ref)


def test_read_chunk_digest_tampering_and_oversize(azure_store):
    a = azure_store
    ref = chunk_ref()
    blob = a.private.get_blob_client(ref["name"])
    configure_read(blob, b"[]")
    with pytest.raises(PublisherError, match="private_chunk_digest_mismatch"):
        a.store.read_chunk(ref)
    blob.get_blob_properties.return_value = {"size": MAX_CHUNK_BYTES + 1, "etag": "etag"}
    blob.download_blob.reset_mock()
    with pytest.raises(PublisherError, match="invalid_chunk"):
        a.store.read_chunk(ref)
    blob.download_blob.assert_not_called()


@pytest.mark.parametrize("operation", ["load_control", "read_chunk", "put_chunk", "inspect_original", "upload_original", "publish", "public_inventory", "save_control"])
def test_primary_lease_loss_fences_every_operation_before_azure(azure_store, operation):
    a = azure_store
    a.primary.renew.side_effect = RuntimeError("private failure diagnostics")
    calls = {
        "load_control": lambda: a.store.load_control(),
        "read_chunk": lambda: a.store.read_chunk(chunk_ref()),
        "put_chunk": lambda: a.store.put_chunk(JOB, "scan", 0, {}),
        "inspect_original": lambda: a.store.inspect_original(PATH),
        "upload_original": lambda: a.store.upload_original(PATH, b"original", "image/jpeg", DIGEST),
        "publish": lambda: a.store.publish(MANIFEST),
        "public_inventory": lambda: a.store.public_inventory("spotify"),
        "save_control": lambda: a.store.save_control({}),
    }
    with pytest.raises(PublisherError, match="^lease_lost$"):
        calls[operation]()
    a.public.get_blob_client.assert_not_called()
    a.public.list_blobs.assert_not_called()
    a.work.upload_blob.assert_not_called()
    assert not a.chunks


def test_inventory_paginates_metadata_and_filters_foreign_paths_without_heads(azure_store):
    a = azure_store
    path2 = photo_path("spotify", "second", "tag", "png")
    listing(a, [[{"name": PATH, "size": 8, "metadata": {"onedrive_sha256": DIGEST}},
                 {"name": "photos/onedrive/spotify/foreign.jpg", "size": 1}],
                [SimpleNamespace(name=path2, size=9, metadata={}),
                 {"name": photo_path("other", "x", "v", "jpg"), "size": 4},
                 {"name": PATH + "?signed=never", "size": 1}]])
    assert a.store.public_inventory("spotify") == {
        PATH: {"bytes": 8, "sha256": DIGEST}, path2: {"bytes": 9, "sha256": None}}
    a.public.list_blobs.assert_called_once_with(name_starts_with="photos/onedrive/spotify/", include=["metadata"])
    a.public.get_blob_client.assert_not_called()
    a.primary.renew.assert_called_once()


@pytest.mark.parametrize("library_id", ["../unsafe", "Spotify", "", None, "spotify/other"])
def test_inventory_rejects_unsafe_library_before_listing(azure_store, library_id):
    with pytest.raises(PublisherError, match="unsafe_library_id"):
        azure_store.store.public_inventory(library_id)
    azure_store.public.list_blobs.assert_not_called()


def test_inventory_limit_duplicate_and_loss_mid_page(azure_store, monkeypatch):
    a = azure_store
    monkeypatch.setattr(batch_storage, "MAX_INVENTORY_PATHS", 1)
    row = {"name": PATH, "size": 8, "metadata": {}}
    listing(a, [[row]])
    assert len(a.store.public_inventory("spotify")) == 1
    listing(a, [[row, {"name": photo_path("spotify", "second", "tag", "jpg"), "size": 1}]])
    with pytest.raises(PublisherError, match="public_inventory_limit"):
        a.store.public_inventory("spotify")
    listing(a, [[row], [row]])
    with pytest.raises(PublisherError, match="invalid_public_inventory"):
        a.store.public_inventory("spotify")

    def interrupted_page():
        yield row
        a.store.lost.set()
        yield {"name": "foreign"}

    listing(a, [interrupted_page()])
    with pytest.raises(PublisherError, match="^lease_lost$"):
        a.store.public_inventory("spotify")


def test_inventory_detects_loss_on_last_page_even_empty(azure_store):
    a = azure_store

    def pages():
        yield []
        a.store.lost.set()

    a.public.list_blobs.return_value.by_page.return_value = pages()
    with pytest.raises(PublisherError, match="lease_lost"):
        a.store.public_inventory("spotify")


@pytest.mark.parametrize("metadata,expected", [({}, None), (None, None), ({"onedrive_sha256": "not-a-digest"}, None),
    ({"onedrive_sha256": "A" * 64}, None), ({"onedrive_sha256": DIGEST}, DIGEST)])
def test_inspect_metadata_only_and_missing_digest_not_proof(azure_store, metadata, expected):
    a = azure_store
    a.original.get_blob_properties.return_value = {"size": 8, "metadata": metadata}
    assert a.store.inspect_original(PATH) == {"bytes": 8, "sha256": expected}
    a.original.download_blob.assert_not_called()
    a.original.upload_blob.assert_not_called()


def test_inspect_missing_is_none_but_other_azure_errors_propagate(azure_store):
    a = azure_store
    a.original.get_blob_properties.side_effect = ResourceNotFoundError("missing")
    assert a.store.inspect_original(PATH) is None
    a.original.get_blob_properties.side_effect = HttpResponseError("denied")
    with pytest.raises(HttpResponseError):
        a.store.inspect_original(PATH)


@pytest.mark.parametrize("size", [-1, True, "8", None])
def test_inspect_rejects_invalid_sizes(azure_store, size):
    azure_store.original.get_blob_properties.return_value = {"size": size, "metadata": {}}
    with pytest.raises(PublisherError, match="invalid_public_metadata"):
        azure_store.store.inspect_original(PATH)


def test_original_upload_create_only_verified_digest_and_mime(azure_store):
    a = azure_store
    a.store.upload_original(PATH, b"original", "image/jpeg", DIGEST)
    kwargs = a.original.upload_blob.call_args.kwargs
    assert kwargs["overwrite"] is False
    assert kwargs["metadata"] == {"onedrive_sha256": DIGEST}
    assert kwargs["content_settings"].content_type == "image/jpeg"
    assert kwargs["content_settings"].cache_control == "public, max-age=3600"
    a.primary.renew.assert_has_calls([call(), call()])
    a.original.get_blob_properties.assert_not_called()
    a.original.download_blob.assert_not_called()


@pytest.mark.parametrize("content,mime,digest,error", [
    (b"changed", "image/jpeg", DIGEST, "invalid_original_digest"),
    ("original", "image/jpeg", DIGEST, "invalid_original_digest"),
    (b"original", "image/jpeg", "A" * 64, "invalid_original_digest"),
    (b"original", "image/png", DIGEST, "invalid_original_mime"),
    (b"original", "text/html", DIGEST, "invalid_original_mime"),
])
def test_original_validation_precedes_upload(azure_store, content, mime, digest, error):
    with pytest.raises(PublisherError, match=error):
        azure_store.store.upload_original(PATH, content, mime, digest)
    azure_store.public.get_blob_client.assert_not_called()
    azure_store.primary.renew.assert_not_called()


def test_original_size_cap(azure_store, monkeypatch):
    monkeypatch.setattr(batch_storage, "MAX_ORIGINAL_BYTES", 7)
    with pytest.raises(PublisherError, match="invalid_original_digest"):
        azure_store.store.upload_original(PATH, b"original", "image/jpeg", DIGEST)
    azure_store.original.upload_blob.assert_not_called()


@pytest.mark.parametrize("size,metadata", [(8, {}), (7, {"onedrive_sha256": DIGEST}),
    (8, {"onedrive_sha256": "b" * 64}), (8, {"onedrive_sha256": "invalid"}),
    (None, {"onedrive_sha256": DIGEST}), (True, {"onedrive_sha256": DIGEST}),
    (8, ["invalid-metadata"])])
def test_existing_original_collision_never_overwrites_foreign(azure_store, size, metadata):
    a = azure_store
    a.original.upload_blob.side_effect = ResourceExistsError("exists")
    a.original.get_blob_properties.return_value = {"size": size, "metadata": metadata}
    with pytest.raises(PublisherError, match="^foreign_copy_collision$"):
        a.store.upload_original(PATH, b"original", "image/jpeg", DIGEST)
    a.original.upload_blob.assert_called_once()
    assert a.original.upload_blob.call_args.kwargs["overwrite"] is False
    a.original.download_blob.assert_not_called()


def test_existing_original_with_matching_size_and_digest_is_idempotent(azure_store):
    a = azure_store
    a.original.upload_blob.side_effect = ResourceExistsError("exists")
    a.original.get_blob_properties.return_value = {"size": 8, "metadata": {"onedrive_sha256": DIGEST}}
    assert a.store.upload_original(PATH, b"original", "image/jpeg", DIGEST) is None
    a.original.upload_blob.assert_called_once()
    a.original.download_blob.assert_not_called()


@pytest.mark.parametrize("path", ["gallery.json", "../secret", PATH + "?token=x", PATH.upper(), None])
def test_original_paths_strict_for_inspect_and_upload(azure_store, path):
    with pytest.raises(PublisherError, match="foreign_blob_path"):
        azure_store.store.inspect_original(path)
    with pytest.raises(PublisherError, match="foreign_blob_path"):
        azure_store.store.upload_original(path, b"original", "image/jpeg", DIGEST)
    azure_store.public.get_blob_client.assert_not_called()


def test_publish_missing_manifest_create_only_final_data(azure_store):
    a = azure_store
    a.store.publish(MANIFEST)
    a.gallery.upload_blob.assert_called_once()
    assert json.loads(a.gallery.upload_blob.call_args.args[0]) == MANIFEST
    kwargs = a.gallery.upload_blob.call_args.kwargs
    assert kwargs["overwrite"] is False
    assert kwargs["content_settings"].cache_control == "no-store"
    a.gallery.acquire_lease.assert_not_called()
    assert a.primary.renew.call_count == 2


def test_publish_existing_uses_own_60s_lease_and_primary_guards(azure_store):
    a = azure_store
    a.gallery.upload_blob.side_effect = [ResourceExistsError("exists"), None]
    a.store.publish(MANIFEST)
    a.gallery.acquire_lease.assert_called_once_with(lease_duration=60)
    kwargs = a.gallery.upload_blob.call_args.kwargs
    assert kwargs["overwrite"] is True
    assert kwargs["lease"] is a.own_lease
    assert kwargs["lease"] is not a.primary
    a.own_lease.release.assert_called_once()
    assert a.primary.renew.call_count == 4


@pytest.mark.parametrize("failure", ["primary_after_acquire", "upload", "primary_after_upload"])
def test_publish_failure_releases_own_lease_and_does_not_retry_unfenced(azure_store, failure):
    a = azure_store
    a.gallery.upload_blob.side_effect = [ResourceExistsError("exists"), None]
    if failure == "primary_after_acquire":
        a.primary.renew.side_effect = [None, None, RuntimeError("lost")]
        expected = PublisherError
    elif failure == "upload":
        a.gallery.upload_blob.side_effect = [ResourceExistsError("exists"), ResourceModifiedError("lease expired")]
        expected = ResourceModifiedError
    else:
        a.primary.renew.side_effect = [None, None, None, RuntimeError("lost")]
        expected = PublisherError
    with pytest.raises(expected):
        a.store.publish(MANIFEST)
    a.own_lease.release.assert_called_once()
    assert a.gallery.upload_blob.call_count == (1 if failure == "primary_after_acquire" else 2)


def test_manifest_lease_acquisition_conflict_does_not_write(azure_store):
    a = azure_store
    a.gallery.upload_blob.side_effect = ResourceExistsError("exists")
    a.gallery.acquire_lease.side_effect = HttpResponseError("lease already held")
    with pytest.raises(HttpResponseError):
        a.store.publish(MANIFEST)
    assert a.gallery.upload_blob.call_count == 1
    a.own_lease.release.assert_not_called()


def test_manifest_release_failure_does_not_hide_original_write_error(azure_store):
    a = azure_store
    a.gallery.upload_blob.side_effect = [ResourceExistsError("exists"), ResourceModifiedError("expired")]
    a.own_lease.release.side_effect = HttpResponseError("release failed")
    with pytest.raises(ResourceModifiedError):
        a.store.publish(MANIFEST)


@pytest.mark.parametrize("data", [None, {}, {**MANIFEST, "version": True},
    {**MANIFEST, "images": "not-a-list"}, {**MANIFEST, "images": ["gallery.json"]},
    {**MANIFEST, "images": [PATH + "?signed=secret"]}, {**MANIFEST, "url": "secret"},
    {**MANIFEST, "updatedAt": None}])
def test_invalid_manifest_never_writes(azure_store, data):
    with pytest.raises(PublisherError):
        azure_store.store.publish(data)
    azure_store.public.get_blob_client.assert_not_called()
    azure_store.primary.renew.assert_not_called()


@pytest.mark.parametrize("operation", ["control", "chunk", "original", "manifest"])
def test_post_write_primary_loss_is_reported(azure_store, operation):
    a = azure_store
    a.primary.renew.side_effect = ([None, None] if operation == "control" else [None]) + [
        RuntimeError("lost after commit")]
    a.work.get_blob_properties.side_effect = ResourceNotFoundError("missing")
    calls = {
        "control": lambda: a.store.save_control({}),
        "chunk": lambda: a.store.put_chunk(JOB, "copy", 0, []),
        "original": lambda: a.store.upload_original(PATH, b"original", "image/jpeg", DIGEST),
        "manifest": lambda: a.store.publish(MANIFEST),
    }
    with pytest.raises(PublisherError, match="^lease_lost$"):
        calls[operation]()
    assert a.store.lost.is_set()


def test_validate_state_uses_internal_sets_preserving_list_state():
    class CountingList(list):
        contains_calls = 0

        def __contains__(self, value):
            self.contains_calls += 1
            return super().__contains__(value)

    state = initial_state()
    paths = [photo_path("spotify", str(index), "version", "jpg") for index in range(2000)]
    state["managed"] = CountingList(paths + [paths[0]])
    state["pending_cleanup"] = CountingList(paths[::2])
    state["libraries"] = {"spotify": {"items": {
        str(index): {"version": "version", "path": path} for index, path in enumerate(paths)}}}
    assert validate_state(state) is state
    assert state["managed"].contains_calls == 0
    assert state["pending_cleanup"].contains_calls == 0
    assert len(state["managed"]) == 2001
    state["pending_cleanup"].append(photo_path("spotify", "unknown", "version", "jpg"))
    with pytest.raises(PublisherError, match="invalid_private_state"):
        validate_state(state)


@pytest.mark.parametrize("ext,mime", [("jpg", "image/jpeg"), ("jpeg", "image/jpeg"),
    ("png", "image/png"), ("gif", "image/gif"), ("webp", "image/webp"), ("avif", "image/avif")])
def test_all_and_only_existing_image_types_upload(azure_store, ext, mime):
    path = photo_path("spotify", "id", "tag", ext)
    azure_store.store.upload_original(path, b"original", mime, DIGEST)
    azure_store.public.get_blob_client.assert_called_with(path)
    assert azure_store.original.upload_blob.call_args.kwargs["content_settings"].content_type == mime


@pytest.mark.parametrize("content", [bytearray(b"original"), memoryview(b"original"),
                                       memoryview(b"original").cast("I")])
def test_original_buffer_snapshot_is_verified_and_uploaded_as_bytes(azure_store, content):
    azure_store.store.upload_original(PATH, content, "image/jpeg", DIGEST)
    actual = azure_store.original.upload_blob.call_args.args[0]
    assert type(actual) is bytes
    assert actual == b"original"


def test_original_typed_memoryview_byte_limit_not_element_limit(azure_store, monkeypatch):
    monkeypatch.setattr(batch_storage, "MAX_ORIGINAL_BYTES", 7)
    content = memoryview(b"original").cast("I")
    assert len(content) == 2
    with pytest.raises(PublisherError, match="invalid_original_digest"):
        azure_store.store.upload_original(PATH, content, "image/jpeg", DIGEST)
    azure_store.original.upload_blob.assert_not_called()


def test_control_loss_during_head_fences_create(azure_store):
    a = azure_store

    def lost_head():
        a.store.lost.set()
        raise ResourceNotFoundError("missing")

    a.work.get_blob_properties.side_effect = lost_head
    with pytest.raises(PublisherError, match="lease_lost"):
        a.store.save_control({})
    a.work.upload_blob.assert_not_called()


@pytest.mark.parametrize("operation", ["control", "chunk"])
def test_etag_changed_during_bounded_read_is_not_accepted_or_retried(azure_store, operation):
    a = azure_store
    if operation == "control":
        blob = a.work
        invoke = a.store.load_control
    else:
        ref = chunk_ref()
        blob = a.private.get_blob_client(ref["name"])
        invoke = lambda: a.store.read_chunk(ref)
    configure_read(blob, b"{}")
    blob.download_blob.side_effect = ResourceModifiedError("etag changed")
    with pytest.raises(ResourceModifiedError):
        invoke()
    blob.download_blob.assert_called_once()
    blob.upload_blob.assert_not_called()


@pytest.mark.parametrize("operation", ["control", "chunk", "original", "manifest", "inventory"])
def test_unrelated_azure_service_errors_propagate_without_fallback_writes(azure_store, operation):
    a = azure_store
    error = HttpResponseError("service unavailable")
    if operation == "control":
        a.work.get_blob_properties.side_effect = error
        invoke = a.store.load_control
        blob = a.work
    elif operation == "chunk":
        name = f"jobs/{JOB}/intent/000000.json"
        blob = a.private.get_blob_client(name)
        blob.upload_blob.side_effect = error
        invoke = lambda: a.store.put_chunk(JOB, "intent", 0, [])
    elif operation == "original":
        blob = a.original
        blob.upload_blob.side_effect = error
        invoke = lambda: a.store.upload_original(PATH, b"original", "image/jpeg", DIGEST)
    elif operation == "manifest":
        blob = a.gallery
        blob.upload_blob.side_effect = error
        invoke = lambda: a.store.publish(MANIFEST)
    else:
        blob = None
        a.public.list_blobs.side_effect = error
        invoke = lambda: a.store.public_inventory("spotify")
    with pytest.raises(HttpResponseError):
        invoke()
    if blob:
        assert blob.upload_blob.call_count <= 1
    a.gallery.acquire_lease.assert_not_called()


@pytest.mark.parametrize("operation", ["control", "chunk", "inspect"])
def test_loss_during_read_does_not_return_checkpoint_or_metadata(azure_store, operation):
    a = azure_store
    if operation == "control":
        blob = a.work
        configure_read(blob, b"{}")
        invoke = a.store.load_control
    elif operation == "chunk":
        ref = chunk_ref()
        blob = a.private.get_blob_client(ref["name"])
        configure_read(blob, b"{}")
        invoke = lambda: a.store.read_chunk(ref)
    else:
        blob = a.original
        invoke = lambda: a.store.inspect_original(PATH)

    if operation == "inspect":
        def lost_read():
            a.store.lost.set()
            return {"size": 8, "metadata": {}}
        blob.get_blob_properties.side_effect = lost_read
    else:
        def lost_read():
            a.store.lost.set()
            return b"{}"
        blob.download_blob.return_value.readall.side_effect = lost_read
    with pytest.raises(PublisherError, match="lease_lost"):
        invoke()


def test_repeated_publication_is_idempotent_under_new_manifest_lease(azure_store):
    a = azure_store
    a.store.publish(MANIFEST)
    first = a.gallery.upload_blob.call_args.args[0]
    a.gallery.upload_blob.side_effect = [ResourceExistsError("exists"), None]
    a.store.publish(MANIFEST)
    assert a.gallery.upload_blob.call_args.args[0] == first
    assert a.gallery.upload_blob.call_args.kwargs["lease"] is a.own_lease
    a.own_lease.release.assert_called_once()


def test_validate_state_transaction_also_uses_set_membership():
    class NoLinearMembership(list):
        def __contains__(self, value):
            raise AssertionError("Linear managed lookup must not be used")

    from config import manifest
    state = initial_state()
    state["managed"] = NoLinearMembership([PATH])
    libs = {"spotify": {"items": {"id": {"version": "v", "path": PATH}}}}
    state["transaction"] = {"libraries": libs, "manifest": manifest(libs)}
    assert validate_state(state) is state
    state["managed"] = NoLinearMembership([])
    with pytest.raises(PublisherError, match="invalid_private_state"):
        validate_state(state)
