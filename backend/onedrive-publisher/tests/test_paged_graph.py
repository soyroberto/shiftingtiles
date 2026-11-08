import json
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import Mock
from urllib.parse import quote

import pytest
import requests

from config import Library, PublisherError, TYPES
from graph import GRAPH, MAX_BYTES, GraphClient
from paged_graph import PAGE_SELECT, PAGE_SIZE, PagedGraph

JPEG = b"\xff\xd8\xffunaltered original bytes"
ROOT = {"id": "0123456789ABCDEF!4321", "drive_id": "0123456789ABCDEF"}
CHILDREN = GRAPH + "/me/drive/items/" + quote(ROOT["id"], safe="") + "/children"
FIRST = CHILDREN + "?$select=" + PAGE_SELECT + "&$top=200"
SIGNED = "https://download.example.test/private?secret=hidden"


def source(ident="item1", ext="jpg", **changes):
    row = {"id": ident, "name": "private source." + ext, "size": len(JPEG),
           "cTag": "c1", "eTag": "e1", "file": {"mimeType": TYPES.get(ext.lower(), "image/jpeg")},
           "parentReference": {"id": ROOT["id"], "driveId": ROOT["drive_id"]},
           "@microsoft.graph.downloadUrl": SIGNED}
    row.update(changes)
    return row


def minimal(ident="item1"):
    return {"id": ident, "size": len(JPEG), "cTag": "c1", "eTag": "e1",
            "file": {"mimeType": "image/jpeg"}, "ext": "jpg"}


def response(data=None, status=200, headers=None, content=JPEG):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.json.return_value = data
    result.iter_content.return_value = iter([content])
    return result


def client(responses=(), downloads=(), **kwargs):
    session, download_session = Mock(), Mock()
    session.get.side_effect = responses
    download_session.get.side_effect = downloads
    return PagedGraph("secret-token", session=session, download_session=download_session, **kwargs)


def test_root_is_owned_minimal_encoded_and_projected():
    metadata = {"id": ROOT["id"], "folder": {}, "parentReference": {"driveId": ROOT["drive_id"]},
                "name": "private", "@microsoft.graph.downloadUrl": SIGNED}
    reply = response(metadata)
    graph = client([reply])
    assert graph.root(Library("spotify", "Fotos/Space # /á+?")) == ROOT
    url = graph.session.get.call_args.args[0]
    assert url == GRAPH + "/me/drive/root:/Fotos/Space%20%23%20/%C3%A1%2B%3F?$select=id,folder,remoteItem,parentReference"
    assert "downloadUrl" not in url
    reply.close.assert_called_once()


def test_root_without_drive_has_explicit_null():
    graph = client([response({"id": "folder", "folder": {}})])
    assert graph.root(Library("spotify", "Musica/Spotify")) == {"id": "folder", "drive_id": None}


@pytest.mark.parametrize("metadata", [
    {"id": "folder", "folder": {}, "remoteItem": {}},
    {"id": "folder", "file": {}}, {"id": "folder", "folder": None},
])
def test_root_rejects_remote_and_not_owned_folder(metadata):
    graph = client([response(metadata)])
    with pytest.raises(PublisherError, match="^source_not_owned_folder$"):
        graph.root(Library("spotify", "Musica/Spotify"))


@pytest.mark.parametrize("parent", [None, "drive", []])
def test_root_rejects_malformed_parent_reference(parent):
    graph = client([response({"id": "folder", "folder": {}, "parentReference": parent})])
    with pytest.raises(PublisherError, match="invalid_parent_reference"):
        graph.root(Library("spotify", "Musica/Spotify"))


@pytest.mark.parametrize("ident", [None, True, 2, "", ".", "..", "a/b", "a%2Fb", "a?b", "a#b",
                                  "a:b", "a\\b", "a\nb", "á", "a" * 513])
def test_root_checked_rejects_unsafe_ids(ident):
    with pytest.raises(PublisherError, match="^invalid_graph_id$"):
        PagedGraph.root_checked(ident)
    with pytest.raises(PublisherError, match="^invalid_graph_id$"):
        PagedGraph.root_checked("safe", ident if ident is not None else "")


def test_root_checked_accepts_personal_and_business_opaque_ids():
    assert PagedGraph.root_checked(ROOT["id"], ROOT["drive_id"]) == ROOT
    assert PagedGraph.root_checked("01ABCDEF-foo_12", "b!drive-safe_2") == {
        "id": "01ABCDEF-foo_12", "drive_id": "b!drive-safe_2"}


