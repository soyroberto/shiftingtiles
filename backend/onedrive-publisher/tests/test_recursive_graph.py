"""Recursive Graph proofs are exercised offline, including fresh process cursors."""
import copy
import json
from urllib.parse import parse_qs, quote, unquote, urlsplit
from unittest.mock import Mock

import pytest

from config import Library, PublisherError
from graph import GRAPH
from paged_graph import (MAX_TREE_CURSOR, MAX_TREE_DEPTH, MAX_TREE_FOLDERS,
                         PagedGraph, _tree_json)
from test_paged_graph import response
from test_sync import JPEG

ROOT = {"id": "root", "drive_id": "drive"}
LIB = Library("spotify", "Musica/Spotify", recursive=True)
SIGNED = "https://download.example.invalid/private?secret=hidden"


def folder(ident, parent="root", drive="drive", **changes):
    value = {"id": ident, "folder": {}, "name": "private folder title",
             "parentReference": {"id": parent, "driveId": drive}}
    value.update(changes)
    return value


def image(ident="photo", parent="root", tag="one", **changes):
    value = {"id": ident, "name": "private original.JPG", "size": len(JPEG),
             "cTag": tag, "eTag": tag, "file": {"mimeType": "image/jpeg"},
             "parentReference": {"id": parent, "driveId": "drive"},
             "@microsoft.graph.downloadUrl": SIGNED}
    value.update(changes)
    return value


def link(ident="root", index=1):
    return f"{GRAPH}/me/drive/items/{quote(ident, safe='')}/children?$skiptoken={index}"


class TreeGraph(PagedGraph):
    """Real scan/item/download, with only the Graph transport replaced by memory."""
    def __init__(self, pages=None, metadata=None, clock=None, scan_cost=0, download_cost=0):
        session, download = Mock(), Mock()
        super().__init__("ephemeral-token-never-persisted", session=session, download_session=download)
        self.pages = pages or {"root": [{"value": []}]}
        self.metadata = {"root": folder("root", "outer"), "outer": folder("outer", "outer")}
        self.metadata.update(copy.deepcopy(metadata or {}))
        for replies in self.pages.values():
            for reply in replies:
                for source in reply.get("value", []):
                    self.metadata.setdefault(source["id"], copy.deepcopy(source))
        self.calls, self.downloads, self.scans = [], [], []
        self.clock, self.scan_cost, self.download_cost = clock, scan_cost, download_cost
        self.page_hook = None
        self.download_hook = None
        self.remaining = 1
        self.closed = False
        self.download_session.get.side_effect = self._download_response

    def budget(self):
        if self.remaining <= 0:
            raise PublisherError("scan_deadline")
        return self.remaining

    def json(self, url):
        self.budget()
        self.graph_url(url)
        self.calls.append(url)
        parsed = urlsplit(url)
        if "/root:/" in parsed.path:
            return copy.deepcopy(self.metadata["root"])
        segment = parsed.path.split("/items/")[1]
        ident = unquote(segment.split("/")[0])
        if segment.endswith("/children"):
            index = int(parse_qs(parsed.query).get("$skiptoken", [0])[0])
            result = copy.deepcopy(self.pages[ident][index])
            if self.clock:
                self.clock.value += self.scan_cost
            if self.page_hook:
                self.page_hook(ident, index)
            return result
        return copy.deepcopy(self.metadata[ident])

    def scan_page(self, root, cursor=None):
        self.scans.append(cursor)
        return super().scan_page(root, cursor)

    def _download_response(self, url, **kwargs):
        assert url == SIGNED and "Authorization" not in kwargs["headers"]
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        if self.clock:
            self.clock.value += self.download_cost
        if self.download_hook:
            self.download_hook()
        return response(content=JPEG)

    def item(self, ident):
        if self.recursive:
            assert self.expected_parent is not None
        data = super().item(ident)
        if "@microsoft.graph.downloadUrl" in data:
            self.downloads.append(ident)
        return data

    def close(self):
        self.closed = True
        super().close()


