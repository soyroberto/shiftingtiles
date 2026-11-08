"""Bounded, resumable Graph pages; source names and download URLs stay ephemeral.

The engine owns global duplicate/page/entry limits and persists only scan_page's
minimal output. Set expected_root to root()'s result before calling the inherited
download(), and bind expected_parent to the recursive row's validated parent_id.
Both full-metadata reads verify the parent/drive and fresh ancestry to that root.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import re
import time
from urllib.parse import quote, unquote, urlsplit

from config import PublisherError, TYPES, content_version, encoded_path
from graph import GRAPH, MAX_BYTES, GraphClient

PAGE_SIZE = 200
PAGE_SELECT = "id,name,file,folder,remoteItem,size,cTag,eTag"
# Graph item/drive IDs are opaque identifiers, not user-supplied paths. Personal
# OneDrive IDs use '!'; business drive IDs may also use URL-safe base64 characters.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9!._~-]{0,511}\Z")
_CHILDREN_PREFIX = "/v1.0/me/drive/items/"
_CHILDREN_SUFFIX = "/children"
MAX_TREE_DEPTH = 32
MAX_TREE_FOLDERS = 1000
MAX_TREE_CURSOR = 192 * 1024  # Leaves room for the bounded private chunk catalogs.
_TREE_FIELDS = {"v", "root", "folders", "current", "next", "pending", "seen", "pages"}


def _tree_json(state):
    return "tree:" + json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)


class PagedGraph(GraphClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_root = None
        self.retry_not_before = None
        self.recursive = False
        self.expected_parent = None

    @staticmethod
    def _identifier(ident):
        if not isinstance(ident, str) or not _IDENTIFIER.fullmatch(ident):
            raise PublisherError("invalid_graph_id")
        return ident

    @staticmethod
    def root_checked(ident, drive_id=None):
        """Validate persisted root IDs without interpreting them as route paths."""
        PagedGraph._identifier(ident)
        if drive_id is not None:
            PagedGraph._identifier(drive_id)
        return {"id": ident, "drive_id": drive_id}

    @classmethod
    def _checked_root(cls, root):
        if not isinstance(root, dict) or set(root) != {"id", "drive_id"}:
            raise PublisherError("invalid_graph_root")
        return cls.root_checked(root["id"], root["drive_id"])

    def root(self, library):
        if type(library.recursive) is not bool:
            raise PublisherError("invalid_source_recursion")
        self.recursive = library.recursive
        path = library.path
        if (not isinstance(path, str) or not path or len(path) > 1024 or
                "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path) or
                any(segment in ("", ".", "..") for segment in path.split("/"))):
            raise PublisherError("invalid_source_path")
        data = self.json(
            f"{GRAPH}/me/drive/root:/{encoded_path(path)}"
            "?$select=id,folder,remoteItem,parentReference"
        )
        if "remoteItem" in data or not isinstance(data.get("folder"), dict):
            raise PublisherError("source_not_owned_folder")
        parent = data.get("parentReference", {})
        if not isinstance(parent, dict):
            raise PublisherError("invalid_parent_reference")
        if self.recursive:
            self._identifier(parent.get("driveId"))
        return self.root_checked(data.get("id"), parent.get("driveId"))

    @classmethod
    def _cursor_checked(cls, root, cursor):
        if isinstance(cursor, str) and cursor.startswith("tree:"):
            cls._tree_checked(root, cursor)
            return cursor
        return cls._url_checked(root, cursor)

    @classmethod
    def _url_checked(cls, root, cursor):
        # urlsplit discards some control characters: reject them *before* parsing.
        if (not isinstance(cursor, str) or not cursor or len(cursor) > 16384 or
                "\\" in cursor or "#" in cursor or
                any(ord(c) <= 32 or ord(c) == 127 for c in cursor)):
            raise PublisherError("unsafe_graph_cursor")
        try:
            parsed = urlsplit(cursor)
        except ValueError:
            raise PublisherError("unsafe_graph_cursor") from None
        if (not cursor.startswith(GRAPH + "/") or parsed.scheme != "https" or
                parsed.netloc != "graph.microsoft.com" or parsed.fragment or
                not parsed.path.startswith(_CHILDREN_PREFIX) or
                not parsed.path.endswith(_CHILDREN_SUFFIX)):
            raise PublisherError("unsafe_graph_cursor")
        segment = parsed.path[len(_CHILDREN_PREFIX):-len(_CHILDREN_SUFFIX)]
        # Preserve the exact nextLink query, including opaque skip tokens. Graph
        # may spell an ID's '!' literally or percent-encode it. Decode only the
        # single ID segment, never the endpoint; aliases/other roots are forbidden.
        try:
            ident = unquote(segment, errors="strict")
        except UnicodeError:
            raise PublisherError("unsafe_graph_cursor") from None
        if ident != root["id"] or not _IDENTIFIER.fullmatch(ident):
            raise PublisherError("unsafe_graph_cursor")
        return cursor

    @classmethod
    def _tree_checked(cls, root, cursor):
        """A compact parent table is the durable proof chain/frontier, never names.

        Table entries remain after visiting a folder to reject rediscovery/cycles.
        Claims are structurally checked here and re-proved by Graph before use.
        """
        try:
            if len(cursor.encode("utf-8")) > MAX_TREE_CURSOR:
                raise ValueError
            state = json.loads(cursor[5:])
            if (not isinstance(state, dict) or set(state) != _TREE_FIELDS or
                    type(state["v"]) is not int or state["v"] != 1 or
                    state["root"] != cls._checked_root(root) or root["drive_id"] is None or
                    _tree_json(state) != cursor):
                raise ValueError
            folders = state["folders"]
            if not isinstance(folders, list) or not 1 <= len(folders) <= MAX_TREE_FOLDERS:
                raise ValueError
            parents = {}
            for entry in folders:
                if not isinstance(entry, list) or len(entry) != 2:
                    raise ValueError
                ident, parent = entry
                cls._identifier(ident)
                if ident in parents or (parent is None) != (ident == root["id"]):
                    raise ValueError
                if parent is not None:
                    cls._identifier(parent)
                parents[ident] = parent
            if folders[0] != [root["id"], None]:
                raise ValueError
            for ident in parents:
                cls._chain(root, parents, ident)
            pending = state["pending"]
            current = state["current"]
            if (not isinstance(pending, list) or any(not isinstance(i, str) for i in pending) or
                    len(set(pending)) != len(pending) or current not in parents or
                    current in pending or root["id"] in pending or
                    any(i not in parents for i in pending)):
                raise ValueError
            if state["next"] is not None:
                cls._url_checked({"id": current}, state["next"])
            seen = state["seen"]
            if (type(state["pages"]) is not int or not 1 <= state["pages"] <= 500 or
                    not isinstance(seen, list) or len(seen) > state["pages"] or
                    any(not isinstance(i, str) or not re.fullmatch(r"[a-f0-9]{64}", i) for i in seen) or
                    len(set(seen)) != len(seen) or
                    (state["next"] is None and seen) or
                    (state["next"] is not None and not seen)):
                raise ValueError
            return state
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, PublisherError):
            raise PublisherError("unsafe_graph_cursor") from None

    @staticmethod
    def _chain(root, parents, ident):
        chain = []
        while ident is not None:
            if ident in chain or ident not in parents or len(chain) > MAX_TREE_DEPTH:
                raise PublisherError("folder_bound")
            chain.append(ident)
            ident = parents[ident]
        if not chain or chain[-1] != root["id"]:
            raise PublisherError("source_ancestry_changed")
        return list(reversed(chain))

    def _folder(self, root, ident, parent=None):
        self._identifier(ident)
        data = self.json(f"{GRAPH}/me/drive/items/{quote(ident, safe='')}"
                         "?$select=id,folder,remoteItem,parentReference")
        ref = data.get("parentReference")
        if (data.get("id") != ident or "remoteItem" in data or
                not isinstance(data.get("folder"), dict) or "file" in data or
                not isinstance(ref, dict) or ref.get("driveId") != root["drive_id"] or
                (parent is not None and ref.get("id") != parent)):
            raise PublisherError("source_ancestry_changed")
        return ref

    def _verify_chain(self, root, chain):
        for index, ident in enumerate(chain):
            self._folder(root, ident, chain[index - 1] if index else None)

    def _verify_parent(self, root, parent):
        """Fresh bounded metadata reads on BOTH inherited download item reads.

        Do not authorize membership from a row/cursor claim or an invocation cache:
        any moved, remote, cyclic or foreign-drive ancestor fails closed.
        """
        self._identifier(parent)
        seen = set()
        while parent != root["id"]:
            if parent in seen or len(seen) >= MAX_TREE_DEPTH:
                raise PublisherError("source_ancestry_changed")
            seen.add(parent)
            ref = self._folder(root, parent)
            parent = self._identifier(ref.get("id"))
        self._folder(root, root["id"])

    @staticmethod
    def _first_url(ident, recursive=False):
        select = PAGE_SELECT + (",parentReference" if recursive else "")
        return (f"{GRAPH}/me/drive/items/{quote(ident, safe='')}/children"
                f"?$select={select}&$top={PAGE_SIZE}")

    def scan_page(self, root, cursor=None):
        root = self._checked_root(root)
        tree = None
        if self.recursive:
            self._identifier(root["drive_id"])
            if cursor is None:
                tree = {"v": 1, "root": root, "folders": [[root["id"], None]],
                        "current": root["id"], "next": None, "pending": [], "seen": [], "pages": 0}
            else:
                if not isinstance(cursor, str) or not cursor.startswith("tree:"):
                    raise PublisherError("unsafe_graph_cursor")
                tree = self._tree_checked(root, cursor)
            current = tree["current"]
            parents = dict(tree["folders"])
            chain = self._chain(root, parents, current)
            self._verify_chain(root, chain)
            url = tree["next"] or self._first_url(current, True)
            if tree["pages"] >= 500 or hashlib.sha256(url.encode()).hexdigest() in tree["seen"]:
                raise PublisherError("pagination_bound")
        else:
            url = self._first_url(root["id"]) if cursor is None else self._url_checked(root, cursor)
            current = root["id"]
        self.budget()
        data = self.json(url)
        values = data.get("value")
        if not isinstance(values, list):
            raise PublisherError("incomplete_page")
        if len(values) > PAGE_SIZE:
            raise PublisherError("page_bound")
        rows, ids, seen, skipped = [], [], set(), {}

        def skip(label):
            skipped[label] = skipped.get(label, 0) + 1

        for source in values:
            if not isinstance(source, dict):
                raise PublisherError("invalid_graph_item")
            ident = self._identifier(source.get("id"))
            if ident in seen:
                raise PublisherError("duplicate_item_during_scan")
            seen.add(ident)
            ids.append(ident)
            if tree is not None:
                parent = source.get("parentReference")
                if ("remoteItem" in source or not isinstance(parent, dict) or
                        parent.get("id") != current or parent.get("driveId") != root["drive_id"]):
                    raise PublisherError("source_ancestry_changed")
            if "remoteItem" in source:
                skip("remote")
                continue
            if "folder" in source:
                if not isinstance(source["folder"], dict):
                    raise PublisherError("invalid_graph_item")
                if tree is not None:
                    if ident in parents or len(parents) >= MAX_TREE_FOLDERS:
                        raise PublisherError("folder_bound")
                    parents[ident] = current
                    self._chain(root, parents, ident)
                    tree["folders"].append([ident, current])
                    tree["pending"].append(ident)
                skip("folder")
                continue
            if "file" not in source:
                skip("nonfile")
                continue
            file = source["file"]
            name = source.get("name")
            if not isinstance(file, dict) or not isinstance(name, str):
                raise PublisherError("invalid_graph_item")
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else "unknown"
            if ext not in TYPES:
                skip(ext if ext in ("heic", "heif", "tif", "tiff", "svg") else "other")
                continue
            mime = file.get("mimeType", "application/octet-stream")
            if not isinstance(mime, str):
                raise PublisherError("invalid_graph_item")
            if mime not in (TYPES[ext], "application/octet-stream"):
                skip("mime")
                continue
            size = source.get("size")
            if type(size) is not int or size < 0:
                raise PublisherError("file_size_bound")
            if size > MAX_BYTES:
                if getattr(self, "skip_invalid", False):
                    skip("oversize")
                    continue
                raise PublisherError("file_size_bound")
            tags = [source.get("cTag"), source.get("eTag")]
            if any(tag is not None and (not isinstance(tag, str) or len(tag) > 4096)
                   for tag in tags):
                raise PublisherError("invalid_item_version")
            row = {"id": ident, "size": size, "cTag": tags[0], "eTag": tags[1],
                   "file": {"mimeType": mime}, "ext": ext}
            if tree is not None:
                row["parent_id"] = current
            content_version(row)
            rows.append(row)
        link = data.get("@odata.nextLink")
        if link is not None:
            if not isinstance(link, str) or not link:
                raise PublisherError("invalid_nextlink")
            self._url_checked({"id": current}, link)
            if link == url:
                raise PublisherError("pagination_bound")
        if tree is not None:
            self._verify_chain(root, chain)  # A move during enumeration cannot commit a page.
            tree["pages"] += 1
            tree["seen"].append(hashlib.sha256(url.encode()).hexdigest())
            if link is not None:
                if hashlib.sha256(link.encode()).hexdigest() in tree["seen"]:
                    raise PublisherError("pagination_bound")
                tree["next"] = link
            elif tree["pending"]:
                tree["current"] = tree["pending"].pop(0)
                tree["next"], tree["seen"] = None, []
            else:
                tree = None
            link = _tree_json(tree) if tree is not None else None
            if link is not None:
                self._tree_checked(root, link)
        return {"rows": rows, "ids": ids, "next": link, "entries": len(values),
                "skipped": skipped}

    def item(self, ident):
        if self.expected_root is None:
            raise PublisherError("missing_expected_root")
        root = self._checked_root(self.expected_root)
        self._identifier(ident)
        data = super().item(ident)
        parent = data.get("parentReference")
        expected = self.expected_parent if self.recursive else root["id"]
        if (not isinstance(parent, dict) or expected is None or parent.get("id") != expected or
                parent.get("driveId") != root["drive_id"]):
            raise PublisherError("download_item_outside_root")
        # Enforce this in both reads; legacy download only checks these facets in
        # its first read. The full signed URL is transient, never part of a page.
        if "remoteItem" in data or "folder" in data or not isinstance(data.get("file"), dict):
            raise PublisherError("download_item_changed")
        if self.recursive:
            self._identifier(root["drive_id"])
            self._verify_parent(root, expected)
        return data

    def _wait(self, response, attempt):
        raw = response.headers.get("Retry-After") if response is not None else None
        delay = min(2 ** attempt, 16)
        now = time.time()
        if raw:
            try:
                parsed = float(raw)
                if math.isfinite(parsed):
                    delay = max(0, parsed)
            except (ValueError, TypeError, OverflowError):
                try:
                    parsed = (parsedate_to_datetime(raw) -
                              datetime.fromtimestamp(now, timezone.utc)).total_seconds()
                    if math.isfinite(parsed):
                        delay = max(0, parsed)
                except (ValueError, TypeError, OverflowError):
                    pass
        not_before = now + delay
        # Record the absolute UTC retry boundary before budget() can raise. The
        # engine must carry it into its durable continuation control, not retry
        # at its ordinary five-minute cadence when the server asked for longer.
        try:
            remaining = self.budget()
        except PublisherError:
            self.retry_not_before = max(self.retry_not_before or 0, not_before)
            raise
        if delay > 120 or delay >= remaining:
            self.retry_not_before = max(self.retry_not_before or 0, not_before)
            raise PublisherError("retry_budget")
        self.sleep(delay)