@pytest.mark.parametrize("path", ["", "a//b", "a/../b", "a/./b", "/a", "a/", "a\\b", "a\x00b"])
def test_root_invalid_path_fails_before_io(path):
    graph = client()
    with pytest.raises(PublisherError, match="invalid_source_path"):
        graph.root(Library("spotify", path))
    graph.session.get.assert_not_called()


def test_root_recursion_is_explicitly_enabled():
    graph = client([response({"id": ROOT["id"], "folder": {},
                              "parentReference": {"driveId": ROOT["drive_id"]}})])
    assert graph.root(Library("spotify", "Musica/Spotify", recursive=True)) == ROOT
    assert graph.recursive is True


def test_scan_page_sanitizes_every_persistible_field_and_counts_all_entries():
    image = source()
    image["file"]["hashes"] = {"private": "hash"}
    rows = [image, source("heic", "heic"), source("svg", "svg"),
            source("mismatch", "jpeg", file={"mimeType": "image/svg+xml"}),
            {"id": "folder", "folder": {}, "name": "private nested folder"},
            {"id": "remote", "remoteItem": {}, "name": "private remote"},
            {"id": "nonfile", "name": "private nonfile"}, source("text", "txt")]
    reply = response({"value": rows})
    graph = client([reply])
    result = graph.scan_page(ROOT)
    assert result == {"rows": [minimal()], "ids": [row["id"] for row in rows],
                      "next": None, "entries": 8,
                      "skipped": {"heic": 1, "svg": 1, "mime": 1, "folder": 1,
                                  "remote": 1, "nonfile": 1, "other": 1}}
    serialized = json.dumps(result)
    assert all(secret not in serialized for secret in ("private", "downloadUrl", "hidden", "hashes", "name"))
    assert graph.session.get.call_args.args[0] == FIRST
    assert graph.session.get.call_count == 1
    reply.close.assert_called_once()


@pytest.mark.parametrize("ext", ["jpg", "jpeg", "png", "gif", "webp", "avif"])
def test_only_current_six_extensions_are_supported_case_insensitively(ext):
    graph = client([response({"value": [source(ext=ext.upper())]})])
    result = graph.scan_page(ROOT)
    assert result["rows"][0]["ext"] == ext
    assert result["rows"][0]["file"] == {"mimeType": TYPES[ext]}
    assert result["skipped"] == {}


def test_missing_mime_defaults_to_octet_stream_and_etag_only_is_valid():
    graph = client([response({"value": [source(file={}, cTag=None)]})])
    row = graph.scan_page(ROOT)["rows"][0]
    assert row["file"] == {"mimeType": "application/octet-stream"}
    assert row["cTag"] is None and row["eTag"] == "e1"


@pytest.mark.parametrize("encoded", [True, False])
def test_nextlink_personal_id_spelling_and_opaque_query_are_preserved_exactly(encoded):
    # Local inventory did not retain a literal nextLink. These realistic Graph
    # wire forms test the documented endpoint, not a claimed live fixture.
    ident = quote(ROOT["id"], safe="") if encoded else ROOT["id"]
    link = (GRAPH + "/me/drive/items/" + ident + "/children"
            "?$select=id%2Cname%2Cfile&$top=200&$skiptoken=opaque%2B%2F%3D%26x")
    graph = client([response({"value": [source()], "@odata.nextLink": link}),
                    response({"value": [source("item2")]})])
    first = graph.scan_page(ROOT)
    assert first["next"] == link
    second = graph.scan_page(ROOT, first["next"])
    assert second["next"] is None and second["ids"] == ["item2"]
    assert [call.args[0] for call in graph.session.get.call_args_list] == [FIRST, link]


UNSAFE_CURSORS = [
    "https://evil.example/v1.0/me/drive/items/" + ROOT["id"] + "/children",
    CHILDREN.replace("https:", "http:"), CHILDREN.replace("graph.microsoft.com", "graph.microsoft.com.evil.test"),
    CHILDREN.replace("graph.microsoft.com", "user@graph.microsoft.com"),
    CHILDREN.replace("graph.microsoft.com", "graph.microsoft.com:443"),
    CHILDREN.replace("graph.microsoft.com", "GRAPH.MICROSOFT.COM"),
    CHILDREN.replace("/v1.0/", "/beta/"), CHILDREN.replace("/me/drive/", "/drive/"),
    CHILDREN.replace("/me/drive/", "/drives/" + ROOT["drive_id"] + "/"),
    CHILDREN.replace("/me/drive/", "/users/another/drive/"),
    CHILDREN.replace(quote(ROOT["id"], safe=""), "foreign-root"),
    CHILDREN.replace("/children", "/content"), CHILDREN + "/", CHILDREN + "/../content",
    CHILDREN + "#", CHILDREN + "#secret", CHILDREN + "?x=1\n", CHILDREN + "?x=\t1",
    CHILDREN + "?x=bad value", CHILDREN + "?x=\\evil", CHILDREN + "?x=" + "a" * 16384,
    CHILDREN.replace("/items/", "/items%2F"),
    "//graph.microsoft.com/v1.0/me/drive/items/" + ROOT["id"] + "/children",
    "https://[invalid/v1.0/", "", None, 4,
]


