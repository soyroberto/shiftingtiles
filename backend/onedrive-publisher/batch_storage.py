"""Bounded private checkpoints and create-only copies under the primary state lease.

The engine owns intent/ledger validation. This adapter never infers ownership from
public presence, creates containers, prunes checkpoints, or changes retention.
"""
import hashlib
import json
import re

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContentSettings

from config import LIBRARY_ID, MANAGED_PATH, TYPES, PublisherError
from storage import BlobStore

CONTROL_BLOB = "publisher-work.json"
MAX_CONTROL_BYTES = 256 * 1024
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_ORIGINAL_BYTES = 50 * 1024 * 1024
MAX_INVENTORY_PATHS = 100000
_JOB = re.compile(r"[a-f0-9]{32}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_CHUNK = re.compile(r"jobs/[a-f0-9]{32}/(?:scan|copy|intent|snapshot)/[0-9]{6}\.json\Z")
_KINDS = frozenset(("scan", "copy", "intent", "snapshot"))


def _field(value, key, default=None):
    """Azure properties are mapping-like; allow attribute-backed SDK properties too."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _json_bytes(value, limit, code):
    try:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True,
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise PublisherError(code) from None
    if len(raw) > limit:
        raise PublisherError(code)
    return raw


def _json_value(raw, code):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError

    try:
        return json.loads(raw, object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise PublisherError(code) from None


class BatchStore(BlobStore):
    def _check_lease(self):
        # Inventory checks this without issuing one renewal per photo.
        if self.lease is None or self.lost.is_set():
            raise PublisherError("lease_lost")

    @staticmethod
    def _read_bounded(blob, limit, code):
        props = blob.get_blob_properties()
        size = _field(props, "size")
        etag = _field(props, "etag")
        if type(size) is not int or size < 0 or size > limit or not isinstance(etag, str) or not etag:
            raise PublisherError(code)
        # HEAD plus an ETag-fenced range prevents an unbounded read after a race.
        raw = blob.download_blob(length=limit + 1, etag=etag,
                                 match_condition=MatchConditions.IfNotModified).readall()
        if not isinstance(raw, bytes) or len(raw) > limit or len(raw) != size:
            raise PublisherError(code)
        return raw

    def load_control(self):
        self.guard()
        try:
            raw = self._read_bounded(self.private.get_blob_client(CONTROL_BLOB),
                                     MAX_CONTROL_BYTES, "invalid_work_control")
        except ResourceNotFoundError:
            self._check_lease()
            return None
        value = _json_value(raw, "invalid_work_control")
        if not isinstance(value, dict):
            raise PublisherError("invalid_work_control")
        self._check_lease()
        return value

    def save_control(self, data):
        if not isinstance(data, dict):
            raise PublisherError("invalid_work_control")
        raw = _json_bytes(data, MAX_CONTROL_BYTES, "invalid_work_control")
        self.guard()
        blob = self.private.get_blob_client(CONTROL_BLOB)
        settings = ContentSettings(content_type="application/json")
        try:
            props = blob.get_blob_properties()
        except ResourceNotFoundError:
            # No upsert: another writer creating it is a concurrency failure.
            self.guard()
            blob.upload_blob(raw, overwrite=False, content_settings=settings)
        else:
            etag = _field(props, "etag")
            if not isinstance(etag, str) or not etag:
                raise PublisherError("invalid_work_control")
            self.guard()
            blob.upload_blob(raw, overwrite=True, etag=etag,
                             match_condition=MatchConditions.IfNotModified,
                             content_settings=settings)
        self.guard()

    @staticmethod
    def _chunk_ref(ref):
        if (not isinstance(ref, dict) or set(ref) != {"name", "sha256"}
                or not isinstance(ref["name"], str) or not _CHUNK.fullmatch(ref["name"])
                or not isinstance(ref["sha256"], str) or not _DIGEST.fullmatch(ref["sha256"])):
            raise PublisherError("invalid_chunk_ref")
        return ref["name"], ref["sha256"]

    def put_chunk(self, job_id, kind, index, payload):
        if (not isinstance(job_id, str) or not _JOB.fullmatch(job_id)
                or not isinstance(kind, str) or kind not in _KINDS
                or type(index) is not int or not 0 <= index <= 999999
                or not isinstance(payload, (dict, list))):
            raise PublisherError("invalid_chunk")
        raw = _json_bytes(payload, MAX_CHUNK_BYTES, "invalid_chunk")
        ref = {"name": f"jobs/{job_id}/{kind}/{index:06d}.json",
               "sha256": hashlib.sha256(raw).hexdigest()}
        self.guard()
        blob = self.private.get_blob_client(ref["name"])
        try:
            blob.upload_blob(raw, overwrite=False,
                             content_settings=ContentSettings(content_type="application/json"))
        except ResourceExistsError:
            self.guard()
            existing = self._read_bounded(blob, MAX_CHUNK_BYTES, "invalid_chunk")
            if hashlib.sha256(existing).hexdigest() != ref["sha256"]:
                raise PublisherError("private_chunk_collision")
        self.guard()
        return ref

    def read_chunk(self, ref):
        name, digest = self._chunk_ref(ref)
        self.guard()
        raw = self._read_bounded(self.private.get_blob_client(name),
                                 MAX_CHUNK_BYTES, "invalid_chunk")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise PublisherError("private_chunk_digest_mismatch")
        value = _json_value(raw, "invalid_chunk")
        if not isinstance(value, (dict, list)):
            raise PublisherError("invalid_chunk")
        self._check_lease()
        return value

    @staticmethod
    def _original_info(props):
        size = _field(props, "size")
        if type(size) is not int or size < 0:
            raise PublisherError("invalid_public_metadata")
        metadata = _field(props, "metadata", {}) or {}
        if not isinstance(metadata, dict):
            raise PublisherError("invalid_public_metadata")
        digest = metadata.get("onedrive_sha256")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            digest = None  # Absence/malformed metadata is never proof of ownership.
        return {"bytes": size, "sha256": digest}

    def public_inventory(self, library_id):
        if not isinstance(library_id, str) or not LIBRARY_ID.fullmatch(library_id):
            raise PublisherError("unsafe_library_id")
        prefix = f"photos/onedrive/{library_id}/"
        self.guard()
        result = {}
        listing = self.public.list_blobs(name_starts_with=prefix, include=["metadata"])
        for page in listing.by_page():
            self._check_lease()
            for props in page:
                self._check_lease()
                name = _field(props, "name")
                if not isinstance(name, str) or not name.startswith(prefix) or not MANAGED_PATH.fullmatch(name):
                    continue  # Foreign blobs are not candidates for adoption or deletion.
                if name in result:
                    raise PublisherError("invalid_public_inventory")
                if len(result) >= MAX_INVENTORY_PATHS:
                    raise PublisherError("public_inventory_limit")
                result[name] = self._original_info(props)
        self._check_lease()
        return result

    def inspect_original(self, path):
        self.safe_path(path)
        self.guard()
        try:
            props = self.public.get_blob_client(path).get_blob_properties()
        except ResourceNotFoundError:
            self._check_lease()
            return None
        result = self._original_info(props)
        self._check_lease()
        return result

    def upload_original(self, path, content, mime, sha256):
        self.safe_path(path)
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise PublisherError("invalid_original_digest")
        size = content.nbytes if isinstance(content, memoryview) else len(content)
        if size > MAX_ORIGINAL_BYTES:
            raise PublisherError("invalid_original_digest")
        # Hash the immutable bytes actually uploaded, even for mutable/typed buffers.
        content = bytes(content)
        if (not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256)
                or hashlib.sha256(content).hexdigest() != sha256):
            raise PublisherError("invalid_original_digest")
        if mime != TYPES[path.rsplit(".", 1)[1]]:
            raise PublisherError("invalid_original_mime")
        self.guard()
        blob = self.public.get_blob_client(path)
        try:
            blob.upload_blob(content, overwrite=False, metadata={"onedrive_sha256": sha256},
                             content_settings=ContentSettings(content_type=mime,
                                                              cache_control="public, max-age=3600"))
        except ResourceExistsError:
            self.guard()
            try:
                info = self._original_info(blob.get_blob_properties())
            except PublisherError:
                raise PublisherError("foreign_copy_collision") from None
            if info != {"bytes": len(content), "sha256": sha256}:
                raise PublisherError("foreign_copy_collision")
        self.guard()

    def publish(self, data):
        if (not isinstance(data, dict) or set(data) != {"version", "images", "updatedAt"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["images"], list) or not isinstance(data["updatedAt"], str)):
            raise PublisherError("invalid_manifest")
        for path in data["images"]:
            self.safe_path(path)
        try:
            raw = json.dumps(data, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise PublisherError("invalid_manifest") from None
        settings = ContentSettings(content_type="application/json", cache_control="no-store")
        self.guard()
        blob = self.public.get_blob_client("gallery.json")
        try:
            # Creation publishes the final manifest directly, never an empty placeholder.
            blob.upload_blob(raw, overwrite=False, content_settings=settings)
        except ResourceExistsError:
            self.guard()
            manifest_lease = blob.acquire_lease(lease_duration=60)
            try:
                self.guard()
                blob.upload_blob(raw, overwrite=True, lease=manifest_lease,
                                 content_settings=settings)
                self.guard()
            finally:
                try:
                    manifest_lease.release()
                except Exception:
                    pass  # The finite lease expires; do not hide the original failure.
        else:
            self.guard()
