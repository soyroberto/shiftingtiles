import json
from unittest.mock import call
import pytest
from config import PublisherError, photo_path
from test_batch_storage import azure_store, configure_read


@pytest.mark.parametrize('name',['publisher-coordinator.json','libraries/instagram/publisher-work.json'])
def test_named_control_load_keeps_bounded_sdk_range(azure_store,name):
    a=azure_store;blob=a.private.get_blob_client(name);configure_read(blob,b'{"version":1}')
    assert a.store.load_control(name=name)=={'version':1}
    a.private.get_blob_client.assert_called_with(name)
    assert blob.download_blob.call_args.kwargs['offset']==0


@pytest.mark.parametrize('name',['../../state','libraries/../publisher-work.json','libraries/Instagram/publisher-work.json','publisher-state.json','https://evil.invalid/'])
def test_unsafe_control_name_fails_before_io(azure_store,name):
    a=azure_store;a.private.reset_mock()
    with pytest.raises(PublisherError,match='unsafe_control_name'):a.store.load_control(name=name)
    a.private.get_blob_client.assert_not_called()


def test_scoped_manifest_keeps_own_prefix(azure_store):
    a=azure_store;path=photo_path('instagram','photo','version','jpg')
    data={'version':1,'images':[path],'updatedAt':'2026-11-09T00:00:00Z'}
    a.store.publish(data,name='photos/onedrive/instagram/gallery.json')
    a.public.get_blob_client.assert_called_with('photos/onedrive/instagram/gallery.json')
    assert json.loads(a.original.upload_blob.call_args.args[0])==data


def test_cross_library_manifest_fails_before_blob_io(azure_store):
    a=azure_store;a.public.reset_mock();path=photo_path('spotify','photo','version','jpg')
    data={'version':1,'images':[path],'updatedAt':'2026-11-09T00:00:00Z'}
    with pytest.raises(PublisherError,match='cross_library_manifest'):
        a.store.publish(data,name='photos/onedrive/instagram/gallery.json')
    a.public.get_blob_client.assert_not_called()