@pytest.mark.parametrize("cursor", [c for c in UNSAFE_CURSORS if c is not None])
def test_untrusted_cursor_rejected_before_authenticated_io(cursor):
    graph = client()
    with pytest.raises(PublisherError, match="^unsafe_graph_cursor$"):
        graph.scan_page(ROOT, cursor)
    graph.session.get.assert_not_called()


@pytest.mark.parametrize("link", UNSAFE_CURSORS)
def test_foreign_or_malformed_nextlink_rejects_entire_page_without_fetching(link):
    if link is None:  # Null means scan completion, not a malformed continuation.
        return
    graph = client([response({"value": [source()], "@odata.nextLink": link})])
    with pytest.raises(PublisherError, match="unsafe_graph_cursor|invalid_nextlink"):
        graph.scan_page(ROOT)
    assert graph.session.get.call_count == 1


def test_self_repeating_cursor_is_rejected():
    graph = client([response({"value": [], "@odata.nextLink": FIRST})])
    with pytest.raises(PublisherError, match="^pagination_bound$"):
        graph.scan_page(ROOT)


@pytest.mark.parametrize("root", [None, {}, {"id": "safe"}, {"id": "a/b", "drive_id": None},
                                  {**ROOT, "name": "private"}])
def test_invalid_persisted_root_fails_before_io(root):
    graph = client()
    with pytest.raises(PublisherError, match="invalid_graph_root|invalid_graph_id"):
        graph.scan_page(root)
    graph.session.get.assert_not_called()


@pytest.mark.parametrize("data", [{}, {"value": None}, {"value": {}}, {"value": "bad"}])
def test_incomplete_page_fails_closed(data):
    graph = client([response(data)])
    with pytest.raises(PublisherError, match="^incomplete_page$"):
        graph.scan_page(ROOT)


@pytest.mark.parametrize("count", [200, 201])
def test_page_is_bounded_at_200_total_entries_not_only_photos(count):
    graph = client([response({"value": [{"id": "entry" + str(i)} for i in range(count)]})])
    if count == PAGE_SIZE:
        page = graph.scan_page(ROOT)
        assert page["entries"] == PAGE_SIZE and len(page["ids"]) == PAGE_SIZE
        assert page["rows"] == [] and page["skipped"] == {"nonfile": PAGE_SIZE}
    else:
        with pytest.raises(PublisherError, match="^page_bound$"):
            graph.scan_page(ROOT)


@pytest.mark.parametrize("rows", [[source(), source()], [{"id": "same", "folder": {}}, {"id": "same", "remoteItem": {}}]])
def test_local_duplicate_ids_include_skipped_entries(rows):
    graph = client([response({"value": rows})])
    with pytest.raises(PublisherError, match="^duplicate_item_during_scan$"):
        graph.scan_page(ROOT)


@pytest.mark.parametrize("row,code", [
    (None, "invalid_graph_item"), ([], "invalid_graph_item"), ({}, "invalid_graph_id"),
    ({"id": "a/b"}, "invalid_graph_id"), ({"id": "folder", "folder": None}, "invalid_graph_item"),
    (source(file=None), "invalid_graph_item"), (source(name=None), "invalid_graph_item"),
    (source(file={"mimeType": []}), "invalid_graph_item"),
    (source(size=True), "file_size_bound"), (source(size=-1), "file_size_bound"),
    (source(size=MAX_BYTES + 1), "file_size_bound"), (source(size=1.5), "file_size_bound"),
    (source(cTag={}, eTag="e1"), "invalid_item_version"),
    (source(cTag="a" * 4097), "invalid_item_version"),
    (source(cTag=None, eTag=None), "missing_item_version"),
])
def test_invalid_rows_never_form_a_partial_success(row, code):
    graph = client([response({"value": [source("good"), row]})])
    with pytest.raises(PublisherError, match="^" + code + "$"):
        graph.scan_page(ROOT)


