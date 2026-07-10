"""Hippius blob backend via the presigned-URL API (the path that actually works).

Hippius's IPFS/objectstore-API upload paths are unreliable (stuck Processing,
hanging objects endpoint). But the underlying S3 at s3.hippius.com is fast and
correct when driven through PRESIGNED URLs minted by the Token API:

  publisher: POST-less GET /api/objectstore/buckets/{bucket}/presigned-url/
             ?key=K&action=put  -> a signed s3.hippius.com PUT url. Then PUT bytes.
  reader:    GET .../presigned-url/?key=K&action=get&expires_in=SECS
             -> a signed GET url readable with ZERO auth for SECS seconds.

So the per-epoch manifest embeds presigned GET urls (TTL >= epoch length) and
miners read CNFs straight from Hippius with no credential. Buckets are private;
presigned urls are the read path.

Proven live 2026-07-06: PUT 200 in 1.2s, miner-on-Stitch GET 200 in 0.78s, bytes
matched.
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_API = "https://api.hippius.com/api"


class HippiusPresign:
    def __init__(
        self,
        *,
        token: str,
        bucket: str,
        api_base: str = DEFAULT_API,
        timeout: float = 30.0,
    ) -> None:
        self.token = token
        self.bucket = bucket
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "HippiusPresign | None":
        token = os.environ.get("CATHEDRAL_HIPPIUS_TOKEN", "").strip()
        bucket = os.environ.get("CATHEDRAL_HIPPIUS_BUCKET", "").strip()
        api = os.environ.get("CATHEDRAL_HIPPIUS_API", DEFAULT_API).strip()
        if not (token and bucket):
            return None
        return cls(token=token, bucket=bucket, api_base=api)

    def _presign(self, key: str, action: str, expires_in: int) -> str:
        url = (
            f"{self.api_base}/objectstore/buckets/{quote(self.bucket)}/presigned-url/"
            f"?key={quote(key, safe='')}&action={action}&expires_in={int(expires_in)}"
        )
        req = Request(
            url,
            headers={
                "Authorization": f"Token {self.token}",
                # Hippius's WAF 403s the default python-urllib User-Agent.
                "User-Agent": "cathedral-publisher/1",
            },
        )
        with urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - fixed Hippius API host.
            body = json.loads(r.read())
        signed = body.get("url")
        if not signed:
            raise RuntimeError(
                f"hippius_presign_failed action={action} key={key}: {body}"
            )
        return signed

    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "text/plain; charset=utf-8",
        cache_control: str | None = None,
        get_ttl: int = 7200,
    ) -> str:
        """Upload bytes to `key`. Returns a public presigned GET url with a long TTL
        (default = 2h, covers an epoch) that a miner reads with no auth."""
        put_url = self._presign(key, "put", expires_in=3600)
        headers = {
            "Content-Type": content_type,
            "User-Agent": "cathedral-publisher/1",
        }
        if cache_control:
            headers["Cache-Control"] = cache_control
        req = Request(put_url, data=data, method="PUT", headers=headers)
        with urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - presigned s3.hippius.com.
            if not (200 <= r.status < 300):
                raise RuntimeError(f"hippius_put_failed status={r.status} key={key}")
        return self.get_url(key, expires_in=get_ttl)

    def get_url(self, key: str, *, expires_in: int = 7200) -> str:
        """A presigned GET url readable with zero auth for expires_in seconds."""
        return self._presign(key, "get", expires_in=expires_in)

    def get_bytes(self, key: str, *, max_bytes: int = 0) -> bytes:
        """Read an object through a presigned URL, optionally with a size cap."""
        req = Request(
            self.get_url(key),
            headers={"User-Agent": "cathedral-publisher/1"},
        )
        chunks: list[bytes] = []
        total = 0
        with urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - presigned s3.hippius.com.
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes > 0 and total > max_bytes:
                    raise ValueError("hippius_object_too_large")
                chunks.append(chunk)
        return b"".join(chunks)

    def get_if_exists(self, key: str, *, max_bytes: int = 0) -> bytes | None:
        """Return object bytes or ``None`` for an actual object-level 404."""
        try:
            return self.get_bytes(key, max_bytes=max_bytes)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
