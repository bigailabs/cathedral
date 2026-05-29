"""Hippius S3 client (boto3 wrapper).

Bucket layout (CONTRACTS.md Section 5):

    cathedral-bundles/
    ├── agents/{submission_id}.bin.enc   (encrypted, never public)
    ├── logos/{submission_id}.{ext}      (plaintext, public-read)
    └── staging/...                      (CATHEDRAL_ENV=staging mirror)

All async-via-thread, mirroring `cathedral.chain.client.BittensorChain`
so the FastAPI event loop is never blocked by network IO.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


_DEFAULT_ENDPOINT = "https://s3.hippius.com"
_DEFAULT_REGION = "decentralized"
_DEFAULT_BUCKET = "cathedral-bundles"


class HippiusError(Exception):
    """Hippius / S3 transport or auth error. Caller maps to 5xx."""


@dataclass(frozen=True)
class HippiusConfig:
    access_key: str
    secret_key: str
    endpoint_url: str = _DEFAULT_ENDPOINT
    region: str = _DEFAULT_REGION
    bucket: str = _DEFAULT_BUCKET
    env_prefix: str = ""  # e.g. "staging/" for CATHEDRAL_ENV=staging

    @classmethod
    def from_env(cls) -> HippiusConfig:
        access = os.environ.get("HIPPIUS_S3_ACCESS_KEY") or os.environ.get(
            "HIPPIUS_S3_ACCESS_KEY_ID"
        )
        secret = os.environ.get("HIPPIUS_S3_SECRET_KEY") or os.environ.get(
            "HIPPIUS_S3_SECRET_ACCESS_KEY"
        )
        if not access or not secret:
            raise HippiusError("missing HIPPIUS_S3_ACCESS_KEY / HIPPIUS_S3_SECRET_KEY env vars")
        env = os.environ.get("CATHEDRAL_ENV", "").lower()
        prefix = "staging/" if env == "staging" else ""
        return cls(
            access_key=access,
            secret_key=secret,
            endpoint_url=os.environ.get("HIPPIUS_S3_ENDPOINT", _DEFAULT_ENDPOINT),
            region=os.environ.get("HIPPIUS_S3_REGION", _DEFAULT_REGION),
            bucket=os.environ.get("HIPPIUS_S3_BUCKET", _DEFAULT_BUCKET),
            env_prefix=prefix,
        )


class HippiusClient:
    """Lazy boto3 S3 wrapper. One per process is fine; thread-safe enough."""

    def __init__(self, config: HippiusConfig) -> None:
        self.config = config
        self._client: object | None = None

    def _ensure_client(self) -> object:
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:  # pragma: no cover - boto3 not always installed
            raise HippiusError(f"boto3 not installed: {e}") from e

        self._client = boto3.client(
            "s3",
            endpoint_url=self.config.endpoint_url,
            aws_access_key_id=self.config.access_key,
            aws_secret_access_key=self.config.secret_key,
            region_name=self.config.region,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3},
                # Cloudflare R2 (and several Hippius gateways) require path-
                # style addressing. boto3 defaults to virtual-host, which
                # rewrites the SNI to `<bucket>.<endpoint>` and trips an
                # SSL handshake failure on R2's account-scoped wildcard.
                s3={"addressing_style": "path"},
            ),
        )
        return self._client

    def _key(self, prefix: str, name: str) -> str:
        env_prefix = self.config.env_prefix
        return f"{env_prefix}{prefix}{name}"

    def s3_uri(self, key: str) -> str:
        return f"s3://{self.config.bucket}/{key}"

    # ------------------------------------------------------------------
    # Bundle (encrypted, private)
    # ------------------------------------------------------------------

    async def put_bundle(
        self,
        submission_id: str,
        ciphertext: bytes,
        *,
        bundle_hash_hex: str,
    ) -> str:
        """Upload encrypted bundle blob; return the S3 URI stored in DB."""
        key = self._key("agents/", f"{submission_id}.bin.enc")

        def _put() -> str:
            client = self._ensure_client()
            try:
                client.put_object(  # type: ignore[attr-defined]
                    Bucket=self.config.bucket,
                    Key=key,
                    Body=ciphertext,
                    Metadata={
                        "bundle-hash": bundle_hash_hex,
                        "encryption": "aes-256-gcm",
                        "cathedral-version": "v1",
                    },
                    ContentType="application/octet-stream",
                )
            except Exception as e:
                raise HippiusError(f"put_bundle failed: {e}") from e
            return self.s3_uri(key)

        return await asyncio.to_thread(_put)

    async def get_bundle(self, blob_key: str) -> bytes:
        """Fetch encrypted bundle bytes back. Caller decrypts in-memory."""
        key = self._key_from_uri(blob_key)

        def _get() -> bytes:
            client = self._ensure_client()
            try:
                resp = client.get_object(Bucket=self.config.bucket, Key=key)  # type: ignore[attr-defined]
                body = resp["Body"].read()
            except Exception as e:
                raise HippiusError(f"get_bundle failed: {e}") from e
            assert isinstance(body, bytes)
            return body

        return await asyncio.to_thread(_get)

    def presigned_get_url(self, blob_key: str, *, expires_in: int = 3600) -> str:
        """Generate a short-lived presigned GET URL for an encrypted bundle.

        Used by the runtime bundle-fetch path so a runner can fetch the
        miner's bundle directly from R2/Hippius without Cathedral having to stream
        the bytes through its own HTTP request. The bundle remains
        encrypted at rest; the runtime image decrypts it using the KEK
        passed via `env_overrides`.

        Synchronous: presign is a pure local signing operation (no
        network), so wrapping it in `asyncio.to_thread` would only add
        overhead. Callers from async contexts may invoke it directly.
        """
        key = self._key_from_uri(blob_key)
        client = self._ensure_client()
        try:
            url = client.generate_presigned_url(  # type: ignore[attr-defined]
                ClientMethod="get_object",
                Params={"Bucket": self.config.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as e:
            raise HippiusError(f"presign get_object failed: {e}") from e
        assert isinstance(url, str)
        return url

    async def delete_bundle(self, blob_key: str) -> None:
        key = self._key_from_uri(blob_key)

        def _del() -> None:
            client = self._ensure_client()
            try:
                client.delete_object(Bucket=self.config.bucket, Key=key)  # type: ignore[attr-defined]
            except Exception as e:
                raise HippiusError(f"delete_bundle failed: {e}") from e

        await asyncio.to_thread(_del)

    # ------------------------------------------------------------------
    # Logo (public-read, plaintext)
    # ------------------------------------------------------------------

    async def put_logo(self, submission_id: str, raw: bytes, *, content_type: str, ext: str) -> str:
        """Upload logo with public-read ACL. Returns the public HTTPS URL."""
        key = self._key("logos/", f"{submission_id}.{ext}")

        def _put() -> str:
            client = self._ensure_client()
            try:
                client.put_object(  # type: ignore[attr-defined]
                    Bucket=self.config.bucket,
                    Key=key,
                    Body=raw,
                    ContentType=content_type,
                    ACL="public-read",
                )
            except Exception as e:
                raise HippiusError(f"put_logo failed: {e}") from e
            return f"{self.config.endpoint_url.rstrip('/')}/{self.config.bucket}/{key}"

        return await asyncio.to_thread(_put)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def healthcheck(self) -> bool:
        """Cheap HEAD on the bucket. Returns True if reachable."""

        def _check() -> bool:
            try:
                client = self._ensure_client()
                client.head_bucket(Bucket=self.config.bucket)  # type: ignore[attr-defined]
                return True
            except Exception as e:
                logger.warning("hippius_healthcheck_failed", error=str(e))
                return False

        return await asyncio.to_thread(_check)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _key_from_uri(self, blob_key: str) -> str:
        """Accept either an `s3://bucket/key` or a bare key."""
        if blob_key.startswith("s3://"):
            without = blob_key[len("s3://") :]
            _, _, key = without.partition("/")
            return key
        return blob_key