def test_download_is_exactly_inherited_and_original_bytes_auth_and_redirect_rules_survive():
    assert PagedGraph.download is GraphClient.download
    before, after, download = response(source()), response(source()), response()
    graph = client([before, after], [download])
    graph.expected_root = ROOT
    assert graph.download(minimal(), "jpg") == JPEG
    assert graph.session.get.call_count == 2
    assert all(call.args[0] == GRAPH + "/me/drive/items/item1" for call in graph.session.get.call_args_list)
    assert all("$select=" not in call.args[0] for call in graph.session.get.call_args_list)
    assert all(call.kwargs["headers"]["Authorization"] == "Bearer secret-token"
               and call.kwargs["allow_redirects"] is False for call in graph.session.get.call_args_list)
    assert graph.download_session.get.call_args.args[0] == SIGNED
    assert graph.download_session.get.call_args.kwargs["headers"] == {"Accept-Encoding": "identity"}
    assert graph.download_session.get.call_args.kwargs["allow_redirects"] is False
    assert graph.download_session.get.call_args.kwargs["stream"] is True
    assert not graph.session.trust_env and not graph.download_session.trust_env
    for reply in (before, after, download):
        reply.close.assert_called_once()


@pytest.mark.parametrize("which", ["before", "after"])
@pytest.mark.parametrize("parent", [None, {}, {"id": "foreign", "driveId": ROOT["drive_id"]},
                                     {"id": ROOT["id"]}, {"id": ROOT["id"], "driveId": "foreign-drive"}])
def test_download_checks_parent_and_drive_before_and_after_bytes(which, parent):
    changed = source(parentReference=parent)
    graph = client([response(changed if which == "before" else source()), response(changed)], [response()])
    graph.expected_root = ROOT
    with pytest.raises(PublisherError, match="^download_item_outside_root$"):
        graph.download(minimal(), "jpg")
    assert graph.download_session.get.call_count == (0 if which == "before" else 1)
    assert graph.session.get.call_count == (1 if which == "before" else 2)


@pytest.mark.parametrize("which", ["before", "after"])
@pytest.mark.parametrize("changes", [{"remoteItem": {}}, {"folder": {}}, {"file": None}])
def test_download_rejects_remote_or_nonfile_even_in_second_read(which, changes):
    changed = source(**changes)
    graph = client([response(changed if which == "before" else source()), response(changed)], [response()])
    graph.expected_root = ROOT
    with pytest.raises(PublisherError, match="^download_item_changed$"):
        graph.download(minimal(), "jpg")


def test_missing_expected_root_and_unsafe_item_id_fail_before_io():
    graph = client()
    with pytest.raises(PublisherError, match="^missing_expected_root$"):
        graph.download(minimal(), "jpg")
    graph.expected_root = ROOT
    with pytest.raises(PublisherError, match="^invalid_graph_id$"):
        graph.item("a/b")
    graph.session.get.assert_not_called()


def test_null_drive_id_is_compared_fail_closed_too():
    graph = client([response(source())])
    graph.expected_root = {"id": ROOT["id"], "drive_id": None}
    with pytest.raises(PublisherError, match="^download_item_outside_root$"):
        graph.item("item1")
    without_drive = source(parentReference={"id": ROOT["id"]})
    graph = client([response(without_drive), response(without_drive)], [response()])
    graph.expected_root = {"id": ROOT["id"], "drive_id": None}
    assert graph.download(minimal(), "jpg") == JPEG


@pytest.mark.parametrize("which", ["before", "after"])
def test_inherited_version_race_checks_are_not_bypassed(which):
    changed = source(cTag="c2")
    graph = client([response(changed if which == "before" else source()), response(changed)], [response()])
    graph.expected_root = ROOT
    with pytest.raises(PublisherError, match="^download_race_" + which + "$"):
        graph.download(minimal(), "jpg")


def test_partial_second_page_raises_no_empty_completion():
    link = CHILDREN + "?$skiptoken=opaque"
    graph = client([response({"value": [source()], "@odata.nextLink": link}), response(status=404)])
    first = graph.scan_page(ROOT)
    with pytest.raises(PublisherError, match="^http_404$"):
        graph.scan_page(ROOT, first["next"])


