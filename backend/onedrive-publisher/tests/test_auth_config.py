import copy
import base64
import json
from unittest.mock import Mock
import pytest

import auth
import admin
from config import Config, PublisherError, libraries, photo_path
from storage import BlobStore, initial_state, validate_state


class Cache:
    def __init__(self):
        self.value = "cached"

    def deserialize(self, value):
        self.value = value

    def serialize(self):
        return self.value


def account(ident="owner", realm=auth.CONSUMER_TENANT):
    return {"home_account_id": ident, "realm": realm}


def ready(store):
    store.durable.update(connected=True, owner="owner", cache="old", client_id="client")
    return store.load()


def test_renewed_cache_persisted_even_if_auth_raises(monkeypatch, store):
    cache = Cache()
    app = Mock()
    app.get_accounts.return_value = [account()]
    def acquire(*args, **kwargs):
        cache.value = "renewed-sensitive-cache"
        raise PublisherError("auth_problem")
    app.acquire_token_silent.side_effect = acquire
    monkeypatch.setattr(auth.msal, "SerializableTokenCache", lambda: cache)
    monkeypatch.setattr(auth, "make_app", lambda *_: app)
    with pytest.raises(PublisherError, match="auth_problem"):
        auth.silent_token(store, ready(store), "client")
    assert store.durable["cache"] == "renewed-sensitive-cache"


def test_cache_persisted_before_graph_failure(monkeypatch, store):
    from sync import sync_locked
    from config import Library
    cache = Cache()
    app = Mock()
    app.get_accounts.return_value = [account()]
    def acquire(*args, **kwargs):
        cache.value = "rotated"
        return {"access_token": "never-public"}
    app.acquire_token_silent.side_effect = acquire
    monkeypatch.setattr(auth.msal, "SerializableTokenCache", lambda: cache)
    monkeypatch.setattr(auth, "make_app", lambda *_: app)
    graph = Mock()
    graph.scan.side_effect = PublisherError("http_401")
    result = sync_locked(store, ready(store), Config(True, [Library("spotify", "Musica/Spotify")], "client"),
                         lambda _: graph)
    assert result["failed"] == 1 and store.durable["cache"] == "rotated"
    assert store.public_manifest is None


@pytest.mark.parametrize("accounts,code", [([], "single_owner"), ([account(), account("other")], "single_owner"),
    ([account("other")], "owner_mismatch"), ([account(realm="organization")], "personal_account")])
def test_single_owner_rejection(accounts, code):
    app = Mock()
    app.get_accounts.return_value = accounts
    with pytest.raises(PublisherError, match=code):
        auth.single_account(app, "owner")


def test_connect_rejects_owner_change_without_overwriting_old_cache(monkeypatch, store):
    state = ready(store)
    before = copy.deepcopy(state)
    app = Mock()
    app.initiate_device_flow.return_value = {"user_code": "ONE-TIME", "message": "setup instruction"}
    app.acquire_token_by_device_flow.return_value = {"access_token": "not-real"}
    app.get_accounts.return_value = [account("other")]
    monkeypatch.setattr(auth, "make_app", lambda *_: app)
    with pytest.raises(PublisherError, match="owner_mismatch"):
        auth.connect(store, state, "client", display=Mock())
    assert store.durable == before


def synthetic_personal_cache():
    """Actual MSAL cache machinery with synthetic data; no network or credentials."""
    cache = auth.msal.SerializableTokenCache()
    client_info = base64.urlsafe_b64encode(json.dumps({
        "uid": "offline-test-user", "utid": auth.CONSUMER_TENANT
    }).encode()).decode().rstrip("=")
    cache.add({
        "client_id": "offline-test-client", "scope": ["Files.Read"],
        "environment": "login.microsoftonline.com",
        "token_endpoint": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "response": {
            "access_token": "offline-test-not-a-credential", "expires_in": 3600,
            "client_info": client_info,
            "id_token_claims": {"tid": auth.CONSUMER_TENANT, "sub": "offline-test-sub"}
        }
    })
    return cache


def test_connect_accepts_actual_msal_consumers_cache_and_pins_owner(monkeypatch, store):
    cache = synthetic_personal_cache()
    accounts = cache.find(cache.CredentialType.ACCOUNT)
    assert accounts[0]["realm"] == "consumers"
    app = Mock()
    app.get_accounts.return_value = accounts
    app.initiate_device_flow.return_value = {"user_code": "OFFLINE", "message": "synthetic"}
    app.acquire_token_by_device_flow.return_value = {
        "access_token": "offline-test-not-a-credential",
        "id_token_claims": {"tid": auth.CONSUMER_TENANT}
    }
    monkeypatch.setattr(auth.msal, "SerializableTokenCache", lambda: cache)
    monkeypatch.setattr(auth, "make_app", lambda *_: app)
    auth.connect(store, initial_state(), "offline-test-client", display=Mock())
    assert store.durable["connected"]
    assert store.durable["owner"] == accounts[0]["home_account_id"]
    assert store.durable["cache"] == cache.serialize()
    assert store.public_manifest is None


