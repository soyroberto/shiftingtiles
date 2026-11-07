"""Offline smoke tests using unittest and locally available Azure/MSAL SDKs.

The full pytest suite adds parameterized coverage. No network is allowed here either.
"""
import copy
import socket
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth
from config import Config, Library, PublisherError, content_version, libraries, photo_path
from graph import GraphClient, GRAPH
from storage import BlobStore
from sync import sync_locked, cleanup, clear_published, recover_publication
from helpers import FakeStore

JPEG = b"\xff\xd8\xffEXIF GPS original bytes"


def row(ident="new", tag="new"):
    return {"id": ident, "name": "secret title.jpg", "cTag": tag, "eTag": tag,
            "size": len(JPEG), "file": {"mimeType": "image/jpeg"},
            "@microsoft.graph.downloadUrl": "https://download.example.test/secret"}


def response(data=None, status=200, headers=None, content=JPEG):
    r = Mock(status_code=status, headers=headers or {})
    r.json.return_value = data
    r.iter_content.return_value = iter([content])
    return r


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("No remote IO"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.network2 = patch.object(socket, "create_connection", side_effect=AssertionError("No remote IO"))
        self.network2.start()
        self.addCleanup(self.network2.stop)
        self.store = FakeStore()
        self.store.durable.update(owner="owner", cache="{}", client_id="client", connected=True)
        self.config = Config(True, [Library("spotify", "Musica/Spotify")], "client")
        self.token_patch = patch("sync.silent_token", return_value="mock-token")
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.seed("spotify")

    def seed(self, lib):
        source = row(lib + "old", "old")
        version = content_version(source)
        path = photo_path(lib, source["id"], version, "jpg")
        self.store.durable["managed"].append(path)
        self.store.durable["libraries"][lib] = {"items": {source["id"]: {"path": path, "version": version}}}
        self.store.blobs[path] = JPEG
        self.store.public_manifest = {"version": 1, "images": sorted(self.store.blobs), "updatedAt": "2026-01-01T00:00:00Z"}
        return path

    def run_sync(self, rows=None, fail=None):
        graph = Mock()
        graph.scan.return_value = ([(r, "jpg") for r in (rows or [])], {})
        if fail:
            graph.scan.side_effect = fail
        graph.download.return_value = JPEG
        result = sync_locked(self.store, self.store.load(), self.config, lambda token: graph)
        graph.close.assert_called_once()
        return result

    def test_upload_publication_deletion_order_and_original_bytes(self):
        old = next(iter(self.store.blobs))
        self.run_sync([row()])
        events = [e[0] for e in self.store.events]
        self.assertLess(events.index("upload"), events.index("publish"))
        self.assertLess(events.index("publish"), events.index("delete"))
        self.assertNotIn(old, self.store.blobs)
        self.assertEqual(list(self.store.blobs.values()), [JPEG])
        self.assertNotIn("secret title", str(self.store.public_manifest))

    def test_empty_success_is_fallback(self):
        self.run_sync()
        self.assertEqual(self.store.public_manifest["images"], [])
        self.assertEqual(self.store.blobs, {})

    def test_errors_retain_all_last_good(self):
        before = copy.deepcopy(self.store.public_manifest)
        self.run_sync(fail=PublisherError("partial_page"))
        self.assertEqual(self.store.public_manifest, before)
        self.assertFalse(any(e[0] == "delete" for e in self.store.events))

    def test_disabled_and_removed_libraries_retain_copies(self):
        retained = self.seed("removed")
        self.run_sync()
        self.assertEqual(self.store.public_manifest["images"], [retained])
        self.assertIn(retained, self.store.blobs)

    def test_disabled_timer_does_not_touch_state(self):
        graph = Mock()
        config = Config(False, self.config.libraries, "client")
        result = sync_locked(self.store, self.store.load(), config, lambda _: graph)
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(self.store.events)
        graph.scan.assert_not_called()

    def test_upload_failure_has_durable_intent_without_manifest_change(self):
        before = copy.deepcopy(self.store.public_manifest)
        self.store.fail_upload = True
        self.run_sync([row()])
        self.assertEqual(self.store.public_manifest, before)
        self.assertEqual(len(self.store.durable["managed"]), 2)
        self.store.fail_upload = False
        self.run_sync()
        self.assertEqual(self.store.durable["managed"], [])

    def test_manifest_crash_recovery_keeps_old_until_commit(self):
        old = next(iter(self.store.blobs))
        self.store.fail_publish = True
        with self.assertRaises(PublisherError):
            self.run_sync([row()])
        self.assertIn(old, self.store.blobs)
        self.assertIsNotNone(self.store.durable["transaction"])
        self.store.fail_publish = False
        state = self.store.load()
        recover_publication(self.store, state)
        cleanup(self.store, state)
        self.assertNotIn(old, self.store.blobs)

    def test_commit_durability_failure_prevents_deletion(self):
        old = next(iter(self.store.blobs))
        self.store.fail_save_at = 3
        with self.assertRaises(PublisherError):
            self.run_sync([row()])
        self.assertIn(old, self.store.blobs)
        self.assertIsNotNone(self.store.durable["transaction"])
        self.assertFalse(any(e[0] == "delete" for e in self.store.events))

    def test_clear_only_managed_copies_and_disconnect(self):
        self.store.blobs["f1.jpg"] = b"bundled"
        clear_published(self.store, self.store.load())
        self.assertEqual(self.store.blobs, {"f1.jpg": b"bundled"})
        self.assertFalse(self.store.durable["connected"])
        self.assertIsNone(self.store.durable["cache"])
        self.assertEqual(self.store.public_manifest["images"], [])

    def test_cleanup_failure_is_durably_retryable(self):
        self.store.fail_delete = True
        self.run_sync()
        self.assertEqual(len(self.store.durable["pending_cleanup"]), 1)
        self.store.fail_delete = False
        self.assertEqual(clear_published(self.store, self.store.load()), 0)

    def test_manually_missing_copy_restored(self):
        source = row("spotifyold", "old")
        self.store.blobs.clear()
        self.run_sync([source])
        self.assertEqual(list(self.store.blobs.values()), [JPEG])

    def graph(self, replies, downloads=None):
        session, download = Mock(), Mock()
        session.get.side_effect = replies
        download.get.side_effect = downloads or []
        return GraphClient("mock-token", session=session, download_session=download, sleep=Mock())

    def test_graph_pagination_and_path_segment_encoding(self):
        g = self.graph([response({"id": "folder", "folder": {}}),
                        response({"value": [row()], "@odata.nextLink": GRAPH + "/me/drive/items/folder/children?page=2"}),
                        response({"value": [row("second")]})])
        result, _ = g.scan(Library("safe", "Fotos/Space #"))
        self.assertEqual(len(result), 2)
        self.assertIn("Fotos/Space%20%23", g.session.get.call_args_list[0].args[0])
        self.assertEqual(g.session.get.call_count, 3)

    def test_partial_graph_page_raises(self):
        g = self.graph([response({"id": "folder", "folder": {}}),
                        response({"value": [row()], "@odata.nextLink": GRAPH + "/me/drive/items/folder/children?page=2"}),
                        response(status=404)])
        with self.assertRaisesRegex(PublisherError, "http_404"):
            g.scan(Library("safe", "Fotos"))

    def test_preauthenticated_download_receives_no_authorization(self):
        source = row()
        g = self.graph([response(source), response(source)], [response()])
        self.assertEqual(g.download(source, "jpg"), JPEG)
        self.assertNotIn("Authorization", g.download_session.get.call_args.kwargs["headers"])
        self.assertFalse(g.download_session.trust_env)

    def test_changed_download_tag_raises(self):
        source = row()
        g = self.graph([response(source), response(row(tag="changed"))], [response()])
        with self.assertRaisesRegex(PublisherError, "download_race_after"):
            g.download(source, "jpg")

    def test_retry_after_honored_and_long_delay_aborts(self):
        g = self.graph([response(status=429, headers={"Retry-After": "3"}), response({"id": "ok"})])
        self.assertEqual(g.item("id"), {"id": "ok"})
        g.sleep.assert_called_once_with(3.0)
        g = self.graph([response(status=429, headers={"Retry-After": "600"})])
        with self.assertRaisesRegex(PublisherError, "retry_budget"):
            g.item("id")

    def test_owner_rejection(self):
        app = Mock()
        app.get_accounts.return_value = [{"home_account_id": "other", "realm": auth.CONSUMER_TENANT}]
        with self.assertRaisesRegex(PublisherError, "owner_mismatch"):
            auth.single_account(app, "owner")

    def test_renewed_cache_saved_on_auth_error(self):
        cache = Mock()
        cache.serialize.return_value = "renewed"
        app = Mock()
        app.get_accounts.return_value = [{"home_account_id": "owner", "realm": auth.CONSUMER_TENANT}]
        app.acquire_token_silent.side_effect = PublisherError("auth_error")
        with patch.object(auth.msal, "SerializableTokenCache", return_value=cache), patch.object(auth, "make_app", return_value=app):
            with self.assertRaisesRegex(PublisherError, "auth_error"):
                auth.silent_token(self.store, self.store.load(), "client")
        self.assertEqual(self.store.durable["cache"], "renewed")

    def test_paths_reject_traversal_and_foreign_root(self):
        for path in ("f1.jpg", "../f1.jpg", "photos/onedrive/safe/source-name.jpg"):
            with self.subTest(path=path), self.assertRaises(PublisherError):
                BlobStore.safe_path(path)
        with self.assertRaises(PublisherError):
            libraries('[{"id":"safe","path":"../Photos"}]')


class StorageOfflineTests(unittest.TestCase):
    def setUp(self):
        import json
        from storage import initial_state
        self.private, self.public, self.blob, self.lease = Mock(), Mock(), Mock(), Mock()
        self.private.get_container_properties.return_value = {"public_access": None}
        self.private.get_blob_client.return_value = self.blob
        self.blob.exists.return_value = True
        self.blob.acquire_lease.return_value = self.lease
        self.blob.download_blob.return_value.readall.return_value = json.dumps(initial_state()).encode()
        service = Mock()
        service.get_container_client.side_effect = [self.private, self.public]
        self.store = BlobStore(Config(False, [], ""), service=service)

    def test_private_container_is_enforced_before_write(self):
        self.private.get_container_properties.return_value = {"public_access": "blob"}
        with self.assertRaisesRegex(PublisherError, "state_container_not_private"):
            with self.store.locked():
                pass
        self.blob.acquire_lease.assert_not_called()
        self.private.create_container.assert_not_called()

    def test_missing_state_with_public_copies_refuses_reset(self):
        self.blob.exists.return_value = False
        self.public.list_blobs.return_value = iter([{"name": "photos/onedrive/old/existing.jpg"}])
        with self.assertRaisesRegex(PublisherError, "state_missing_with_published_copies"):
            with self.store.locked():
                pass
        self.blob.upload_blob.assert_not_called()

    def test_state_read_write_carry_renewable_lease(self):
        with self.store.locked():
            self.store.save(self.store.load())
            self.assertIs(self.blob.download_blob.call_args.kwargs["lease"], self.lease)
            self.assertIs(self.blob.upload_blob.call_args.kwargs["lease"], self.lease)
        self.blob.acquire_lease.assert_called_once_with(lease_duration=60)
        self.lease.release.assert_called_once()
        self.assertGreaterEqual(self.lease.renew.call_count, 2)

    def test_lost_lease_fences_publication(self):
        with self.store.locked():
            self.lease.renew.side_effect = RuntimeError("private diagnostics")
            with self.assertRaisesRegex(PublisherError, "lease_lost"):
                self.store.publish({"version": 1, "images": [], "updatedAt": "2026-01-01T00:00:00Z"})
            self.public.get_blob_client.assert_not_called()

    def test_private_container_is_never_automatically_created(self):
        self.blob.exists.return_value = False
        self.public.list_blobs.return_value = iter([])
        with self.store.locked():
            pass
        self.assertFalse(self.blob.upload_blob.call_args.kwargs["overwrite"])
        self.private.create_container.assert_not_called()
        self.public.create_container.assert_not_called()


if __name__ == "__main__":
    unittest.main()