def test_no_redirect_can_forward_graph_or_download_authorization():
    graph = client([response(status=302, headers={"Location": "https://evil.example/"})])
    with pytest.raises(PublisherError, match="^http_302$"):
        graph.scan_page(ROOT)
    assert graph.session.get.call_count == 1
    graph = client([response(source())], [response(status=302, headers={"Location": "https://evil.example/"})])
    graph.expected_root = ROOT
    with pytest.raises(PublisherError, match="^http_302$"):
        graph.download(minimal(), "jpg")
    assert graph.download_session.get.call_count == 1


def test_retry_after_numeric_is_honored_without_resetting_base_retry_rules():
    sleep = Mock()
    replies = [response(status=429, headers={"Retry-After": "3"}), response(status=503), response({"value": []})]
    graph = client(replies, sleep=sleep)
    assert graph.scan_page(ROOT)["next"] is None
    assert [call.args for call in sleep.call_args_list] == [(3.0,), (2,)]
    assert graph.session.get.call_count == 3
    assert graph.retry_not_before is None
    for reply in replies:
        reply.close.assert_called_once()


@pytest.mark.parametrize("raw", ["600", "http-date"])
def test_long_retry_after_records_utc_boundary_and_never_sleeps_or_retries_early(monkeypatch, raw):
    now = 1804640400.0
    monkeypatch.setattr("paged_graph.time.time", lambda: now)
    if raw == "http-date":
        raw = format_datetime(datetime.fromtimestamp(now + 600, timezone.utc), usegmt=True)
    reply, sleep = response(status=429, headers={"Retry-After": raw}), Mock()
    graph = client([reply], sleep=sleep)
    with pytest.raises(PublisherError, match="^retry_budget$"):
        graph.scan_page(ROOT)
    assert graph.retry_not_before == now + 600
    sleep.assert_not_called()
    assert graph.session.get.call_count == 1
    reply.close.assert_called_once()


def test_retry_after_larger_than_remaining_budget_is_durable_even_below_120(monkeypatch):
    monkeypatch.setattr("paged_graph.time.time", lambda: 1000.0)
    graph = client([response(status=503, headers={"Retry-After": "15"})], sleep=Mock())
    graph.budget = Mock(return_value=10)
    with pytest.raises(PublisherError, match="^retry_budget$"):
        graph.scan_page(ROOT)
    assert graph.retry_not_before == 1015.0
    graph.sleep.assert_not_called()


def test_expired_budget_inside_wait_still_records_retry_after(monkeypatch):
    monkeypatch.setattr("paged_graph.time.time", lambda: 1000.0)
    graph = client(sleep=Mock())
    graph.budget = Mock(side_effect=PublisherError("scan_deadline"))
    with pytest.raises(PublisherError, match="^scan_deadline$"):
        graph._wait(response(status=429, headers={"Retry-After": "600"}), 0)
    assert graph.retry_not_before == 1600.0
    graph.sleep.assert_not_called()


def test_retry_boundary_cannot_be_shortened(monkeypatch):
    monkeypatch.setattr("paged_graph.time.time", lambda: 1000.0)
    graph = client(sleep=Mock())
    graph.retry_not_before = 2000.0
    graph.budget = Mock(return_value=10)
    with pytest.raises(PublisherError, match="^retry_budget$"):
        graph._wait(response(status=429, headers={"Retry-After": "15"}), 0)
    assert graph.retry_not_before == 2000.0


@pytest.mark.parametrize("raw", ["invalid", "NaN", "Infinity", "-3"])
def test_malformed_or_negative_retry_after_uses_safe_base_delay(raw):
    graph = client(sleep=Mock())
    graph._wait(response(headers={"Retry-After": raw}), 2)
    assert graph.sleep.call_args.args == ((0,) if raw == "-3" else (4,))


def test_transport_error_and_exhaustion_keep_safe_codes_and_five_attempt_bound():
    graph = client([requests.ConnectionError("private URL secret-token") for _ in range(5)], sleep=Mock())
    with pytest.raises(PublisherError, match="^transport_error$"):
        graph.scan_page(ROOT)
    assert graph.session.get.call_count == 5
    replies = [response(status=503) for _ in range(5)]
    graph = client(replies, sleep=Mock())
    with pytest.raises(PublisherError, match="^retry_exhausted$"):
        graph.scan_page(ROOT)
    assert graph.session.get.call_count == 5
    for reply in replies:
        reply.close.assert_called_once()


def test_deadline_stops_before_any_io():
    graph = client(deadline=time.monotonic() - 1)
    with pytest.raises(PublisherError, match="^scan_deadline$"):
        graph.scan_page(ROOT)
    graph.session.get.assert_not_called()
