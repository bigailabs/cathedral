"""Operator probe for the token-gated SAT CNF download path."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

from cathedral.lanes.challenge_source import (
    SqliteChallengeSource,
    SqliteFetchTokenStore,
    init_sqlite_challenge_source,
)
from cathedral.lanes.synthetic_boolean_v1 import (
    DEFAULT_TIME_LIMIT_SECONDS,
)
from cathedral.lanes.synthetic_boolean_v1 import (
    FAMILY_ID as SYNTHETIC_BOOLEAN_FAMILY_ID,
)


async def probe_sat_cnf_url(
    url: str,
    *,
    expected_sha256: str,
    timeout_secs: float = 300.0,
    min_bytes_per_second: float = 0.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Stream a token-gated CNF URL and verify bytes without returning content."""
    expected = expected_sha256.strip().lower()
    if not _looks_like_sha256(expected):
        return {
            "ok": False,
            "errors": ["expected CNF SHA-256 must be a 64-character hex digest"],
        }

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_secs),
            follow_redirects=False,
        )

    started = time.perf_counter()
    status_code: int | None = None
    content_type = ""
    byte_count = 0
    digest = hashlib.sha256()
    try:
        async with client.stream("GET", url) as response:
            status_code = response.status_code
            content_type = response.headers.get("content-type", "")
            if response.status_code != 200:
                elapsed = _elapsed(started)
                return _probe_result(
                    ok=False,
                    status_code=status_code,
                    content_type=content_type,
                    byte_count=0,
                    elapsed_secs=elapsed,
                    expected_sha256=expected,
                    actual_sha256=None,
                    min_bytes_per_second=min_bytes_per_second,
                    errors=[f"CNF URL returned HTTP {response.status_code}"],
                )

            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                byte_count += len(chunk)
                digest.update(chunk)
    except httpx.HTTPError as exc:
        elapsed = _elapsed(started)
        return _probe_result(
            ok=False,
            status_code=status_code,
            content_type=content_type,
            byte_count=byte_count,
            elapsed_secs=elapsed,
            expected_sha256=expected,
            actual_sha256=None,
            min_bytes_per_second=min_bytes_per_second,
            errors=[f"CNF URL probe failed: {exc.__class__.__name__}"],
        )
    finally:
        if close_client:
            await client.aclose()

    elapsed = _elapsed(started)
    actual = digest.hexdigest()
    rate = _bytes_per_second(byte_count, elapsed)
    errors: list[str] = []
    if byte_count <= 0:
        errors.append("CNF URL returned an empty body")
    if actual != expected:
        errors.append("CNF URL SHA-256 does not match expected metadata")
    if min_bytes_per_second > 0 and rate < min_bytes_per_second:
        errors.append("CNF URL throughput is below the configured minimum")

    return _probe_result(
        ok=not errors,
        status_code=status_code,
        content_type=content_type,
        byte_count=byte_count,
        elapsed_secs=elapsed,
        expected_sha256=expected,
        actual_sha256=actual,
        min_bytes_per_second=min_bytes_per_second,
        errors=errors,
    )


