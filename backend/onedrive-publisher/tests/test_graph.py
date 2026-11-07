import time
from unittest.mock import Mock
import pytest
import requests

from config import Library, PublisherError, photo_path, content_version
from graph import GraphClient, GRAPH, raster_signature

JPEG = b"\xff\xd8\xffunaltered original bytes"


def item(ident="1", tag="v1", name="private.jpg"):
    return {"id": ident, "cTag": tag, "eTag": tag, "name": name, "file": {"mimeType": "image/jpeg"},
            "size": len(JPEG), "@microsoft.graph.downloadUrl": "https://download.example.test/private?secret=hidden"}


def response(data=None, status=200, headers=None, content=JPEG):
    r = Mock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = data
    r.iter_content.return_value = iter([content])
    return r


def client(responses, downloads=None, **kwargs):
    session = Mock()
    session.get.side_effect = responses
    download = Mock()
    download.get.side_effect = downloads or []
    return GraphClient("secret-token", session=session, download_session=download, **kwargs)


def test_encoded_folder_resolves_and_all_pages_enumerated():
    g = client([response({"id": "folder", "folder": {}}),
                response({"value": [item()], "@odata.nextLink": GRAPH + "/me/drive/items/folder/children?$skiptoken=opaque"}),
                response({"value": [item("2")]})])
    rows, skipped = g.scan(Library("safe", "Fotos/Space # /á+?"))
    assert len(rows) == 2 and skipped == {}
    assert "/Fotos/Space%20%23%20/%C3%A1%2B%3F?" in g.session.get.call_args_list[0].args[0]
    assert all(c.kwargs["allow_redirects"] is False for c in g.session.get.call_args_list)
    assert g.session.get.call_count == 3


def test_partial_pagination_raises_instead_of_returning_partial_list():
    g = client([response({"id": "folder", "folder": {}}),
                response({"value": [item()], "@odata.nextLink": GRAPH + "/me/drive/items/folder/children?page=2"}),
                response(status=404)])
    with pytest.raises(PublisherError, match="http_404"):
        g.scan(Library("safe", "Fotos"))


def test_malformed_missing_value_raises():
    g = client([response({"id": "folder", "folder": {}}), response({})])
    with pytest.raises(PublisherError, match="incomplete_page"):
        g.scan(Library("safe", "Fotos"))


def test_foreign_nextlink_rejected_before_token_can_leak():
    g = client([response({"id": "folder", "folder": {}}),
                response({"value": [], "@odata.nextLink": "https://evil.example.test/v1.0/steal"})])
    with pytest.raises(PublisherError, match="unsafe_graph_url"):
        g.scan(Library("safe", "Fotos"))
    assert g.session.get.call_count == 2


@pytest.mark.parametrize("recursive,count", [(False, 0), (True, 1)])
def test_recursion_is_explicit(recursive, count):
    g = client([response({"id": "folder", "folder": {}}),
                response({"value": [{"id": "child", "folder": {}}]}),
                response({"value": [item()]})])
    rows, _ = g.scan(Library("safe", "Fotos", recursive=recursive))
    assert len(rows) == count
    assert g.session.get.call_count == (3 if recursive else 2)


def test_unsupported_rasters_svg_remote_and_mime_skipped_with_counts():
    rows = [item(str(i), name="private." + ext) for i, ext in enumerate(["HEIC", "TIFF", "SVG", "tif", "jpeg"])]
    rows[-1]["file"]["mimeType"] = "image/svg+xml"
    rows.append({"id": "remote", "folder": {}, "remoteItem": {}})
    g = client([response({"id": "folder", "folder": {}}), response({"value": rows})])
    result, skipped = g.scan(Library("safe", "Fotos", recursive=True))
    assert not result
    assert skipped == {"heic": 1, "tiff": 1, "svg": 1, "tif": 1, "mime": 1, "remote": 1}


