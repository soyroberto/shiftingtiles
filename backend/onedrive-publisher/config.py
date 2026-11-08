"""Nonsecret configuration; invalid input fails closed before remote operations."""
from dataclasses import dataclass
import hashlib
import json
import os
import re
from urllib.parse import quote

DEFAULT_LIBRARIES = '[{"id":"spotify","path":"Musica/Spotify","recursive":false,"enabled":true}]'
LIBRARY_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")
MANAGED_PATH = re.compile(r"photos/onedrive/[a-z0-9][a-z0-9_-]{0,47}/[a-f0-9]{64}\.(jpg|jpeg|png|webp|gif|avif)\Z")
TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "webp": "image/webp", "gif": "image/gif", "avif": "image/avif"}


class PublisherError(Exception):
    """Safe error code only: never include Graph URLs, names, tokens or responses."""


@dataclass(frozen=True)
class Library:
    id: str
    path: str
    recursive: bool = False
    enabled: bool = True


def boolean(value, name):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise PublisherError("invalid_" + name)


def bounded_integer(name, default, minimum, maximum):
    raw = os.getenv(name, str(default))
    if not re.fullmatch(r"[0-9]{1,6}", raw):
        raise PublisherError("invalid_" + name)
    value = int(raw)
    if not minimum <= value <= maximum:
        raise PublisherError("invalid_" + name)
    return value


def libraries(raw):
    try:
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) > 20:
            raise ValueError
        result = []
        seen = set()
        for row in data:
            if not isinstance(row, dict) or set(row) - {"id", "path", "recursive", "enabled"}:
                raise ValueError
            ident, path = row["id"], row["path"]
            if not isinstance(ident, str) or not LIBRARY_ID.fullmatch(ident) or ident in seen:
                raise ValueError
            if not isinstance(path, str) or len(path) > 1024 or "\\" in path or "\x00" in path:
                raise ValueError
            if not path or any(p in ("", ".", "..") for p in path.split("/")):
                raise ValueError
            if not isinstance(row.get("recursive", False), bool) or not isinstance(row.get("enabled", True), bool):
                raise ValueError
            seen.add(ident)
            result.append(Library(ident, path, row.get("recursive", False), row.get("enabled", True)))
        return result
    except (KeyError, ValueError, TypeError) as exc:
        raise PublisherError("invalid_ONEDRIVE_LIBRARIES") from None


@dataclass(frozen=True)
class Config:
    enabled: bool
    libraries: list
    client_id: str
    account_url: str = "https://saauxfotos.blob.core.windows.net"
    state_container: str = "onedrive-state"
    batch_size: int = 64
    max_items: int = 100000
    max_pages: int = 500
    run_seconds: int = 180
    retain_initial: bool = False
    progressive: bool = False
    skip_invalid: bool = False

    @classmethod
    def from_env(cls):
        client = os.getenv("ONEDRIVE_CLIENT_ID", "")
        if client and not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", client):
            raise PublisherError("invalid_ONEDRIVE_CLIENT_ID")
        # Deliberately fixed storage boundaries for this single-site beta.
        return cls(boolean(os.getenv("ONEDRIVE_SYNC_ENABLED", "false"), "ONEDRIVE_SYNC_ENABLED"),
                   libraries(os.getenv("ONEDRIVE_LIBRARIES", DEFAULT_LIBRARIES)), client,
                   batch_size=bounded_integer("ONEDRIVE_BATCH_SIZE", 64, 1, 100),
                   max_items=bounded_integer("ONEDRIVE_MAX_ITEMS", 100000, 1000, 100000),
                   max_pages=bounded_integer("ONEDRIVE_MAX_PAGES", 500, 1, 500),
                   run_seconds=bounded_integer("ONEDRIVE_RUN_SECONDS", 180, 60, 180),
                   retain_initial=boolean(os.getenv("ONEDRIVE_RETAIN_INITIAL", "false"),
                                          "ONEDRIVE_RETAIN_INITIAL"),
                   progressive=boolean(os.getenv("ONEDRIVE_PROGRESSIVE_PUBLISH", "false"),
                                       "ONEDRIVE_PROGRESSIVE_PUBLISH"),
                   skip_invalid=boolean(os.getenv("ONEDRIVE_SKIP_INVALID", "false"),
                                        "ONEDRIVE_SKIP_INVALID"))


def encoded_path(path):
    return "/".join(quote(segment, safe="") for segment in path.split("/"))


def content_version(item):
    if not item.get("id") or not (item.get("cTag") or item.get("eTag")):
        raise PublisherError("missing_item_version")
    return json.dumps([item.get("cTag"), item.get("eTag")], separators=(",", ":"))


def photo_path(library_id, item_id, version, ext):
    if not LIBRARY_ID.fullmatch(library_id) or ext not in TYPES:
        raise PublisherError("unsafe_photo_path")
    digest = hashlib.sha256(json.dumps([library_id, item_id, version], separators=(",", ":")).encode()).hexdigest()
    return f"photos/onedrive/{library_id}/{digest}.{ext}"


def manifest(libs):
    from datetime import datetime, timezone
    paths = sorted({item["path"] for lib in libs.values() for item in lib["items"].values()})
    if any(not MANAGED_PATH.fullmatch(path) for path in paths):
        raise PublisherError("unsafe_manifest_path")
    return {"version": 1, "images": paths,
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
