import json
from unittest.mock import Mock
import pytest
from azure.core.exceptions import ResourceNotFoundError
from config import Config, PublisherError, photo_path
from storage import BlobStore, initial_state


def setup_store(public_access=None, state_exists=True):
    private, public, blob, lease = Mock(), Mock(), Mock(), Mock()
    private.get_container_properties.return_value = {"public_access": public_access}
    private.get_blob_client.return_value = blob
    blob.exists.return_value = state_exists
    blob.acquire_lease.return_value = lease
    blob.download_blob.return_value.readall.return_value = json.dumps(initial_state()).encode()
    service = Mock()
    service.get_container_client.side_effect = [private, public]
    store = BlobStore(Config(False, [], ""), service=service)
    return store, private, public, blob, lease


def test_private_state_container_required_before_lease_or_writes():
    store, private, public, blob, lease = setup_store("blob")
    with pytest.raises(PublisherError, match="state_container_not_private"):
        with store.locked():
            pass
    blob.acquire_lease.assert_not_called()
    private.create_container.assert_not_called()
    public.create_container.assert_not_called()


def test_missing_state_with_public_copies_is_not_reinitialized():
    store, private, public, blob, lease = setup_store(state_exists=False)
    public.list_blobs.return_value = iter([{"name": "photos/onedrive/old/existing.jpg"}])
    with pytest.raises(PublisherError, match="state_missing_with_published_copies"):
        with store.locked():
            pass
    blob.upload_blob.assert_not_called()


def test_lease_applies_to_state_read_write_and_releases():
    store, private, public, blob, lease = setup_store()
    with store.locked():
        store.save(store.load())
        assert blob.download_blob.call_args.kwargs["lease"] is lease
        assert blob.upload_blob.call_args.kwargs["lease"] is lease
    blob.acquire_lease.assert_called_once_with(lease_duration=60)
    lease.release.assert_called_once()
    assert lease.renew.call_count >= 2
    private.create_container.assert_not_called()


def test_renew_failure_fences_public_mutation():
    store, private, public, blob, lease = setup_store()
    with store.locked():
        lease.renew.side_effect = RuntimeError("private diagnostics")
        with pytest.raises(PublisherError, match="lease_lost"):
            store.publish({"version": 1, "images": [], "updatedAt": "2026-01-01T00:00:00Z"})
        public.get_blob_client.assert_not_called()


def test_missing_managed_blob_delete_is_idempotent():
    store, private, public, blob, lease = setup_store()
    path = photo_path("safe", "id", "tag", "jpg")
    public.get_blob_client.return_value.delete_blob.side_effect = ResourceNotFoundError("missing")
    with store.locked():
        store.delete(path)
    public.get_blob_client.assert_called_once_with(path)


def test_initial_state_blob_creation_never_creates_container():
    store, private, public, blob, lease = setup_store(state_exists=False)
    public.list_blobs.return_value = iter([])
    with store.locked():
        pass
    assert blob.upload_blob.call_args.kwargs["overwrite"] is False
    private.create_container.assert_not_called()
    public.create_container.assert_not_called()
