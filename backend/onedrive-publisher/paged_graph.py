"""Bounded, resumable Graph pages; source names and download URLs stay ephemeral.

The engine owns global duplicate/page/entry limits and persists only scan_page's
minimal output. Set expected_root to root()'s result before calling the inherited
download(); both full-metadata reads then verify membership in that same root.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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


class PagedGraph(GraphClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_root = None
        self.retry_not_before = None

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
        if library.recursive is not False:
            raise PublisherError("recursive_source_not_supported")
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
        return self.root_checked(data.get("id"), parent.get("driveId"))

    @classmethod
    def _cursor_checked(cls, root, cursor):
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

    def scan_page(self, root, cursor=None):
        root = self._checked_root(root)
        if cursor is None:
            url = (f"{GRAPH}/me/drive/items/{quote(root['id'], safe='')}/children"
                   f"?$select={PAGE_SELECT}&$top={PAGE_SIZE}")
        else:
            url = self._cursor_checked(root, cursor)
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
            if "remoteItem" in source:
                skip("remote")
                continue
            if "folder" in source:
                if not isinstance(source["folder"], dict):
                    raise PublisherError("invalid_graph_item")
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
            content_version(row)
            rows.append(row)
        link = data.get("@odata.nextLink")
        if link is not None:
            if not isinstance(link, str) or not link:
                raise PublisherError("invalid_nextlink")
            self._cursor_checked(root, link)
            if link == url:
                raise PublisherError("pagination_bound")
        return {"rows": rows, "ids": ids, "next": link, "entries": len(values),
                "skipped": skipped}

    def item(self, ident):
        if self.expected_root is None:
            raise PublisherError("missing_expected_root")
        root = self._checked_root(self.expected_root)
        self._identifier(ident)
        data = super().item(ident)
        parent = data.get("parentReference")
        if (not isinstance(parent, dict) or parent.get("id") != root["id"] or
                parent.get("driveId") != root["drive_id"]):
            raise PublisherError("download_item_outside_root")
        # Enforce this in both reads; legacy download only checks these facets in
        # its first read. The full signed URL is transient, never part of a page.
        if "remoteItem" in data or "folder" in data or not isinstance(data.get("file"), dict):
            raise PublisherError("download_item_changed")
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
