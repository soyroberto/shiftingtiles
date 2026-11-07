"""Graph access never modifies originals. No source names/URLs are logged."""
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from urllib.parse import quote, urlsplit

import requests
from config import PublisherError, TYPES, content_version, encoded_path

GRAPH = "https://graph.microsoft.com/v1.0"
MAX_ITEMS = 10000
MAX_PAGES = 500
MAX_DEPTH = 20
MAX_BYTES = 50 * 1024 * 1024
RETRIES = 5
SELECT = "id,name,file,folder,remoteItem,size,cTag,eTag,@microsoft.graph.downloadUrl"


class GraphClient:
    def __init__(self, token, deadline=None, session=None, download_session=None, sleep=time.sleep):
        self.token = token
        self.deadline = deadline or time.monotonic() + 210
        self.session = session or requests.Session()
        self.download_session = download_session or requests.Session()
        # Disable netrc auth (especially for preauthenticated URLs) and implicit proxies.
        self.session.trust_env = False
        self.download_session.trust_env = False
        self.sleep = sleep

    def close(self):
        self.session.close()
        self.download_session.close()

    def budget(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise PublisherError("scan_deadline")
        return remaining

    @staticmethod
    def graph_url(url):
        p = urlsplit(url)
        if (p.scheme != "https" or p.netloc != "graph.microsoft.com" or
                not p.path.startswith("/v1.0/") or p.fragment):
            raise PublisherError("unsafe_graph_url")

    @staticmethod
    def download_url(url):
        p = urlsplit(url)
        if (p.scheme != "https" or not p.hostname or p.username or p.password or
                p.port not in (None, 443) or p.fragment):
            raise PublisherError("unsafe_download_url")
        # URLs originate only from authenticated Graph metadata; never from config/names.

    def _wait(self, response, attempt):
        raw = response.headers.get("Retry-After") if response is not None else None
        delay = min(2 ** attempt, 16)
        if raw:
            try:
                delay = max(0, float(raw))
            except ValueError:
                try:
                    delay = max(0, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        # Honor Retry-After or abort; do not hammer early if it exceeds our bound.
        if delay > 120 or delay >= self.budget():
            raise PublisherError("retry_budget")
        self.sleep(delay)

    def get(self, url, authenticated=True, stream=False):
        if authenticated:
            self.graph_url(url)
        else:
            self.download_url(url)
        headers = {"Accept-Encoding": "identity"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        session = self.session if authenticated else self.download_session
        for attempt in range(RETRIES):
            timeout = min(20, self.budget())
            try:
                response = session.get(url, headers=headers, timeout=(min(5, timeout), timeout),
                                       allow_redirects=False, stream=stream)
            except requests.RequestException:
                if attempt == RETRIES - 1:
                    raise PublisherError("transport_error") from None
                self._wait(None, attempt)
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                try:
                    if attempt == RETRIES - 1:
                        raise PublisherError("retry_exhausted")
                    self._wait(response, attempt)
                finally:
                    response.close()
                continue
            if response.status_code != 200:
                response.close()
                raise PublisherError("http_" + str(response.status_code))
            return response
        raise PublisherError("retry_exhausted")

    def json(self, url):
        response = self.get(url)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise PublisherError("invalid_graph_response")
            return data
        except ValueError:
            raise PublisherError("invalid_graph_json") from None
        finally:
            response.close()

    def item(self, ident):
        # Personal OneDrive can omit its download URL from projected metadata.
        # Fetch the full item for download/version checks; keep library scans bounded.
        return self.json(f"{GRAPH}/me/drive/items/{quote(ident, safe='')}")

    def scan(self, library):
        root = self.json(f"{GRAPH}/me/drive/root:/{encoded_path(library.path)}?$select=id,folder,remoteItem")
        if not root.get("id") or "folder" not in root or "remoteItem" in root:
            raise PublisherError("source_not_owned_folder")
        folders = deque([(root["id"], 0)])
        visited_folders = set()
        seen_items = set()
        result = []
        skipped = {}
        pages = 0
        entries = 0
        while folders:
            ident, depth = folders.popleft()
            if ident in visited_folders or depth > MAX_DEPTH:
                raise PublisherError("folder_bound")
            visited_folders.add(ident)
            url = f"{GRAPH}/me/drive/items/{quote(ident, safe='')}/children?$select={SELECT}&$top=200"
            visited_pages = set()
            while url:
                self.budget()
                pages += 1
                if pages > MAX_PAGES or url in visited_pages:
                    raise PublisherError("pagination_bound")
                visited_pages.add(url)
                data = self.json(url)
                if not isinstance(data.get("value"), list):
                    raise PublisherError("incomplete_page")
                for row in data["value"]:
                    entries += 1
                    if entries > MAX_ITEMS or not isinstance(row, dict) or not isinstance(row.get("id"), str):
                        raise PublisherError("item_bound_or_invalid")
                    if row["id"] in seen_items:
                        raise PublisherError("duplicate_item_during_scan")
                    seen_items.add(row["id"])
                    if "remoteItem" in row:
                        skipped["remote"] = skipped.get("remote", 0) + 1
                        continue
                    if "folder" in row:
                        if library.recursive:
                            folders.append((row["id"], depth + 1))
                        continue
                    if "file" not in row:
                        skipped["nonfile"] = skipped.get("nonfile", 0) + 1
                        continue
                    name = row.get("name", "")
                    ext = name.rsplit(".", 1)[-1].lower() if isinstance(name, str) and "." in name else "unknown"
                    if ext not in TYPES:
                        label = ext if ext in ("heic", "heif", "tif", "tiff", "svg") else "other"
                        skipped[label] = skipped.get(label, 0) + 1
                        continue
                    mime = row["file"].get("mimeType", "application/octet-stream")
                    if mime not in (TYPES[ext], "application/octet-stream"):
                        skipped["mime"] = skipped.get("mime", 0) + 1
                        continue
                    if not isinstance(row.get("size"), int) or row["size"] < 0 or row["size"] > MAX_BYTES:
                        raise PublisherError("file_size_bound")
                    content_version(row)
                    result.append((row, ext))
                link = data.get("@odata.nextLink")
                if link is not None and (not isinstance(link, str) or not link):
                    raise PublisherError("invalid_nextlink")
                url = link
        # Any page error raises: no incomplete library snapshot is returned.
        return result, skipped

    def download(self, scanned, ext):
        before = self.item(scanned["id"])
        if before.get("id") != scanned["id"] or content_version(before) != content_version(scanned):
            raise PublisherError("download_race_before")
        if "remoteItem" in before or "file" not in before or before.get("size") != scanned["size"]:
            raise PublisherError("download_item_changed")
        url = before.get("@microsoft.graph.downloadUrl")
        if not isinstance(url, str):
            raise PublisherError("missing_download_url")
        response = self.get(url, authenticated=False, stream=True)
        try:
            if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise PublisherError("unexpected_content_encoding")
            chunks = []
            length = 0
            for chunk in response.iter_content(64 * 1024):
                self.budget()
                length += len(chunk)
                if length > MAX_BYTES:
                    raise PublisherError("download_size_bound")
                chunks.append(chunk)
            content = b"".join(chunks)
        except requests.RequestException:
            raise PublisherError("download_transport_error") from None
        finally:
            response.close()
        after = self.item(scanned["id"])
        if after.get("id") != scanned["id"] or content_version(after) != content_version(before) or length != scanned["size"]:
            raise PublisherError("download_race_after")
        if not raster_signature(content, ext):
            raise PublisherError("invalid_raster_signature")
        return content


def raster_signature(data, ext):
    if ext in ("jpg", "jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == "png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == "gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if ext == "webp":
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if ext == "avif":
        if len(data) < 16 or data[4:8] != b"ftyp":
            return False
        box_size = int.from_bytes(data[:4], "big")
        brands = [data[8:12]] + [data[i:i + 4] for i in range(16, min(box_size, len(data), 256), 4)]
        return b"avif" in brands or b"avis" in brands
    return False