def nested_pages():
    return {
        "root": [{"value": [image("r"), folder("a"), folder("b")],
                  "@odata.nextLink": link()}, {"value": [image("r2")]}],
        "a": [{"value": [image("a1", "a"), folder("c", "a")],
               "@odata.nextLink": link("a")}, {"value": [image("a2", "a")]}],
        "b": [{"value": []}],
        "c": [{"value": [image("c1", "c")]}],
    }


def initial_cursor():
    graph = TreeGraph({"root": [{"value": [folder("a")]}], "a": [{"value": []}]})
    root = graph.root(LIB)
    return graph.scan_page(root)["next"]


def test_nested_owned_pages_resume_with_a_new_client_each_page_and_no_private_fields():
    pages, cursor, result = nested_pages(), None, []
    for _ in range(6):
        graph = TreeGraph(pages)
        root = graph.root(LIB)
        page = graph.scan_page(root, cursor)
        result.extend(page["rows"])
        cursor = page["next"]
        assert len([url for url in graph.calls if "/children" in url]) == 1
        if cursor:
            assert cursor.startswith("tree:")
            assert len(cursor.encode()) <= MAX_TREE_CURSOR
            assert PagedGraph._cursor_checked(ROOT, cursor) == cursor
        text = json.dumps(page)
        assert all(secret not in text for secret in ("private original", "private folder", "downloadUrl", "secret=", '"name"'))
    assert cursor is None
    assert [(row["id"], row["parent_id"]) for row in result] == [
        ("r", "root"), ("r2", "root"), ("a1", "a"), ("a2", "a"), ("c1", "c")]


def test_false_keeps_exact_legacy_rows_and_skips_nested_folders():
    graph = TreeGraph({"root": [{"value": [folder("a"), image()]}]})
    assert graph.root(Library("spotify", LIB.path)) == ROOT
    page = graph.scan_page(ROOT)
    assert page["next"] is None and page["skipped"] == {"folder": 1}
    assert "parent_id" not in page["rows"][0]
    assert len(graph.calls) == 2  # root lookup and children only, no extra legacy reads.


def test_recursive_root_requires_drive_proof_but_false_still_accepts_null():
    graph = TreeGraph(metadata={"root": {"id": "root", "folder": {}}})
    with pytest.raises(PublisherError, match="invalid_graph_id"):
        graph.root(LIB)
    assert graph.root(Library("spotify", LIB.path)) == {"id": "root", "drive_id": None}


@pytest.mark.parametrize("changes", [
    {"remoteItem": {}}, {"parentReference": {"id": "foreign", "driveId": "drive"}},
    {"parentReference": {"id": "root", "driveId": "foreign"}}, {"parentReference": None},
])
def test_recursive_page_remote_or_foreign_entries_fail_closed_not_invalid_skip(changes):
    graph = TreeGraph({"root": [{"value": [image("ok"), image(**changes)]}]})
    graph.root(LIB)
    graph.skip_invalid = True
    with pytest.raises(PublisherError, match="source_ancestry_changed"):
        graph.scan_page(ROOT)


