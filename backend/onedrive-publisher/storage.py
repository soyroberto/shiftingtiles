"""One private leased state blob is the journal, cache and serialization boundary."""
from contextlib import contextmanager
import json
import threading

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from config import MANAGED_PATH, PublisherError

STATE_BLOB = "publisher-state.json"


def initial_state():
    return {"version": 1, "owner": None, "connected": False, "client_id": None,
            "cache": None, "libraries": {}, "managed": [], "pending_cleanup": [], "transaction": None}


def validate_state(state):
    try:
        if state["version"] != 1 or not isinstance(state["connected"], bool):
            raise ValueError
        if state["owner"] is not None and not isinstance(state["owner"], str):
            raise ValueError
        if state["cache"] is not None and not isinstance(state["cache"], str):
            raise ValueError
        if state["connected"] and (not state["owner"] or not state["cache"] or not state["client_id"]):
            raise ValueError
        for field in ("managed", "pending_cleanup"):
            if not isinstance(state[field], list) or any(not MANAGED_PATH.fullmatch(p) for p in state[field]):
                raise ValueError
        managed = set(state["managed"])
        pending_cleanup = set(state["pending_cleanup"])
        if not pending_cleanup.issubset(managed):
            raise ValueError
        collections = [state["libraries"]]
        # Runtime library views journal separate publications without replacing
        # another library's transaction or its committed gallery.
        transactions = state.get("library_transactions", {})
        if not isinstance(transactions, dict) or len(transactions) > 20:
            raise ValueError
        from config import LIBRARY_ID, manifest
        for ident, tx in transactions.items():
            if (not LIBRARY_ID.fullmatch(ident) or not isinstance(tx, dict) or
                    set(tx) != {"libraries", "manifest"} or set(tx["libraries"]) != {ident}):
                raise ValueError
            m = tx["manifest"]
            if (set(m) != {"version", "images", "updatedAt"} or type(m["version"]) is not int or
                    m["version"] != 1 or not isinstance(m["updatedAt"], str) or
                    m["images"] != manifest(tx["libraries"])["images"]):
                raise ValueError
            collections.append(tx["libraries"])
        if state["transaction"] is not None:
            collections.append(state["transaction"]["libraries"])
            m = state["transaction"]["manifest"]
            if m["version"] != 1 or not isinstance(m["images"], list):
                raise ValueError
            from config import manifest
            if m["images"] != manifest(state["transaction"]["libraries"])["images"]:
                raise ValueError
        for libs in collections:
            if not isinstance(libs, dict):
                raise ValueError
            for ident, lib in libs.items():
                from config import LIBRARY_ID
                if not LIBRARY_ID.fullmatch(ident) or not isinstance(lib["items"], dict):
                    raise ValueError
                for item in lib["items"].values():
                    if not isinstance(item["version"], str) or not MANAGED_PATH.fullmatch(item["path"]):
                        raise ValueError
                    if not item["path"].startswith(f"photos/onedrive/{ident}/") or item["path"] not in managed:
                        raise ValueError
        return state
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PublisherError("invalid_private_state") from None


class BlobStore:
    def __init__(self, config, operator=False, service=None):
        self.credential = None
        if service is None:
            # Runtime: managed identity only. Operator: existing Azure CLI identity only.
            # No secret, workload, browser, broker, or password credential fallback.
            self.credential = DefaultAzureCredential(
                exclude_environment_credential=True,
                exclude_workload_identity_credential=True,
                exclude_managed_identity_credential=operator,
                exclude_cli_credential=not operator,
                exclude_powershell_credential=True,
                exclude_developer_cli_credential=True,
                exclude_shared_token_cache_credential=True,
                exclude_visual_studio_code_credential=True,
                exclude_interactive_browser_credential=True,
                exclude_broker_credential=True)
            service = BlobServiceClient(config.account_url, credential=self.credential,
                                        connection_timeout=5, read_timeout=20, retry_total=2,
                                        logging_enable=False)
        self.service = service
        self.private = service.get_container_client(config.state_container)
        self.public = service.get_container_client("$web")
        self.state_blob = self.private.get_blob_client(STATE_BLOB)
        self.lease = None
        self.lost = threading.Event()
        self.stop = threading.Event()
        self.renew_lock = threading.Lock()

    def close(self):
        self.service.close()
        if self.credential:
            self.credential.close()

    def guard(self):
        if self.lease is None or self.lost.is_set():
            raise PublisherError("lease_lost")
        # Renew immediately before every mutation, in addition to the heartbeat.
        with self.renew_lock:
            try:
                self.lease.renew()
            except Exception:
                self.lost.set()
                raise PublisherError("lease_lost") from None

    def _heartbeat(self):
        while not self.stop.wait(15):
            try:
                self.guard()
            except PublisherError:
                return

    @contextmanager
    def locked(self):
        props = self.private.get_container_properties()  # Must pre-exist; never create containers.
        if props.get("public_access"):
            raise PublisherError("state_container_not_private")
        self.public.get_container_properties()
        if not self.state_blob.exists():
            # Refuse reset over previous public copies: lost state must be restored privately.
            if next(iter(self.public.list_blobs(name_starts_with="photos/onedrive/")), None) is not None:
                raise PublisherError("state_missing_with_published_copies")
            try:
                self.state_blob.upload_blob(json.dumps(initial_state()), overwrite=False,
                                            content_settings=ContentSettings(content_type="application/json"))
            except ResourceExistsError:
                pass  # Another writer initialized; the lease still serializes actual state use.
        self.lease = self.state_blob.acquire_lease(lease_duration=60)
        self.stop.clear()
        self.lost.clear()
        heartbeat = threading.Thread(target=self._heartbeat, daemon=True)
        heartbeat.start()
        try:
            yield self
        finally:
            self.stop.set()
            heartbeat.join(timeout=25)
            try:
                self.lease.release()
            except Exception:
                pass  # Expiring lease is intentionally recoverable after process termination.
            self.lease = None

    def load(self):
        self.guard()
        try:
            raw = self.state_blob.download_blob(lease=self.lease).readall()
            return validate_state(json.loads(raw))
        except (ValueError, UnicodeError):
            raise PublisherError("invalid_private_state") from None

    def save(self, state):
        validate_state(state)
        self.guard()
        self.state_blob.upload_blob(json.dumps(state, separators=(",", ":")), overwrite=True,
                                    lease=self.lease,
                                    content_settings=ContentSettings(content_type="application/json"))

    @staticmethod
    def safe_path(path):
        if not isinstance(path, str) or not MANAGED_PATH.fullmatch(path):
            raise PublisherError("foreign_blob_path")

    def exists(self, path):
        self.safe_path(path)
        self.guard()
        return self.public.get_blob_client(path).exists()

    def upload(self, path, content, mime):
        self.safe_path(path)
        self.guard()
        self.public.get_blob_client(path).upload_blob(
            content, overwrite=True,
            content_settings=ContentSettings(content_type=mime, cache_control="public, max-age=3600"))

    def publish(self, data):
        if set(data) != {"version", "images", "updatedAt"} or data["version"] != 1:
            raise PublisherError("invalid_manifest")
        for path in data["images"]:
            self.safe_path(path)
        self.guard()
        self.public.get_blob_client("gallery.json").upload_blob(
            json.dumps(data, separators=(",", ":")), overwrite=True,
            content_settings=ContentSettings(content_type="application/json", cache_control="no-store"))

    def delete(self, path):
        self.safe_path(path)
        self.guard()
        try:
            self.public.get_blob_client(path).delete_blob(delete_snapshots="include")
        except ResourceNotFoundError:
            pass  # A local manual deletion is already clean; never touch any original.
