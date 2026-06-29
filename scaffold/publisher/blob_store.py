"""Blob storage adapters for V2 solution artifacts.

Phase 1/2 supports a local filesystem backend for tests/staging and a generic
HTTP fetch path for externally published blobs. Hippius/IPFS/R2 can be layered in
by returning content-addressed URLs/CIDs in the manifest; the verifier only needs
`fetch(cid)` plus sha256 validation.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

_CID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9+.-]{1,31}://")


@dataclass(frozen=True)
class BlobPutResult:
    cid: str
    sha256: str
    size: int


class BlobStore(Protocol):
    def put(self, data: bytes, *, kind: str = "solution") -> BlobPutResult: ...
    def fetch(self, cid: str, *, max_bytes: int = 0) -> bytes: ...


class LocalBlobStore:
    """Content-addressed local blob store.

    CIDs are `local://<kind>/<sha256>`. This is deliberately simple and useful
    for staging/private beta when the app and verifier share a volume. For true
    decentralized storage miners should upload to Hippius/IPFS/etc. and submit
    that CID in the same manifest shape.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, sha256: str) -> Path:
        safe_kind = re.sub(r"[^a-zA-Z0-9_.-]", "_", kind or "blob")
        return self.root / safe_kind / sha256[:2] / sha256[2:]

    def put(self, data: bytes, *, kind: str = "solution") -> BlobPutResult:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("blob data must be bytes")
        body = bytes(data)
        sha = hashlib.sha256(body).hexdigest()
        path = self._path(kind, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(body)
        return BlobPutResult(cid=f"local://{quote(kind)}/{sha}", sha256=sha, size=len(body))

    def fetch(self, cid: str, *, max_bytes: int = 0) -> bytes:
        if not cid.startswith("local://"):
            raise ValueError("unsupported_local_cid")
        rest = cid[len("local://"):]
        try:
            kind, sha = rest.split("/", 1)
        except ValueError as exc:
            raise ValueError("invalid_local_cid") from exc
        kind = unquote(kind)
        sha = sha.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("invalid_local_sha")
        path = self._path(kind, sha)
        if not path.exists():
            raise FileNotFoundError("blob_not_found")
        if max_bytes > 0 and path.stat().st_size > max_bytes:
            raise ValueError("blob_too_large")
        return path.read_bytes()


class CompositeBlobStore:
    """Local + HTTP(S) fetch adapter.

    HTTP fetch is read-only: the miner or a prior upload step publishes the blob
    and the manifest carries its URL/CID. For schemes like hippius:// or ipfs://,
    set CATHEDRAL_V2_CID_GATEWAY_TEMPLATE to an HTTP URL containing `{cid}`.
    """

    def __init__(self, local: LocalBlobStore, *, timeout: float = 30.0,
                 gateway_template: str = "") -> None:
        self.local = local
        self.timeout = timeout
        self.gateway_template = gateway_template

    def put(self, data: bytes, *, kind: str = "solution") -> BlobPutResult:
        return self.local.put(data, kind=kind)

    def _fetch_http(self, url: str, *, max_bytes: int = 0) -> bytes:
        req = Request(url, headers={"User-Agent": "cathedral-v2-verifier/1"})
        chunks: list[bytes] = []
        total = 0
        with urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - verifier fetches miner-provided blob URLs by design.
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes > 0 and total > max_bytes:
                    raise ValueError("blob_too_large")
                chunks.append(chunk)
        return b"".join(chunks)

    def fetch(self, cid: str, *, max_bytes: int = 0) -> bytes:
        if cid.startswith("local://"):
            return self.local.fetch(cid, max_bytes=max_bytes)
        if cid.startswith("http://") or cid.startswith("https://"):
            return self._fetch_http(cid, max_bytes=max_bytes)
        if _CID_RE.match(cid):
            if self.gateway_template:
                url = self.gateway_template.replace("{cid}", quote(cid, safe=""))
                return self._fetch_http(url, max_bytes=max_bytes)
            raise ValueError("cid_fetch_backend_not_configured")
        raise ValueError("unsupported_cid_scheme")


def store_from_env() -> CompositeBlobStore:
    root = (
        os.environ.get("CATHEDRAL_V2_BLOB_DIR")
        or os.environ.get("CATHEDRAL_BLOB_DIR")
        or "/tmp/cathedral-v2-blobs"
    )
    timeout = float(os.environ.get("CATHEDRAL_V2_BLOB_FETCH_TIMEOUT_SECS", "30") or "30")
    gateway_template = os.environ.get("CATHEDRAL_V2_CID_GATEWAY_TEMPLATE", "").strip()
    return CompositeBlobStore(LocalBlobStore(root), timeout=timeout, gateway_template=gateway_template)