@pytest.mark.parametrize("when", ["before", "during"])
@pytest.mark.parametrize("changes", [
    {"parentReference": {"id": "outer", "driveId": "drive"}},
    {"parentReference": {"id": "root", "driveId": "foreign"}},
    {"remoteItem": {}}, {"folder": None},
])
def test_descendant_reproved_before_and_after_enumeration(when, changes):
    graph = TreeGraph({"root": [{"value": [folder("a")]}], "a": [{"value": [image("nested", "a")]}]})
    graph.root(LIB)
    cursor = graph.scan_page(ROOT)["next"]
    graph.calls.clear()
    if when == "before":
        graph.metadata["a"].update(changes)
    else:
        graph.page_hook = lambda *_: graph.metadata["a"].update(changes)
    with pytest.raises(PublisherError, match="source_ancestry_changed"):
        graph.scan_page(ROOT, cursor)
    assert len([url for url in graph.calls if "/children" in url]) == (0 if when == "before" else 1)


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(v=True), lambda s: s.update(extra="private"),
    lambda s: s["root"].update(id="foreign"),
    lambda s: s.update(next=link("foreign"), seen=["a" * 64]),
    lambda s: s.update(next="https://evil.invalid/v1.0/me/drive/items/a/children", seen=["a" * 64]),
    lambda s: s["folders"].append(["a", "root"]),
    lambda s: s["folders"].__setitem__(1, ["a", "a"]),
    lambda s: s["folders"].__setitem__(1, ["a", "missing"]),
    lambda s: s.update(current="missing"), lambda s: s.update(pending=["a", "a"]),
    lambda s: s.update(pages=501), lambda s: s.update(seen=["private source name"]),
    lambda s: s["folders"].append(["a/b", "root"]),
])
def test_tampered_tree_cursor_rejected_before_any_io(mutation):
    state = json.loads(initial_cursor()[5:])
    mutation(state)
    graph = TreeGraph()
    graph.recursive = True
    with pytest.raises(PublisherError, match="unsafe_graph_cursor"):
        graph.scan_page(ROOT, _tree_json(state))
    assert graph.calls == []


def test_tree_cursor_bounds_and_canonical_json_are_checked_before_io():
    cursor = initial_cursor()
    for bad in (cursor + " ", "tree:" + "x" * MAX_TREE_CURSOR, "tree:{", "tree:" + "[" * 2000,
                cursor.replace('"v":1', '"v":1,"v":1')):
        graph = TreeGraph()
        graph.recursive = True
        with pytest.raises(PublisherError, match="unsafe_graph_cursor"):
            graph.scan_page(ROOT, bad)
        assert graph.calls == []


def test_forged_but_structural_foreign_folder_chain_requires_metadata_proof():
    state = json.loads(initial_cursor()[5:])
    state["folders"][1][0] = state["current"] = "foreign"
    graph = TreeGraph(metadata={"foreign": folder("foreign", "outer")})
    graph.recursive = True
    with pytest.raises(PublisherError, match="source_ancestry_changed"):
        graph.scan_page(ROOT, _tree_json(state))
    assert not any("/children" in url for url in graph.calls)


@pytest.mark.parametrize("ident", ["root", "a"])
def test_cycles_and_rediscovered_folder_ids_are_rejected(ident):
    graph = TreeGraph({"root": [{"value": [folder("a")]}],
                       "a": [{"value": [folder(ident, "a")]}]})
    graph.root(LIB)
    cursor = graph.scan_page(ROOT)["next"]
    with pytest.raises(PublisherError, match="folder_bound"):
        graph.scan_page(ROOT, cursor)


def test_depth_32_allowed_but_discovery_of_depth_33_fails_closed():
    pages = {}
    for depth in range(MAX_TREE_DEPTH + 1):
        current = "root" if depth == 0 else "f" + str(depth)
        pages[current] = [{"value": [folder("f" + str(depth + 1), current)]}]
    graph = TreeGraph(pages)
    graph.root(LIB)
    cursor = None
    for _ in range(MAX_TREE_DEPTH):
        cursor = graph.scan_page(ROOT, cursor)["next"]
    with pytest.raises(PublisherError, match="folder_bound"):
        graph.scan_page(ROOT, cursor)


def test_total_folder_discovery_is_bounded_at_1000_including_root():
    state = {"v": 1, "root": ROOT, "folders": [["root", None]], "current": "root",
             "next": link(), "pending": [], "seen": ["a" * 64], "pages": 1}
    for index in range(MAX_TREE_FOLDERS - 1):
        ident = f"folder{index}"
        state["folders"].append([ident, "root"])
        state["pending"].append(ident)
    graph = TreeGraph({"root": [{"value": []}, {"value": [folder("over-limit")]}]})
    graph.recursive = True
    with pytest.raises(PublisherError, match="folder_bound"):
        graph.scan_page(ROOT, _tree_json(state))


