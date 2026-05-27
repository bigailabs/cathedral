"""HTTP client for the private SAT challenge generator service.

Pairs with the contract documented in ``docs/lanes/sat-generator-contract.md``
and Serge's implementation at ``fred-bsat/src/generator/service/app.py``.

The generator is Cathedral-internal infrastructure: miners and validators
NEVER hit it. This client is only used by the Cathedral publisher to
lease pre-generated CNFs from the pool, fetch the bytes, verify the
sha256, then either ``confirm`` (the challenge was durably imported into
``lane_challenges``) or ``release`` (anything failed; return the CNF to
the pool for someone else).

Auth: bearer token loaded from ``CATHEDRAL_SAT_GENERATOR_TOKEN``. The
token is NEVER logged, NEVER written to disk, and NEVER included in any
miner-facing surface. All log lines emitted from this module are
manually structured to exclude the Authorization header and any
secret-bearing path components.

Failure model: every error subclasses ``SatGeneratorError`` so the
caller (the import flow) can catch the boundary cleanly and release the
lease without leaking exception types from httpx.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx
import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SatGeneratorError(Exception):
    """Base for every error this client raises. Catch this at the boundary."""


class SatGeneratorAuthError(SatGeneratorError):
    """Bearer token rejected (401/403). Operator must rotate or re-set."""


class SatGeneratorNotFound(SatGeneratorError):
    """Lease, artifact, or other resource not found (404)."""


class SatGeneratorConflict(SatGeneratorError):
    """Confirm received a hash mismatch (409) or other conflict."""


class SatGeneratorTimeout(SatGeneratorError):
    """Request exceeded the configured timeout."""


class SatGeneratorServerError(SatGeneratorError):
    """5xx from the generator."""


class SatGeneratorHashMismatch(SatGeneratorError):
    """Downloaded CNF body did not match the leased sha256."""


class SatGeneratorOversized(SatGeneratorError):
    """Downloaded CNF exceeded the configured max_cnf_bytes cap."""


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseResult:
    """A leased CNF artifact from the generator pool.

    Mirrors the response shape of ``POST /v1/challenges/lease``. The
    ``cnf_url`` field is treated as opaque internal data — it is never
    surfaced to miners or written into miner-facing audit metadata.
    """

    lease_id: str
    expires_at: datetime
    generator_run_id: str
    cnf_url: str
    cnf_sha256: str
    byte_size: int
    num_vars: int
    num_clauses: int
    tier: int
    kind: str
    family: str
    cnf_class: str

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> "LeaseResult":
        return cls(
            lease_id=str(body["lease_id"]),
            expires_at=_parse_iso8601(str(body["expires_at"])),
            generator_run_id=str(body["generator_run_id"]),
            cnf_url=str(body["cnf_url"]),
            cnf_sha256=str(body["cnf_sha256"]).lower(),
            byte_size=int(body["byte_size"]),
            num_vars=int(body["num_vars"]),
            num_clauses=int(body["num_clauses"]),
            tier=int(body["tier"]),
            kind=str(body["kind"]),
            family=str(body.get("family", "synthetic_boolean_v1")),
            cnf_class=str(body.get("cnf_class", "")),
        )


@dataclass(frozen=True)
class PoolHealth:
    """Per-(family, tier, kind) pool depth + producer rate.

    Used by the future top-up loop to decide when to lease more CNFs.
    """

    families: list[dict[str, Any]]
    producer: dict[str, Any]

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> "PoolHealth":
        return cls(
            families=list(body.get("families") or []),
            producer=dict(body.get("producer") or {}),
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


# Identify a token by sha256 prefix so rotation is detectable from logs
# without ever logging any byte of the token itself.
_TOKEN_FINGERPRINT_LEN = 12


class SatGeneratorClient:
    """Thin async HTTP client for the private generator service.

    Construct one per publisher process; pass it into the import flow.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 30.0,
        max_cnf_bytes: int = 64 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = float(timeout_seconds)
        self._max_cnf_bytes = int(max_cnf_bytes)
        # Build the httpx client. The Authorization header is set per
        # request (NOT on the client object's headers) so a stack trace
        # involving the client doesn't render the header in debugger reprs.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SatGeneratorClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """GET /health — basic liveness + producer status."""
        return await self._get_json("/health")

    async def pool_health(self) -> PoolHealth:
        """GET /v1/pool/health — per-(tier,kind) pool depth + generation rate."""
        return PoolHealth.from_json(await self._get_json("/v1/pool/health"))

    async def lease(
        self,
        *,
        tier: int,
        kind: str,
        family: str = "synthetic_boolean_v1",
        idempotency_key: str | None = None,
    ) -> LeaseResult:
        """POST /v1/challenges/lease — lease one pre-generated CNF.

        The generator will return the same lease for a repeated
        ``Idempotency-Key`` within its retry window. We auto-generate
        a uuid4 if none provided so a single client call is naturally
        idempotent across transient connection failures.
        """
        if tier < 1:
            raise ValueError("tier must be >= 1")
        if not kind:
            raise ValueError("kind is required")
        key = idempotency_key or str(uuid4())
        body = {"family": family, "tier": int(tier), "kind": str(kind)}
        resp = await self._request(
            "POST", "/v1/challenges/lease", json=body, extra_headers={"Idempotency-Key": key}
        )
        return LeaseResult.from_json(resp.json())

    async def fetch_cnf(self, lease: LeaseResult) -> bytes:
        """GET the lease's cnf_url — returns body bytes after sha256 verify.

        Streams + hashes the body; aborts if the running byte count
        exceeds ``max_cnf_bytes`` or if the final sha256 doesn't match
        ``lease.cnf_sha256``. NEVER returns un-verified bytes.
        """
        url = lease.cnf_url
        # cnf_url is generator-internal — log only the path (no host, no token).
        try:
            path_only = httpx.URL(url).path
        except Exception:
            path_only = "<opaque>"
        try:
            # follow_redirects=True: generators commonly serve artifacts
            # via a 301/302 to a signed object-store URL (R2/S3). The
            # sha256 verify below still gates which bytes we accept, so
            # following the redirect is safe.
            async with self._client.stream(
                "GET",
                url,
                headers=self._auth_headers(),
                timeout=self._timeout,
                follow_redirects=True,
            ) as resp:
                if resp.status_code in (401, 403):
                    raise SatGeneratorAuthError(
                        f"auth rejected on artifact fetch (status {resp.status_code})"
                    )
                if resp.status_code == 404:
                    raise SatGeneratorNotFound(f"artifact not found: {path_only}")
                if 500 <= resp.status_code < 600:
                    raise SatGeneratorServerError(
                        f"server error on artifact fetch (status {resp.status_code})"
                    )
                if resp.status_code != 200:
                    raise SatGeneratorError(
                        f"unexpected status {resp.status_code} on artifact fetch"
                    )
                hasher = hashlib.sha256()
                bytes_seen = 0
                chunks: list[bytes] = []
                async for chunk in resp.aiter_bytes():
                    bytes_seen += len(chunk)
                    if bytes_seen > self._max_cnf_bytes:
                        raise SatGeneratorOversized(
                            f"cnf exceeds max_cnf_bytes={self._max_cnf_bytes} "
                            f"(received at least {bytes_seen})"
                        )
                    hasher.update(chunk)
                    chunks.append(chunk)
                actual_sha = hasher.hexdigest()
        except httpx.TimeoutException as exc:
            raise SatGeneratorTimeout(f"timeout fetching {path_only}") from exc
        except (httpx.RequestError, httpx.HTTPError) as exc:
            # Don't include exc args in the message — they can contain URL fragments
            raise SatGeneratorError(f"network error fetching {path_only}") from exc

        if actual_sha != lease.cnf_sha256:
            raise SatGeneratorHashMismatch(
                f"sha256 mismatch: expected={lease.cnf_sha256[:12]}... "
                f"got={actual_sha[:12]}..."
            )
        logger.info(
            "sat_generator_cnf_fetched",
            lease_id=lease.lease_id,
            bytes_received=bytes_seen,
            sha256_prefix=actual_sha[:12],
        )
        return b"".join(chunks)

    async def confirm(
        self,
        lease_id: str,
        *,
        cathedral_challenge_id: str,
        cnf_sha256_witnessed: str,
    ) -> None:
        """POST /v1/challenges/lease/{lease_id}/confirm.

        Generator returns 409 on hash mismatch; we raise SatGeneratorConflict.
        """
        body = {
            "cathedral_challenge_id": cathedral_challenge_id,
            "cnf_sha256_witnessed": cnf_sha256_witnessed.lower(),
        }
        try:
            await self._request(
                "POST", f"/v1/challenges/lease/{lease_id}/confirm", json=body
            )
        except SatGeneratorConflict:
            raise
        logger.info(
            "sat_generator_lease_confirmed",
            lease_id=lease_id,
            cathedral_challenge_id=cathedral_challenge_id,
        )

    async def release(self, lease_id: str) -> None:
        """POST /v1/challenges/lease/{lease_id}/release — return to pool.

        Best-effort: a release failure on top of a primary failure should
        NOT crash the caller. The caller logs separately.
        """
        await self._request("POST", f"/v1/challenges/lease/{lease_id}/release")
        logger.info("sat_generator_lease_released", lease_id=lease_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _get_json(self, path: str) -> dict[str, Any]:
        resp = await self._request("GET", path)
        return resp.json()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = self._base_url + path
        headers = self._auth_headers()
        if extra_headers:
            # Defensive: ensure no caller can override Authorization by accident
            merged = dict(extra_headers)
            merged.pop("Authorization", None)
            merged.pop("authorization", None)
            headers.update(merged)
        try:
            resp = await self._client.request(method, url, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise SatGeneratorTimeout(f"timeout on {method} {path}") from exc
        except (httpx.RequestError, httpx.HTTPError) as exc:
            raise SatGeneratorError(f"network error on {method} {path}") from exc
        return self._raise_for_status(resp, method=method, path=path)

    def _raise_for_status(
        self,
        resp: httpx.Response,
        *,
        method: str,
        path: str,
    ) -> httpx.Response:
        sc = resp.status_code
        if 200 <= sc < 300:
            return resp
        if sc in (401, 403):
            raise SatGeneratorAuthError(
                f"auth rejected on {method} {path} (status {sc})"
            )
        if sc == 404:
            raise SatGeneratorNotFound(f"not found: {method} {path}")
        if sc == 409:
            # Deliberately do not include resp.text — authenticated service
            # bodies can contain identifiers we don't want re-emitted in
            # exception messages.
            raise SatGeneratorConflict(f"conflict on {method} {path}")
        if 500 <= sc < 600:
            raise SatGeneratorServerError(
                f"server error on {method} {path} (status {sc})"
            )
        raise SatGeneratorError(f"unexpected status {sc} on {method} {path}")


# ---------------------------------------------------------------------------
# Helpers + factory
# ---------------------------------------------------------------------------


def _parse_iso8601(s: str) -> datetime:
    # FastAPI emits Z-suffixed UTC; normalize for datetime.fromisoformat.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def client_from_env(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SatGeneratorClient | None:
    """Construct a client from env vars, or return None if not configured.

    Recognised env vars (all CATHEDRAL_SAT_GENERATOR_*):
      - ENABLED ("true" required to construct)
      - URL (required if enabled)
      - TOKEN (required if enabled)
      - TIMEOUT_SECONDS (default 30)
      - MAX_CNF_BYTES (default 64 MiB)

    Returns None when ENABLED is unset/false, so callers can safely
    no-op when the generator isn't configured.
    """
    enabled = os.environ.get("CATHEDRAL_SAT_GENERATOR_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    base_url = os.environ.get("CATHEDRAL_SAT_GENERATOR_URL", "").strip()
    token = os.environ.get("CATHEDRAL_SAT_GENERATOR_TOKEN", "").strip()
    if not base_url:
        raise ValueError(
            "CATHEDRAL_SAT_GENERATOR_ENABLED=true but CATHEDRAL_SAT_GENERATOR_URL is empty"
        )
    if not token:
        raise ValueError(
            "CATHEDRAL_SAT_GENERATOR_ENABLED=true but CATHEDRAL_SAT_GENERATOR_TOKEN is empty"
        )
    timeout = float(os.environ.get("CATHEDRAL_SAT_GENERATOR_TIMEOUT_SECONDS", "30"))
    max_bytes = int(
        os.environ.get("CATHEDRAL_SAT_GENERATOR_MAX_CNF_BYTES", str(64 * 1024 * 1024))
    )
    # Identify the token by sha256 fingerprint so an operator can confirm
    # rotation took effect without leaking ANY byte of the token itself.
    token_fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:_TOKEN_FINGERPRINT_LEN]
    logger.info(
        "sat_generator_client_initialised",
        base_url=base_url,
        token_fingerprint=token_fp,
        timeout_seconds=timeout,
        max_cnf_bytes=max_bytes,
    )
    return SatGeneratorClient(
        base_url=base_url,
        token=token,
        timeout_seconds=timeout,
        max_cnf_bytes=max_bytes,
        transport=transport,
    )