def test_original_download_has_no_authorization_and_stays_unchanged():
    source = item()
    g = client([response(source), response(source)], [response()])
    result = g.download(source, "jpg")
    assert result == JPEG
    headers = g.download_session.get.call_args.kwargs["headers"]
    assert "Authorization" not in headers and headers == {"Accept-Encoding": "identity"}
    assert g.download_session.trust_env is False
    assert g.session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer secret-token"


@pytest.mark.parametrize("which", ["before", "after"])
def test_content_tag_race_aborts_publication(which):
    source, changed = item(), item(tag="v2")
    g = client([response(changed if which == "before" else source), response(changed)], [response()])
    with pytest.raises(PublisherError, match="download_race_" + which):
        g.download(source, "jpg")
    assert g.download_session.get.call_count == (0 if which == "before" else 1)


def test_content_length_race_aborts():
    source = item()
    g = client([response(source), response(source)], [response(content=JPEG[:-1])])
    with pytest.raises(PublisherError, match="download_race_after"):
        g.download(source, "jpg")


def test_no_redirect_can_forward_graph_authentication():
    g = client([response(status=302, headers={"Location": "https://evil.example/steal"})])
    with pytest.raises(PublisherError, match="http_302"):
        g.item("source/id")
    assert "source%2Fid" in g.session.get.call_args.args[0]
    assert g.session.get.call_count == 1


def test_retry_after_honored_and_transient_retry_bounded():
    sleep = Mock()
    g = client([response(status=429, headers={"Retry-After": "3"}), response(status=503), response({"id": "ok"})], sleep=sleep)
    assert g.item("1") == {"id": "ok"}
    assert sleep.call_args_list[0].args == (3.0,)
    assert g.session.get.call_count == 3


def test_long_retry_after_aborts_instead_of_retrying_early():
    sleep = Mock()
    g = client([response(status=429, headers={"Retry-After": "600"})], sleep=sleep)
    with pytest.raises(PublisherError, match="retry_budget"):
        g.item("1")
    sleep.assert_not_called()


def test_retry_exhaustion_is_bounded():
    g = client([response(status=503) for _ in range(5)], sleep=Mock())
    with pytest.raises(PublisherError, match="retry_exhausted"):
        g.item("1")
    assert g.session.get.call_count == 5


def test_transport_errors_are_safe_codes_without_urls():
    g = client([requests.ConnectionError("private URL secret-token") for _ in range(5)], sleep=Mock())
    with pytest.raises(PublisherError, match="^transport_error$"):
        g.item("1")


def test_deadline_stops_before_io():
    g = client([], deadline=time.monotonic() - 1)
    with pytest.raises(PublisherError, match="scan_deadline"):
        g.item("1")
    g.session.get.assert_not_called()


def test_duplicate_page_items_are_not_a_successful_scan():
    g = client([response({"id": "folder", "folder": {}}), response({"value": [item(), item()]})])
    with pytest.raises(PublisherError, match="duplicate_item"):
        g.scan(Library("safe", "Fotos"))


@pytest.mark.parametrize("ext,data", [("jpg", b"\xff\xd8\xffrest"), ("jpeg", b"\xff\xd8\xffrest"),
    ("png", b"\x89PNG\r\n\x1a\nrest"), ("gif", b"GIF89arest"), ("webp", b"RIFFxxxxWEBPrest"),
    ("avif", b"\x00\x00\x00\x14ftypavif\x00\x00\x00\x00mif1")])
def test_browser_raster_magic_and_svg_disguise_rejection(ext, data):
    assert raster_signature(data, ext)
    assert not raster_signature(b"<svg>private</svg>", ext)


def test_opaque_stable_version_paths_have_no_source_names_or_ids():
    source = item()
    version = content_version(source)
    path = photo_path("safe", source["id"], version, "jpg")
    assert path == photo_path("safe", source["id"], version, "jpg")
    assert "private" not in path and "/1/" not in path
    assert path != photo_path("safe", source["id"], version + "changed", "jpg")