def test_empty_pagination_cycles_are_durable_across_new_client_resumption():
    pages = {"root": [{"value": [], "@odata.nextLink": link(index=1)},
                       {"value": [], "@odata.nextLink": link(index=2)},
                       {"value": [], "@odata.nextLink": link(index=1)}]}
    cursor = None
    for _ in range(2):
        graph = TreeGraph(pages)
        graph.root(LIB)
        cursor = graph.scan_page(ROOT, cursor)["next"]
    graph = TreeGraph(pages)
    graph.root(LIB)
    with pytest.raises(PublisherError, match="pagination_bound"):
        graph.scan_page(ROOT, cursor)


def test_tree_rejects_a_foreign_child_nextlink_and_plain_legacy_cursor():
    graph = TreeGraph({"root": [{"value": [], "@odata.nextLink": link("foreign")}]})
    graph.root(LIB)
    with pytest.raises(PublisherError, match="unsafe_graph_cursor"):
        graph.scan_page(ROOT)
    for cursor in (link(), "https://evil.invalid/"):
        before = len(graph.calls)
        with pytest.raises(PublisherError, match="unsafe_graph_cursor"):
            graph.scan_page(ROOT, cursor)
        assert len(graph.calls) == before


def test_deadline_cannot_advance_a_tree_cursor_or_yield_an_incomplete_page():
    graph = TreeGraph()
    graph.recursive = True
    graph.remaining = 0
    with pytest.raises(PublisherError, match="scan_deadline"):
        graph.scan_page(ROOT, initial_cursor())
    assert graph.calls == []


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("mutation", [
    lambda g: g.metadata["a"].update(remoteItem={}),
    lambda g: g.metadata["a"]["parentReference"].update(id="outer"),
    lambda g: g.metadata["a"]["parentReference"].update(driveId="foreign"),
    lambda g: g.metadata["a"]["parentReference"].update(id="a"),
    lambda g: g.metadata["root"].update(remoteItem={}),
    lambda g: g.metadata["photo"]["parentReference"].update(id="root"),
    lambda g: g.metadata["photo"]["parentReference"].update(driveId="foreign"),
    lambda g: g.metadata["photo"].update(remoteItem={}),
])
def test_download_checks_row_parent_and_all_ancestors_on_both_metadata_reads(when, mutation):
    graph = TreeGraph(metadata={"a": folder("a"), "photo": image(parent="a")})
    graph.root(LIB)
    graph.expected_root, graph.expected_parent = ROOT, "a"
    source = {"id": "photo", "parent_id": "a", "size": len(JPEG),
              "cTag": "one", "eTag": "one", "file": {"mimeType": "image/jpeg"}, "ext": "jpg"}
    if when == "before":
        mutation(graph)
    else:
        graph.download_hook = lambda: mutation(graph)
    with pytest.raises(PublisherError, match="source_ancestry_changed|download_item_outside_root|download_item_changed|invalid_graph_id"):
        graph.download(source, "jpg")
    assert graph.download_session.get.call_count == (0 if when == "before" else 1)


def test_original_bytes_download_has_fresh_ancestry_before_and_after_with_no_cache():
    graph = TreeGraph(metadata={"a": folder("a"), "b": folder("b", "a"), "photo": image(parent="b")})
    graph.root(LIB)
    graph.expected_root, graph.expected_parent = ROOT, "b"
    source = {"id": "photo", "parent_id": "b", "size": len(JPEG), "cTag": "one", "eTag": "one"}
    graph.calls.clear()
    assert graph.download(source, "jpg") == JPEG
    assert len(graph.calls) == 8  # file + b + a + root, fresh on each read.
    assert graph.download_session.get.call_count == 1