@pytest.mark.parametrize("claims", [None, {}, {"tid": None}, {"tid": "organization"}])
def test_consumers_alias_cannot_bypass_personal_tenant_claims(monkeypatch, store, claims):
    state = initial_state()
    before = copy.deepcopy(store.durable)
    app = Mock()
    app.get_accounts.return_value = [account(realm="consumers")]
    app.initiate_device_flow.return_value = {"user_code": "OFFLINE", "message": "synthetic"}
    app.acquire_token_by_device_flow.return_value = {
        "access_token": "offline-test-not-a-credential", "id_token_claims": claims
    }
    monkeypatch.setattr(auth, "make_app", lambda *_: app)
    with pytest.raises(PublisherError, match="personal_account_required"):
        auth.connect(store, state, "client", display=Mock())
    assert store.durable == before


def test_silent_acquisition_accepts_consumers_realm_and_preserves_pinned_owner(monkeypatch, store):
    cache = synthetic_personal_cache()
    accounts = cache.find(cache.CredentialType.ACCOUNT)
    app = Mock()
    app.get_accounts.return_value = accounts
    app.acquire_token_silent.return_value = {"access_token": "offline-test-not-a-credential"}
    state = initial_state()
    state.update(connected=True, owner=accounts[0]["home_account_id"],
                 cache=cache.serialize(), client_id="offline-test-client")
    store.save(state)
    monkeypatch.setattr(auth, "make_app", lambda *_: app)
    assert auth.silent_token(store, state, "offline-test-client") == "offline-test-not-a-credential"
    app.acquire_token_silent.assert_called_once_with(auth.SCOPES, account=accounts[0])
    assert store.durable["owner"] == accounts[0]["home_account_id"]


@pytest.mark.parametrize("realm", [auth.CONSUMER_TENANT, "consumers", "CONSUMERS"])
def test_supported_personal_cache_realms(realm):
    app = Mock()
    app.get_accounts.return_value = [account(realm=realm)]
    assert auth.single_account(app, "owner")["home_account_id"] == "owner"


@pytest.mark.parametrize("realm", [None, "", "common", "organizations"])
def test_other_cache_realms_remain_rejected(realm):
    app = Mock()
    app.get_accounts.return_value = [account(realm=realm)]
    with pytest.raises(PublisherError, match="personal_account_required"):
        auth.single_account(app, "owner")


def test_disconnect_clears_private_active_cache_retains_owner_and_lists(store):
    state = ready(store)
    auth.disconnect(store, state)
    assert store.durable["owner"] == "owner" and store.durable["cache"] is None
    assert not store.durable["connected"]


def test_clear_confirmation_required_before_storage_io(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(admin, "BlobStore", factory)
    with pytest.raises(SystemExit) as stopped:
        admin.main(["clear-published"])
    assert stopped.value.code == 2
    factory.assert_not_called()


@pytest.mark.parametrize("raw", ['{}', '[{"id":"../bad","path":"Photos"}]',
    '[{"id":"safe","path":"../Photos"}]', '[{"id":"safe","path":"Photos//Child"}]',
    '[{"id":"safe","path":"/Photos"}]', '[{"id":"safe","path":"Photos","recursive":"false"}]',
    '[{"id":"safe","path":"Photos"},{"id":"safe","path":"Other"}]'])
def test_invalid_library_config_fails_closed(raw):
    with pytest.raises(PublisherError, match="invalid_ONEDRIVE_LIBRARIES"):
        libraries(raw)


def test_defaults_are_single_nonrecursive_spotify_disabled_globally(monkeypatch):
    for env in ("ONEDRIVE_SYNC_ENABLED", "ONEDRIVE_CLIENT_ID", "ONEDRIVE_LIBRARIES"):
        monkeypatch.delenv(env, raising=False)
    config = Config.from_env()
    assert not config.enabled and len(config.libraries) == 1
    assert config.libraries[0].id == "spotify" and config.libraries[0].path == "Musica/Spotify"
    assert not config.libraries[0].recursive


@pytest.mark.parametrize("path", ["f1.jpeg", "../f1.jpeg", "photos/onedrive/../../x.jpg", "photos/other/x.jpg",
                                  "photos/onedrive/safe/source-name.jpg"])
def test_storage_operations_reject_foreign_or_traversal_paths(path):
    with pytest.raises(PublisherError, match="foreign_blob_path"):
        BlobStore.safe_path(path)


def test_state_rejects_public_list_without_durable_ownership():
    state = initial_state()
    path = photo_path("safe", "id", "tag", "jpg")
    state["libraries"]["safe"] = {"items": {"id": {"path": path, "version": "tag"}}}
    with pytest.raises(PublisherError, match="invalid_private_state"):
        validate_state(state)


def test_importing_disabled_timer_has_no_remote_io(monkeypatch):
    import function_app
    factory = Mock()
    monkeypatch.setattr(function_app, "BlobStore", factory)
    monkeypatch.setenv("ONEDRIVE_SYNC_ENABLED", "false")
    function_app.onedrive_publisher(Mock())
    factory.assert_not_called()
