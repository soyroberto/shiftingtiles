"""Personal account public client. Serialized tokens exist only in private state."""
import msal
from config import PublisherError

AUTHORITY = "https://login.microsoftonline.com/consumers"
CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
# Cache realm comes from the token endpoint, not necessarily ID-token tid.
CONSUMER_REALMS = frozenset({"consumers", CONSUMER_TENANT})
SCOPES = ["Files.Read"]  # MSAL adds its reserved OpenID/offline_access scopes itself.


def make_app(client_id, cache):
    if not client_id:
        raise PublisherError("missing_ONEDRIVE_CLIENT_ID")
    return msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache,
                                        enable_pii_log=False)


def single_account(app, owner=None):
    accounts = app.get_accounts()
    if len(accounts) != 1:
        raise PublisherError("single_owner_account_required")
    account = accounts[0]
    ident = account.get("home_account_id")
    realm = account.get("realm")
    if not ident or not isinstance(realm, str) or realm.lower() not in CONSUMER_REALMS:
        raise PublisherError("personal_account_required")
    if owner is not None and ident != owner:
        raise PublisherError("owner_mismatch")
    return account


def silent_token(store, state, client_id):
    if not state["connected"] or not state["cache"]:
        raise PublisherError("disconnected")
    if state["client_id"] != client_id:
        raise PublisherError("client_id_changed_reconnect_required")
    cache = msal.SerializableTokenCache()
    try:
        cache.deserialize(state["cache"])
    except (ValueError, TypeError):
        raise PublisherError("invalid_msal_cache") from None
    try:
        app = make_app(client_id, cache)
        account = single_account(app, state["owner"])
        result = app.acquire_token_silent(SCOPES, account=account)
        single_account(app, state["owner"])
        if not result or "access_token" not in result:
            raise PublisherError("reauthorization_required")
        return result["access_token"]
    finally:
        # Persist even on token errors; token refresh may have rotated the refresh token.
        # This occurs before Graph access, so all Graph error paths retain renewed cache.
        state["cache"] = cache.serialize()
        store.save(state)


def connect(store, state, client_id, display=print):
    # Fresh ephemeral cache avoids accumulating a second account. Only validated owner
    # is persisted; a failed/mismatched login leaves the existing durable cache unchanged.
    cache = msal.SerializableTokenCache()
    app = make_app(client_id, cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise PublisherError("device_flow_start_failed")
    # This one-time setup instruction is intentionally shown, never application-logged.
    # Do not capture the terminal/transcript while connecting; no token/cache is printed.
    display(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if not result or "access_token" not in result:
        raise PublisherError("device_flow_failed")
    account = single_account(app, state["owner"])
    claims = result.get("id_token_claims")
    tenant = claims.get("tid") if isinstance(claims, dict) else None
    if not isinstance(tenant, str) or tenant.lower() != CONSUMER_TENANT:
        raise PublisherError("personal_account_required")
    state["owner"] = account["home_account_id"]
    state["connected"] = True
    state["client_id"] = client_id
    state["cache"] = cache.serialize()
    store.save(state)


def disconnect(store, state):
    state["connected"] = False
    state["cache"] = None
    state["client_id"] = None
    # Owner remains pinned; last-good libraries/publication journal are retained.
    store.save(state)