async def probe_active_sat_cnf_url_from_db(
    database_path: str,
    *,
    public_base_url: str,
    timeout_secs: float = 300.0,
    min_bytes_per_second: float = 0.0,
    announced_time_limit_secs: int = DEFAULT_TIME_LIMIT_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Probe the active SAT CNF through the public URL surface.

    The command mints a fetch token when one does not already exist for
    the active challenge. The result never includes the token or URL.
    """
    if not await asyncio.to_thread(_database_exists, database_path):
        return {
            "ok": False,
            "active": False,
            "family_id": SYNTHETIC_BOOLEAN_FAMILY_ID,
            "errors": ["publisher database does not exist"],
        }
    base_error = _public_base_url_error(public_base_url)
    if base_error:
        return {
            "ok": False,
            "active": False,
            "family_id": SYNTHETIC_BOOLEAN_FAMILY_ID,
            "errors": [base_error],
        }

    conn = await init_sqlite_challenge_source(database_path)
    try:
        source = SqliteChallengeSource(conn)
        tokens = SqliteFetchTokenStore(conn)
        record = await source.get_active(SYNTHETIC_BOOLEAN_FAMILY_ID)
        if record is None:
            return {
                "ok": False,
                "active": False,
                "family_id": SYNTHETIC_BOOLEAN_FAMILY_ID,
                "errors": ["no active synthetic_boolean_v1 challenge"],
            }

        expected = _expected_sha256(record)
        base_result: dict[str, Any] = {
            "active": True,
            "challenge_id": record.challenge_id,
            "family_id": record.family_id,
            "tier": record.tier,
            "status": record.status,
            "storage": "file" if record.cnf_path else "sqlite_text",
        }
        if expected is None:
            return {
                **base_result,
                "ok": False,
                "errors": ["active challenge has no CNF SHA-256 metadata"],
            }

        existing = await tokens.get(record.challenge_id)
        if existing is None:
            token_status = "minted"
            token_row = await tokens.mint_if_absent(
                record.challenge_id,
                fetch_token=secrets.token_urlsafe(32),
                minted_at_iso=_now_ms_iso(),
                announced_time_limit_secs=int(announced_time_limit_secs),
            )
        else:
            token_status = "existing"
            token_row = existing
        base_result["announced_time_limit_secs"] = token_row.announced_time_limit_secs
        url = _cnf_url(public_base_url, record.challenge_id, token_row.fetch_token)
        probe = await probe_sat_cnf_url(
            url,
            expected_sha256=expected,
            timeout_secs=timeout_secs,
            min_bytes_per_second=min_bytes_per_second,
            client=client,
        )
        return {
            **base_result,
            "fetch_token_status": token_status,
            **probe,
        }
    finally:
        await conn.close()


def _probe_result(
    *,
    ok: bool,
    status_code: int | None,
    content_type: str,
    byte_count: int,
    elapsed_secs: float,
    expected_sha256: str,
    actual_sha256: str | None,
    min_bytes_per_second: float,
    errors: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": ok,
        "status_code": status_code,
        "content_type": content_type,
        "bytes": byte_count,
        "elapsed_secs": elapsed_secs,
        "bytes_per_second": _bytes_per_second(byte_count, elapsed_secs),
        "cnf_sha256_expected": expected_sha256,
        "min_bytes_per_second": min_bytes_per_second,
    }
    if actual_sha256 is not None:
        result["cnf_sha256_actual"] = actual_sha256
        result["cnf_hash_matches_expected"] = actual_sha256 == expected_sha256
    if errors:
        result["errors"] = errors
    return result


def _expected_sha256(record: Any) -> str | None:
    audit_value = record.audit_metadata.get("cnf_sha256")
    if audit_value is not None:
        candidate = str(audit_value).strip().lower()
        return candidate if _looks_like_sha256(candidate) else None
    if record.cnf_text:
        return hashlib.sha256(record.cnf_text.encode("utf-8")).hexdigest()
    return None


def _public_base_url_error(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "public base URL must be an absolute http(s) URL"
    return None


def _cnf_url(public_base_url: str, challenge_id: str, fetch_token: str) -> str:
    base = public_base_url.rstrip("/")
    challenge = quote(challenge_id, safe="")
    query = urlencode({"t": fetch_token})
    return f"{base}/v1/challenges/{challenge}/cnf?{query}"


def _looks_like_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _elapsed(started: float) -> float:
    return max(time.perf_counter() - started, 0.000001)


def _bytes_per_second(byte_count: int, elapsed_secs: float) -> float:
    return byte_count / max(elapsed_secs, 0.000001)


def _database_exists(database_path: str) -> bool:
    return Path(database_path).expanduser().exists()


def _now_ms_iso() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


__all__ = [
    "probe_active_sat_cnf_url_from_db",
    "probe_sat_cnf_url",
]
