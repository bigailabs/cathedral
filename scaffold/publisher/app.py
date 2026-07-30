"""The thin publisher — a FastAPI app that replaces the 46.5k-line monolith
publisher while keeping the frozen wire surface byte-identical.

Surfaces:
  M1 validator feed   GET /v1/validator/weights/next (signed final scores + burn)
                      GET /v1/leaderboard/recent  (dual cursor, signed rows — audit trail)
                      GET /.well-known/cathedral-jwks.json
                      GET /health
  M2 Lane A miners    GET /v1/synthetic-boolean/active-challenges | current-challenge
                      GET /v1/synthetic-boolean/active-cnf        (hotkey-signed)
                      GET /v1/challenges/{id}/cnf?t=<token>       (token, opaque 404)
                      POST /v1/agents/submit                      (6-field sig, solve-on-submit)
  M3 Lane S/I         POST /v1/arena/solvers   GET /v1/arena/status
                      POST /v1/arena/instances

Construct with build_app(database_path=..., signing_key_hex=...). The whole
service is one module + the auth/store/rows/sat_solution helpers, well under the
2k new-line cap.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .. import wire
from ..lanes.solver_arena import SolverRegistry, SolverSpec
from . import board_cache as board_cache_mod
from . import keys, rows, scoring, top_cache as top_cache_mod, weights as weights_mod
from . import materialized_snapshot as materialized_snapshot_mod
from . import dashboard_snapshot as dashboard_snapshot_mod
from .auth import canonical_claim_bytes, default_verifier, sha256_hex
from .board_cache import BoardCache, board_cache_headers
from .materialized_snapshot import MaterializedSnapshot, snapshot_headers
from .cnf_store import CNFStore
from . import v2_cnf_store
from . import epoch_publisher as v2_cnf_artifacts
from .sat_solution import verify_dimacs_solution
from . import cybergym_ingest, external_scores, submit_admission
from . import solution_manifest
from . import v2_pipeline
from . import v2_bitset_submit
from . import v2_receipts
from . import blob_store as blob_store_mod
from . import hippius_presign as hippius_presign_mod
from . import results_publisher
from .per_hotkey_limit import (
    ABUSE_REASON as _PER_HOTKEY_ABUSE_REASON,
    PerHotkeyLimiter,
    config_from_env as per_hotkey_config_from_env,
)
from .pressure_telemetry import (
    PressureTelemetry,
    PressureTelemetryMiddleware,
    config_from_env as pressure_config_from_env,
    mark_verified_hotkey,
)
from .store import Store, new_uuid

_FAMILY = "synthetic_boolean_v1"
_AUDIT_SCANNER_CARD = "cathedral_audit_scanner_v1"
_SKEW_SECS = 300
# Public, non-scored readiness probe: a tiny satisfiable toy CNF miners fetch to
# self-test their solve pipeline before mining. Byte-identical to the monolith's
# tier-1 toy instance so any client that pinned its sha still passes.
_READINESS_CNF = "p cnf 3 3\n1 -2 3 0\n-1 2 3 0\n1 2 -3 0\n"
_READINESS_SHA = hashlib.sha256(_READINESS_CNF.encode("utf-8")).hexdigest()
_QUARANTINE_ROUNDS = 3      # Lane I (V4-DESIGN.md)
_MIN_BATCH_SCORE = 0.5      # Lane I (V4-DESIGN.md)
_CNF_TOKEN_TTL = 120        # active-cnf fetch token lifetime (seconds)
_CNF_TOKEN_SECRET_ENV = "CATHEDRAL_CNF_TOKEN_SECRET"
_CNF_PUBLIC_BASE_URL_ENV = "CATHEDRAL_CNF_PUBLIC_BASE_URL"


def _retry_after_payload(reason: str, retry_after_secs: int) -> dict[str, Any]:
    retry_after_secs = max(1, int(retry_after_secs))
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=retry_after_secs)
    return {
        "detail": reason,
        "reason": reason,
        "retry_after_seconds": retry_after_secs,
        "retry_at": retry_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def _retry_after_body(reason: str, retry_after_secs: int) -> bytes:
    return json.dumps(
        _retry_after_payload(reason, retry_after_secs),
        separators=(",", ":"),
    ).encode("utf-8")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def _scope_header(scope, name: bytes) -> str | None:
    """Extract a single header value from a raw ASGI scope (latin-1 decoded)."""
    name_lower = name.lower()
    for k, v in scope.get("headers", []):
        if k.lower() == name_lower:
            return v.decode("latin-1", errors="replace")
    return None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_bytes(name: str, default: int) -> int:
    """Parse environment variable as byte count with sane positive default.

    Accepts:
    - Plain integer (bytes): "1048576"
    - With suffix: "1M", "1Mi", "1MiB" (1 MiB = 1024^2 bytes)

    Returns:
    - Parsed byte count (strictly positive integer)
    - default (sane positive integer) if env var is missing, empty, or unparseable
    """
    positive_default = (
        default
        if isinstance(default, int) and not isinstance(default, bool) and default > 0
        else 1
    )
    raw = os.environ.get(name, "").strip()
    if not raw:
        return positive_default
    try:
        raw_upper = raw.upper()
        if raw_upper.endswith(("MIB", "MI", "M")):
            if raw_upper.endswith("MIB"):
                val_str = raw[:-3]
            elif raw_upper.endswith("MI"):
                val_str = raw[:-2]
            else:
                val_str = raw[:-1]
            val = int(val_str.strip())
            if val <= 0:
                return positive_default
            return val * 1024 * 1024
        else:
            val = int(raw)
            if val <= 0:
                return positive_default
            return val
    except (ValueError, AttributeError):
        return positive_default


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Read request body bounded by max_bytes.

    Strategy:
    1. Check Content-Length header (if present and valid) — reject with 413 if over cap
    2. For chunked or missing/misleading Content-Length, consume request.stream()
       incrementally into a bytearray
    3. Stop immediately with 413 once accumulated bytes exceed max_bytes
    4. Preserve exact accumulated bytes for JSON parse and HMAC

    Returns:
    - bytes: the exact accumulated body

    Raises:
    - HTTPException(413): declared or actual size exceeds max_bytes
    - HTTPException(400): malformed negative Content-Length
    """
    # Check Content-Length header first (fail-fast for declared oversize)
    content_length_header = request.headers.get("content-length", "").strip()
    if content_length_header:
        try:
            declared_size = int(content_length_header)
            if declared_size < 0:
                # Malformed negative Content-Length — treat as 400
                raise HTTPException(400, "invalid_content_length_negative")
            if declared_size > max_bytes:
                # Declared size exceeds cap — fail fast with 413
                raise HTTPException(413, "external_scores_body_too_large")
        except ValueError:
            # Unparseable Content-Length — fall through to stream consumption
            pass

    # Consume stream incrementally (handles chunked, missing, or misleading Content-Length)
    accumulated = bytearray()
    async for chunk in request.stream():
        accumulated.extend(chunk)
        if len(accumulated) > max_bytes:
            # Exceeded cap during streaming — fail with 413
            raise HTTPException(413, "external_scores_body_too_large")

    return bytes(accumulated)


def _cnf_token_secret(service_role: str) -> bytes:
    raw = (
        os.environ.get(_CNF_TOKEN_SECRET_ENV, "").lstrip("\ufeff").strip()
        or os.environ.get("CATHEDRAL_PUBLISHER_SEED_SECRET", "").lstrip("\ufeff").strip()
    )
    if raw:
        return hashlib.sha256(raw.encode("utf-8")).digest()
    if service_role == "submit":
        raise RuntimeError(
            f"{_CNF_TOKEN_SECRET_ENV} is required when CATHEDRAL_SERVICE_ROLE=submit"
        )
    # Local/dev fallback. Production split roles must set a stable secret.
    print(f"[cnf] WARNING: {_CNF_TOKEN_SECRET_ENV} is unset; "
          "active-cnf tokens are process-local and unsafe for split replicas")
    return secrets.token_bytes(32)


def _public_cnf_url(path: str) -> str:
    base = os.environ.get(_CNF_PUBLIC_BASE_URL_ENV, "").lstrip("\ufeff").strip().rstrip("/")
    return f"{base}{path}" if base else path


def _weights_vector_expired(vec: Any, now_epoch_ms: float | None = None) -> bool:
    """Fail-closed expiry check for the signed weight vector.

    Returns True (do NOT serve as 200) when the vector is missing a usable,
    future ``expires_at`` — i.e. expired, absent, or unparseable. Used by the
    origin ``/v1/validator/weights/next`` handler so a wedged refresh can never
    serve an expired signed vector as success (the edge worker enforces the same
    rule; this is origin-side defense-in-depth).
    """
    from datetime import datetime, timezone

    exp = vec.get("expires_at") if isinstance(vec, dict) else None
    if not isinstance(exp, str) or not exp:
        return True
    try:
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    now_dt = (
        datetime.now(timezone.utc) if now_epoch_ms is None
        else datetime.fromtimestamp(now_epoch_ms / 1000.0, timezone.utc))
    return now_dt >= exp_dt


class _SoftTtlCache:
    """Serve stale cached visibility payloads while one background refresh runs.

    States returned by get(): ``hit`` (fresh successful value), ``stale``
    (serving a previously-successful value while a refresh runs), ``warming``
    (cold placeholder, first real build in progress), ``degraded`` (refresh is
    failing and backing off — serving last-known-good if any, else the cold
    placeholder), ``cold`` (synchronous first build).

    Failure handling (the 2026-06-29 fix): a failed background refresh must NOT
    (a) freeze a cold ``warming`` placeholder forever, nor (b) re-trigger a build
    on every request. On failure we preserve the last-known-good value and stamp
    ``last_attempt_at`` so the next refresh is gated behind ``retry_backoff_secs``;
    the status flips ``warming -> degraded`` so callers can report honest state
    instead of an eternal "warming".
    """

    def __init__(
        self, name: str, ttl_secs: float, retry_backoff_secs: float | None = None
    ) -> None:
        self.name = name
        self.ttl_secs = max(0.0, ttl_secs)
        # Don't hammer the DB on every request when a build keeps failing.
        self.retry_backoff_secs = (
            max(self.ttl_secs * 2.0, 10.0)
            if retry_backoff_secs is None else max(0.0, retry_backoff_secs))
        # A refresh thread that hangs (never returns AND never raises) would leave
        # refreshing=True forever and block all future refreshes. After this long
        # in-flight we abandon the (daemon) thread and allow a new attempt.
        self.max_inflight_secs = max(self.retry_backoff_secs * 3.0, 30.0)
        self._lock = threading.Lock()
        self._entries: dict[Any, dict[str, Any]] = {}

    def _maybe_spawn(self, key: Any, builder, entry: dict[str, Any], now: float) -> None:
        """Spawn a refresh iff not already running and past the backoff window.
        Caller must hold self._lock."""
        if entry.get("refreshing"):
            # Block a concurrent refresh — unless the in-flight one has been
            # running long enough to be considered hung, in which case we abandon
            # it (daemon) and allow a fresh attempt.
            started = float(entry.get("refresh_started_at", 0.0))
            if (now - started) < self.max_inflight_secs:
                return
        # Healthy stale->refresh is immediate (preserves normal SWR cadence).
        # Backoff only applies once a build has started failing, so a failing
        # refresh can't be retried on every single request.
        if int(entry.get("failures", 0)) > 0 and \
                (now - float(entry.get("last_attempt_at", 0.0))) < self.retry_backoff_secs:
            return
        entry["refreshing"] = True
        entry["last_attempt_at"] = now
        entry["refresh_started_at"] = now
        threading.Thread(
            target=self._refresh, args=(key, builder),
            name=f"{self.name}-refresh", daemon=True,
        ).start()

    def get(
        self,
        key: Any,
        builder,
        *,
        cold_async: bool = False,
        cold_value=None,
    ) -> tuple[Any, str]:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                fresh = entry.get("has_success") and (
                    self.ttl_secs <= 0
                    or (now - float(entry["built_at"])) <= self.ttl_secs)
                if fresh:
                    return entry["value"], "hit"
                # Not fresh: try to refresh (gated by backoff), serve current value.
                self._maybe_spawn(key, builder, entry, now)
                if entry.get("has_success"):
                    status = "stale" if entry.get("refreshing") else "degraded"
                else:
                    status = "warming" if entry.get("refreshing") else "degraded"
                return entry["value"], status

            if cold_async:
                value = cold_value() if callable(cold_value) else cold_value
                self._entries[key] = {
                    "value": value,
                    "built_at": 0.0,
                    "refreshing": True,
                    "last_attempt_at": now,
                    "refresh_started_at": now,
                    "failures": 0,
                    "has_success": False,
                }
                threading.Thread(
                    target=self._refresh, args=(key, builder),
                    name=f"{self.name}-cold-refresh", daemon=True,
                ).start()
                return value, "warming"

        value = builder()
        with self._lock:
            self._entries[key] = {
                "value": value,
                "built_at": time.monotonic(),
                "refreshing": False,
                "last_attempt_at": time.monotonic(),
                "failures": 0,
                "has_success": True,
            }
        return value, "cold"

    def _refresh(self, key: Any, builder) -> None:
        try:
            value = builder()
            with self._lock:
                self._entries[key] = {
                    "value": value,
                    "built_at": time.monotonic(),
                    "refreshing": False,
                    "last_attempt_at": time.monotonic(),
                    "failures": 0,
                    "has_success": True,
                }
        except Exception as exc:
            print(f"[visibility_cache] refresh_failed name={self.name} key={key!r} error={exc!r}")
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None:
                    # Preserve last-known-good value/built_at/has_success; just
                    # clear the in-flight flag and stamp the attempt so the next
                    # refresh is gated by retry_backoff_secs (no per-request storm).
                    entry["refreshing"] = False
                    entry["failures"] = int(entry.get("failures", 0)) + 1
                    entry["last_attempt_at"] = time.monotonic()


def _now_iso_ms() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now_iso_ms_plus(secs: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=secs)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_iso(ts: str) -> float | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


_SERVICE_ROLES = {"all", "read", "submit", "worker"}


def _service_role_from_env() -> str:
    raw = os.environ.get("CATHEDRAL_SERVICE_ROLE", "all").strip().lower() or "all"
    if raw not in _SERVICE_ROLES:
        raise RuntimeError(f"invalid CATHEDRAL_SERVICE_ROLE={raw!r}")
    return raw


def _role_runs_worker(role: str) -> bool:
    return role in {"all", "worker"}


def _role_runs_read_background(role: str) -> bool:
    return role in {"all", "read"}


def _role_serves_reads(role: str) -> bool:
    # "read" serves the public board/leaderboard/weights surface; "all" still
    # serves the same routes in the single-service deploy. Both must run with a
    # bounded Postgres statement timeout so a single slow board query (the
    # /v1/leaderboard/recent 30-46s scans seen in prod) cannot pin a pool
    # connection and starve every other reader.
    return role in {"all", "read"}


def _statement_timeout_guard_warning(role: str, raw_value: str | None) -> str | None:
    """Return a loud warning string if a read-serving role boots without a
    positive CATHEDRAL_PG_STATEMENT_TIMEOUT_MS, else None.

    Unset or 0 (Postgres' "no timeout" sentinel) is the exact missing value that
    let /v1/leaderboard/recent run 30-46s and exhaust the connection pool. We
    warn rather than hard-fail so an operator can still boot in an emergency.
    """
    if not _role_serves_reads(role):
        return None
    try:
        value = int((raw_value or "0").strip() or "0")
    except ValueError:
        value = 0
    if value > 0:
        return None
    return (
        "[runtime] WARNING: CATHEDRAL_PG_STATEMENT_TIMEOUT_MS is unset/0 for "
        f"read-serving role {role!r}. A single slow board query (e.g. "
        "/v1/leaderboard/recent at 30-46s) can pin pool connections and take "
        "the read origin down. Set CATHEDRAL_PG_STATEMENT_TIMEOUT_MS=4000."
    )


def _check_read_statement_timeout(role: str) -> None:
    warning = _statement_timeout_guard_warning(
        role, os.environ.get("CATHEDRAL_PG_STATEMENT_TIMEOUT_MS")
    )
    if warning:
        print(warning)


def build_app(
    *,
    database_path: str = ":memory:",
    signing_key_hex: str | None = None,
    submit_min_interval_secs: int | None = None,
) -> FastAPI:
    from . import launch_profile
    _profile_errors = launch_profile.validate_env(
        signing_key_hex_provided=signing_key_hex is not None)
    if _profile_errors:
        raise RuntimeError(
            "launch profile misconfiguration (fail-closed): "
            + "; ".join(_profile_errors))
    key_hex = signing_key_hex or keys.load_signing_key()
    pub_hex = rows.public_key_hex(key_hex)
    weight_policy_key_hex = os.environ.get(weights_mod.SIGNING_KEY_ENV, "").strip()
    try:
        jwks_doc = rows.jwks_from_key(
            key_hex,
            weight_policy_private_key_hex=weight_policy_key_hex or None,
            weight_policy_kid=os.environ.get(
                weights_mod.KEY_ID_ENV, "cathedral-weight-policy"),
        )
    except ValueError as exc:
        if weight_policy_key_hex:
            raise ValueError(
                f"invalid {weights_mod.SIGNING_KEY_ENV}: expected a 32-byte "
                "Ed25519 private key encoded as hex"
            ) from exc
        raise
    store = Store(database_path)
    v2_database_path = (
        os.environ.get("CATHEDRAL_V2_DATABASE_URL", "").strip()
        or os.environ.get("CATHEDRAL_V2_DB_PATH", "").strip()
    )
    def _build_v2_store(path: str) -> Store:
        # Store's Postgres pool knobs are legacy/global. Map V2-prefixed pool
        # knobs only for this constructor call so a beta stack does not require
        # setting generic CATHEDRAL_PG_POOL_* env.
        mapped = {
            "CATHEDRAL_PG_POOL_MIN": "CATHEDRAL_V2_PG_POOL_MIN",
            "CATHEDRAL_PG_POOL_MAX": "CATHEDRAL_V2_PG_POOL_MAX",
            "CATHEDRAL_PG_CONNECT_TIMEOUT": "CATHEDRAL_V2_PG_CONNECT_TIMEOUT",
        }
        old: dict[str, str | None] = {}
        try:
            for legacy, v2_name in mapped.items():
                if v2_name in os.environ:
                    old[legacy] = os.environ.get(legacy)
                    os.environ[legacy] = os.environ[v2_name]
            return Store(path, prefer_env_database_url=False)
        finally:
            for legacy, value in old.items():
                if value is None:
                    os.environ.pop(legacy, None)
                else:
                    os.environ[legacy] = value

    # If configured, V2 uses a physically separate DB/store so beta deploys and
    # live-adjacent tests cannot mutate the current subnet payout DB. If unset,
    # local tests share the app store.
    v2_store = _build_v2_store(v2_database_path) if v2_database_path else store
    if (launch_profile.converged() and store.backend != "postgres"
            and "PYTEST_CURRENT_TEST" not in os.environ):
        # Two deployment processes with a SQLite fallback would silently stop
        # sharing the scoring/V2 store (DATABASE_URL unset or malformed).
        # Payout-critical: fail closed outside tests.
        raise RuntimeError(
            "launch profile v2-converged requires a shared Postgres store: "
            "set DATABASE_URL (postgresql://...); refusing SQLite fallback")
    if v2_pipeline.pm_payout_bridge_enabled() and v2_database_path:
        # The bridge records per_miner_solves rows via the verify worker's store
        # handle (the V2 store). Scoring reads the MAIN store. With a split V2
        # DB the bridged payout rows would be invisible to weights -- miners
        # would verify but never earn. Payout code fails closed: refuse to boot.
        raise RuntimeError(
            "CATHEDRAL_V2_PM_PAYOUT_BRIDGE requires V2 to share the main store: "
            "unset CATHEDRAL_V2_DATABASE_URL/CATHEDRAL_V2_DB_PATH or disable "
            "the bridge. Bridged per_miner_solves rows in a split V2 DB never "
            "reach the payout store (miners verify but never earn)."
        )
    v2_blob_store = blob_store_mod.store_from_env()
    # Hippius presign client for flat results-file pushes. None when env is
    # unset; gated in results_publisher so missing config is always a no-op.
    v2_hip = hippius_presign_mod.HippiusPresign.from_env()
    # Startup-time env pinning: copy the V2 per-miner env onto the legacy names
    # once, while build_app is still single-threaded, so the V2 per-miner
    # handlers and verify worker skip v2_pm_env()'s process-global lock (it
    # serialized every V2 per-miner request to one-at-a-time per process).
    # Implied by CATHEDRAL_LAUNCH_PROFILE=v2-converged, with an explicit opt-in
    # still available for surgical rollout. Refusal reasons are logged inside
    # pin_v2_pm_env().
    v2_pm_env_pinned = v2_pipeline.pin_v2_pm_env()
    print(f"[v2_pm_env] pinned={v2_pm_env_pinned}")
    # Best-effort hotkey->coldkey resolver for the public receipts feed,
    # reusing the existing metagraph-backed coldkey_map table (see
    # weights._load_coldkey_map). Built once per app instance so its internal
    # 10-minute cache is actually shared across requests.
    v2_receipts_coldkey_resolver = v2_receipts.make_coldkey_resolver(v2_store)
    service_role = _service_role_from_env()
    _check_read_statement_timeout(service_role)
    verifier = default_verifier()
    epoch_salt = f"epoch_{datetime.now(timezone.utc):%Y%m%d}:{_FAMILY}"
    arena_registry = SolverRegistry()
    # Shared HMAC secret lets active-cnf and CNF fetch land on different replicas.
    token_secret = _cnf_token_secret(service_role)
    min_interval = (
        submit_min_interval_secs
        if submit_min_interval_secs is not None
        else int(os.environ.get("CATHEDRAL_SUBMIT_MIN_INTERVAL_SECS", "0"))
    )
    # in-process per-(hotkey, challenge) last-submit clock for rate limiting
    last_submit: dict[tuple[str, str], float] = {}
    last_submit_lock = threading.Lock()
    try:
        configured_submit_max_concurrency = int(os.environ.get(
            "CATHEDRAL_SUBMIT_MAX_CONCURRENCY", "24") or "0")
    except ValueError:
        configured_submit_max_concurrency = 24
    try:
        submit_hard_cap = int(os.environ.get("CATHEDRAL_SUBMIT_HARD_CAP", "8") or "0")
    except ValueError:
        submit_hard_cap = 8
    submit_max_concurrency = configured_submit_max_concurrency
    # Track 2 (item 7): the global concurrency cap is a *saturation* gate, not a
    # fairness throttle. Once admission is cheap (async verify) it can be raised
    # high, with the per-hotkey limiter (below) preventing any single miner from
    # starving the rest. DEFAULT OFF: the legacy hard-cap clamp stays in force
    # unless an operator explicitly lifts it, so live behaviour is unchanged.
    submit_hard_cap_bypass = _env_bool("CATHEDRAL_SUBMIT_HARD_CAP_BYPASS", False)
    if submit_hard_cap > 0 and submit_max_concurrency > 0 and not submit_hard_cap_bypass:
        submit_max_concurrency = min(submit_max_concurrency, submit_hard_cap)
    submit_gate = (
        threading.BoundedSemaphore(submit_max_concurrency)
        if submit_max_concurrency > 0 else None
    )
    configured_pm_read_hard_cap = _env_int("CATHEDRAL_PM_READ_HARD_CAP", 128)
    pm_read_min_cap = _env_int("CATHEDRAL_PM_READ_MIN_CAP", 128)
    pm_read_hard_cap = (
        0 if configured_pm_read_hard_cap <= 0
        else configured_pm_read_hard_cap
    )
    submit_log_events = os.environ.get("CATHEDRAL_SUBMIT_LOG_EVENTS", "").strip().lower() in {
        "1", "true", "yes", "on"}
    # Phase 4/5 durable admission. DEFAULT OFF: when off, /v1/agents/submit keeps
    # its legacy synchronous 200 ranked/rejected contract verbatim (no behaviour
    # change for live miners/validators). When on, the PUBLIC lane returns 202 +
    # a durable receipt and an async worker verifies later (see submit_admission /
    # verify_worker). Older clients can still force the legacy path per-request
    # with `X-Cathedral-Submit-Mode: sync`.
    submit_async_enabled = _env_bool("CATHEDRAL_SUBMIT_ASYNC_ENABLED", False)
    # TRACK 1 durable admission for the PRIVATE (pm-*) lane. DEFAULT OFF and gated
    # behind BOTH this flag AND the shared async-verify worker flag — when either is
    # unset the pm-* submit branch keeps its byte-for-byte inline synchronous
    # contract. When on, the pm-* branch does only cheap checks inline (signature,
    # body-size, ownership/recovery, idempotency) and returns 202 + a receipt; the
    # async worker re-materializes the miner's own CNF, runs the DIMACS + anti-copy
    # checks, and writes the terminal accept/reject to the SAME ledger scoring reads.
    #   SHADOW (CATHEDRAL_PM_ASYNC_SHADOW): the inline path stays authoritative for
    #   payout and the async path runs in parallel into shadow_* columns only, so
    #   go-live can prove async-vs-inline parity before cutover (no payout change).
    from . import verify_worker as _vw_flags
    pm_submit_async_enabled = (
        submit_async_enabled and _vw_flags.pm_async_enabled())
    pm_async_shadow_enabled = (
        pm_submit_async_enabled and _vw_flags.pm_async_shadow())
    # Body-size limits for the cheap async admission checks.
    submit_max_solution_bytes = _env_int(
        "CATHEDRAL_SUBMIT_MAX_SOLUTION_BYTES", 1_000_000)
    pm_submit_max_solution_bytes = _env_int(
        "CATHEDRAL_PM_SUBMIT_MAX_SOLUTION_BYTES", 1_000_000)
    # V2 off-chain manifest submit is phase-1/2 only: signature-verified,
    # durable, no payout/scoring until workers are wired. Default off so deploys
    # are inert unless explicitly enabled.
    solution_manifest_enabled = _env_bool(
        "CATHEDRAL_V2_ENABLED", launch_profile.converged())
    solution_manifest_max_bytes = _env_int("CATHEDRAL_V2_MAX_SOLUTION_BYTES", 0)
    solution_blob_upload_enabled = _env_bool(
        "CATHEDRAL_V2_BLOB_UPLOAD_ENABLED", solution_manifest_enabled)
    solution_blob_upload_max_bytes = _env_int(
        "CATHEDRAL_V2_BLOB_UPLOAD_MAX_BYTES", 5_000_000)
    # Durable inline copy threshold. The local blob dir is per-container /tmp, so
    # the async verify worker (a different container) cannot read what the web
    # container wrote — a redeploy or cross-container fetch loses the bytes and
    # the row poison-loops on blob_fetch_failed forever. Inline every solution we
    # accept for blob upload so verification never depends on the local blob
    # store. Defaults to the upload max so coverage always matches what we admit.
    solution_inline_max_bytes = _env_int(
        "CATHEDRAL_V2_INLINE_MAX_BYTES", solution_blob_upload_max_bytes)
    v2_shadow_v1_enabled = _env_bool("CATHEDRAL_V2_SHADOW_V1_ENABLED", False)
    v2_shadow_v1_max_solution_bytes = _env_int(
        "CATHEDRAL_V2_SHADOW_V1_MAX_SOLUTION_BYTES", solution_blob_upload_max_bytes)
    v2_submit_bitset_enabled = _env_bool(
        "CATHEDRAL_V2_SUBMIT_BITSET_ENABLED", launch_profile.converged())
    # V1-style lazy issuance for the V2 challenges page: descriptors only, no
    # CNF generation or token minting at listing time. The miner gets the
    # (time-bound) submit token, actual nvars, and cnf_sha256 from the CNF
    # fetch headers -- it must fetch the CNF to solve it anyway. This removes
    # the per-page CPU cost that melted the origin on 2026-07-08. Default off
    # (on under the v2-converged launch profile) so existing page-token
    # clients keep working until they migrate.
    v2_lazy_issuance = _env_bool(
        "CATHEDRAL_V2_LAZY_ISSUANCE", launch_profile.converged())
    v2_submit_token_secret = os.environ.get("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "").strip()
    v2_submit_token_ttl_secs = max(1, _env_int("CATHEDRAL_V2_SUBMIT_TOKEN_TTL_SECS", 300))
    v2_submit_token_allowlist = {
        item.strip()
        for item in os.environ.get("CATHEDRAL_V2_SUBMIT_TOKEN_ALLOWLIST", "").replace("\n", ",").split(",")
        if item.strip()
    }
    # Phase 2 immutable CNF delivery.  Explicit rollout gate: the legacy
    # body-plus-token endpoint remains authoritative until current+next epoch
    # artifacts have been published and the operator enables metadata access.
    v2_cnf_artifacts_enabled = _env_bool(
        "CATHEDRAL_V2_CNF_ARTIFACTS_ENABLED", False)
    v2_submit_bitset_max_body_bytes = max(
        1024, _env_int("CATHEDRAL_V2_SUBMIT_BITSET_MAX_BODY_BYTES", 16_384))
    v2_submit_backpressure_enabled = _env_bool(
        "CATHEDRAL_V2_SUBMIT_BACKPRESSURE_ENABLED", False)
    v2_submit_backpressure_max_pending = max(
        0, _env_int("CATHEDRAL_V2_SUBMIT_BACKPRESSURE_MAX_PENDING", 0))
    v2_submit_backpressure_max_oldest_age_secs = max(
        0.0, _env_float("CATHEDRAL_V2_SUBMIT_BACKPRESSURE_MAX_OLDEST_AGE_SECS", 0.0))
    v2_submit_backpressure_retry_after_secs = max(
        1, _env_int("CATHEDRAL_V2_SUBMIT_BACKPRESSURE_RETRY_AFTER_SECS", 5))
    # Front-door shed for DB connection pressure (open-v2 incident 2026-07-08):
    # under real all-miner load, submit admission and receipt polling exhausted
    # PG connection acquisition (psycopg2.OperationalError from psycopg2.connect
    # inside _pool.getconn) and surfaced as raw 500s + readiness flaps. When the
    # DB is briefly unreachable/saturated, return a controlled 503 with a
    # distinct reason + Retry-After instead of an unhandled OperationalError.
    v2_db_unavailable_retry_after_secs = max(
        1, _env_int("CATHEDRAL_V2_DB_UNAVAILABLE_RETRY_AFTER_SECS", 2))
    # Receipt pollers hammer the origin immediately after submit and every poll
    # is a DB read; a small dedicated concurrency gate keeps a poll flood from
    # exhausting the PG pool before the shed above ever fires. 0 disables.
    v2_receipt_poll_max_concurrency = max(
        0, _env_int("CATHEDRAL_V2_RECEIPT_POLL_MAX_CONCURRENCY", 16))
    # /v2/agents/submit-bitset is an async handler, but the verify+admit body it
    # runs is sync CPU (CNF regeneration, witness check) + sync DB. Running that
    # inline on the event loop froze the whole worker (health checks, connection
    # accepts, all in-flight I/O) whenever a submit waited behind a slow page
    # fetch under the shared per-miner env lock. Offload the blocking section to
    # a DEDICATED pool: not asyncio.to_thread (its default executor also runs the
    # in-process verify worker's heartbeat -> a submit burst could starve it and
    # trip the stale-lock steal), and not the anyio threadpool (shared with the
    # sync challenges//cnf handlers -> submits would queue behind page floods).
    v2_submit_bitset_threads = max(
        1, _env_int("CATHEDRAL_V2_SUBMIT_BITSET_THREADS", 8))
    v2_submit_executor = ThreadPoolExecutor(
        max_workers=v2_submit_bitset_threads, thread_name_prefix="v2-submit")
    # Dedicated executor for the per-miner challenges/cnf READ path. The item loop
    # (generate/read-through + parse_cnf + sha + mint_token, x page size) is CPU
    # heavy; running it in the shared anyio threadpool lets a challenges flood
    # saturate every thread so /health, /submit and everything else queue behind
    # it (observed live: 502/503/timeout while the DB pool sat idle — worker/thread
    # starvation, not DB). A separate bounded pool isolates read load so the rest
    # of the origin stays responsive under a page flood.
    v2_read_threads = max(1, _env_int("CATHEDRAL_V2_READ_THREADS", 6))
    v2_read_executor = ThreadPoolExecutor(
        max_workers=v2_read_threads, thread_name_prefix="v2-read")
    v2_worker_enabled = _env_bool("CATHEDRAL_V2_VERIFY_WORKER_ENABLED", False)
    v2_worker_batch_size = max(1, _env_int("CATHEDRAL_V2_VERIFY_BATCH_SIZE", 8))
    v2_worker_interval_secs = max(
        0.1, _env_float("CATHEDRAL_V2_VERIFY_INTERVAL_SECS", 1.0))
    v2_worker_lock_secs = max(
        1.0, _env_float("CATHEDRAL_V2_VERIFY_LOCK_SECS", 120.0))
    v2_worker_max_blob_bytes = _env_int(
        "CATHEDRAL_V2_VERIFY_MAX_BLOB_BYTES", solution_blob_upload_max_bytes)
    v2_worker_parallel_claims = _env_bool("CATHEDRAL_V2_VERIFY_PARALLEL_CLAIMS", False)
    submit_queue_backpressure_enabled = _env_bool(
        "CATHEDRAL_SUBMIT_QUEUE_BACKPRESSURE_ENABLED", False)
    submit_queue_backpressure = (
        {
            "max_pending": _env_int("CATHEDRAL_SUBMIT_QUEUE_MAX_PENDING", 0),
            "max_worker_lag_secs": _env_float(
                "CATHEDRAL_SUBMIT_QUEUE_MAX_WORKER_LAG_SECS", 0.0),
            "worker_stale_secs": _env_float(
                "CATHEDRAL_SUBMIT_QUEUE_WORKER_STALE_SECS",
                max(10.0, float(_vw_flags.lock_secs()))),
        }
        if submit_queue_backpressure_enabled else None
    )
    submit_queue_backpressure_retry_after = max(
        1, _env_int("CATHEDRAL_SUBMIT_QUEUE_BACKPRESSURE_RETRY_AFTER_SECS", 5))
    # Async admission is only safe when at least one verifier worker is alive.
    # Otherwise miners get durable 202 receipts that never drain to payout.
    submit_async_require_worker = _env_bool(
        "CATHEDRAL_SUBMIT_ASYNC_REQUIRE_WORKER", True)
    submit_async_worker_stale_secs = _env_float(
        "CATHEDRAL_SUBMIT_ASYNC_WORKER_STALE_SECS",
        max(10.0, float(_vw_flags.lock_secs())))
    submit_async_worker_ready_cache_secs = max(
        0.1, _env_float("CATHEDRAL_SUBMIT_ASYNC_WORKER_READY_CACHE_SECS", 1.0))
    submit_async_worker_retry_after = max(
        1, _env_int("CATHEDRAL_SUBMIT_ASYNC_WORKER_RETRY_AFTER_SECS", 5))
    submit_async_worker_cache: dict[str, Any] = {
        "expires_at": 0.0,
        "ready": False,
        "metrics": {"active_workers": 0},
    }
    # Track 2 (item 7): per-hotkey fairness limiter — abuse control distinct from
    # the global saturation gate. DEFAULT OFF (see per_hotkey_limit.py); when off
    # `allow()` is a no-op so live behaviour is unchanged. Rejections from this
    # limiter use the distinct reason `abuse_rate_limited`.
    per_hotkey_limiter = PerHotkeyLimiter(per_hotkey_config_from_env())
    # Phase 3: short bounded wait before returning submit_busy_retry (seconds).
    submit_busy_wait_secs = max(
        0.0, min(2.0, float(os.environ.get("CATHEDRAL_SUBMIT_BUSY_WAIT_SECS", "0.35") or "0")))
    submit_metrics_lock = threading.Lock()
    submit_metrics: dict[str, Any] = {
        "started_at_iso": _now_iso_ms(),
        "max_concurrency": submit_max_concurrency,
        "configured_max_concurrency": configured_submit_max_concurrency,
        "hard_cap": submit_hard_cap,
        "pm_read_hard_cap": pm_read_hard_cap,
        "configured_pm_read_hard_cap": configured_pm_read_hard_cap,
        "pm_read_min_cap": pm_read_min_cap,
        "v2_receipt_poll_max_concurrency": v2_receipt_poll_max_concurrency,
        "min_interval_secs": min_interval,
        "total": 0,
        "by_outcome": {},
        "by_reason": {},
        "by_kind": {},
        "recent": [],
    }

    def _challenge_kind(challenge_id: str | None) -> str:
        if not challenge_id:
            return "unknown"
        return "per_miner" if challenge_id.startswith("pm-") else "public"

    def _record_submit_event(
        outcome: str,
        reason: str,
        *,
        challenge_id: str | None = None,
        status_code: int | None = None,
        log: bool = False,
    ) -> None:
        kind = _challenge_kind(challenge_id)
        event = {
            "ts": _now_iso_ms(),
            "outcome": outcome,
            "reason": reason,
            "kind": kind,
            "status_code": status_code,
        }
        with submit_metrics_lock:
            submit_metrics["total"] = int(submit_metrics["total"]) + 1
            for bucket, key in (
                ("by_outcome", outcome),
                ("by_reason", reason),
                ("by_kind", kind),
            ):
                values = submit_metrics[bucket]
                values[key] = int(values.get(key, 0)) + 1
            recent = submit_metrics["recent"]
            recent.append(event)
            del recent[:-25]
        if log and submit_log_events:
            print("[submit] " + json.dumps(event, sort_keys=True))

    def _raise_submit_queue_backpressure(
        challenge_id: str, decision: dict[str, Any]
    ) -> None:
        reason = str(decision.get("reason") or "submit_queue_backpressure")
        _record_submit_event(
            "rate_limited",
            reason,
            challenge_id=challenge_id,
            status_code=503,
            log=True,
        )
        raise HTTPException(
            503,
            {
                "detail": "submit_queue_backpressure",
                "reason": reason,
                "pending": decision.get("pending"),
                "worker_lag_secs": decision.get("worker_lag_secs"),
            },
            headers={
                "Retry-After": str(submit_queue_backpressure_retry_after),
                "X-Cathedral-Rejection-Reason": "submit_queue_backpressure",
            },
        )

    def _async_worker_ready() -> tuple[bool, dict[str, Any]]:
        if not submit_async_require_worker:
            return True, {"active_workers": "not_required"}
        now_mono = time.monotonic()
        cached_until = float(submit_async_worker_cache.get("expires_at") or 0.0)
        if now_mono < cached_until:
            return (
                bool(submit_async_worker_cache.get("ready")),
                dict(submit_async_worker_cache.get("metrics") or {}),
            )
        metrics = submit_admission.worker_metrics(
            store, now_iso=_now_iso_ms(),
            stale_secs=submit_async_worker_stale_secs)
        ready = int(metrics.get("active_workers") or 0) > 0
        submit_async_worker_cache.update({
            "expires_at": now_mono + submit_async_worker_ready_cache_secs,
            "ready": ready,
            "metrics": metrics,
        })
        return ready, metrics

    def _require_async_worker_ready(challenge_id: str) -> None:
        ready, metrics = _async_worker_ready()
        if ready:
            return
        _record_submit_event(
            "rate_limited",
            "async_worker_unavailable",
            challenge_id=challenge_id,
            status_code=503,
            log=True,
        )
        raise HTTPException(
            503,
            {
                "detail": "async_worker_unavailable",
                "active_workers": int(metrics.get("active_workers") or 0),
                "stale_after_secs": metrics.get("stale_after_secs"),
            },
            headers={
                "Retry-After": str(submit_async_worker_retry_after),
                "X-Cathedral-Rejection-Reason": "async_worker_unavailable",
            },
        )

    def _submit_metrics_snapshot() -> dict[str, Any]:
        with submit_metrics_lock:
            return {
                "started_at_iso": submit_metrics["started_at_iso"],
                "max_concurrency": submit_metrics["max_concurrency"],
                "configured_max_concurrency": submit_metrics["configured_max_concurrency"],
                "hard_cap": submit_metrics["hard_cap"],
                "pm_read_hard_cap": submit_metrics["pm_read_hard_cap"],
                "configured_pm_read_hard_cap": submit_metrics["configured_pm_read_hard_cap"],
                "pm_read_min_cap": submit_metrics["pm_read_min_cap"],
                "v2_receipt_poll_max_concurrency": submit_metrics["v2_receipt_poll_max_concurrency"],
                "min_interval_secs": submit_metrics["min_interval_secs"],
                "total": submit_metrics["total"],
                "by_outcome": dict(submit_metrics["by_outcome"]),
                "by_reason": dict(submit_metrics["by_reason"]),
                "by_kind": dict(submit_metrics["by_kind"]),
                "recent": list(submit_metrics["recent"]),
            }

    # ---- HTTP status telemetry (5xx rates) --------------------------------
    # Process-wide response-status counters, fed by a cheap ASGI middleware that
    # only reads the http.response.start status (no body buffering). Surfaced on
    # the validator-health endpoint so an operator/release gate can see the 5xx
    # rate without scraping logs. Per-class totals are cumulative since process
    # start; the weight-feed route is tracked separately because a single 5xx
    # there is the highest-severity signal in the system.
    http_status_lock = threading.Lock()
    _WEIGHTS_FEED_SUFFIX = "/v1/validator/weights/next"
    http_status_metrics: dict[str, Any] = {
        "started_at_iso": _now_iso_ms(),
        "total": 0,
        "by_class": {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0},
        "weights_feed_total": 0,
        "weights_feed_5xx": 0,
        "recent_5xx": [],
    }

    def _record_http_status(path: str, status: int | None) -> None:
        if not isinstance(status, int):
            return
        klass = f"{status // 100}xx"
        is_weights_feed = path.endswith(_WEIGHTS_FEED_SUFFIX)
        with http_status_lock:
            http_status_metrics["total"] = int(http_status_metrics["total"]) + 1
            by_class = http_status_metrics["by_class"]
            by_class[klass] = int(by_class.get(klass, 0)) + 1
            if is_weights_feed:
                http_status_metrics["weights_feed_total"] = (
                    int(http_status_metrics["weights_feed_total"]) + 1)
                if status >= 500:
                    http_status_metrics["weights_feed_5xx"] = (
                        int(http_status_metrics["weights_feed_5xx"]) + 1)
            if status >= 500:
                recent = http_status_metrics["recent_5xx"]
                recent.append({"ts": _now_iso_ms(), "path": path, "status": status})
                del recent[:-25]

    def _http_status_snapshot() -> dict[str, Any]:
        with http_status_lock:
            by_class = dict(http_status_metrics["by_class"])
            total = int(http_status_metrics["total"])
            count_5xx = int(by_class.get("5xx", 0))
            wf_total = int(http_status_metrics["weights_feed_total"])
            wf_5xx = int(http_status_metrics["weights_feed_5xx"])
            recent_5xx = list(http_status_metrics["recent_5xx"])
        return {
            "started_at_iso": http_status_metrics["started_at_iso"],
            "total": total,
            "by_class": by_class,
            "rate_5xx": round(count_5xx / total, 6) if total else 0.0,
            "weights_feed_total": wf_total,
            "weights_feed_5xx": wf_5xx,
            "weights_feed_rate_5xx": round(wf_5xx / wf_total, 6) if wf_total else 0.0,
            "recent_5xx": recent_5xx,
        }

    def _submit_slot():
        if submit_gate is None:
            yield
            return
        # Phase 3: a brief bounded wait turns most transient overlaps into an
        # accepted submit instead of an instant miner-facing 429. The hard ceiling
        # is preserved — we still reject after the wait, just less often.
        #
        # LEAK FIX (same class as the read gate): the acquired slot must be
        # released on EVERY exit path, including a cancel that fires between the
        # successful acquire() and the try/yield below. Hold the slot in a single
        # try that spans the reject-check and the yield so a disconnect can never
        # leak it (a leak drains the BoundedSemaphore and 429s spuriously on an
        # idle origin — observed as 2/20 sequential submit 429s before this fix).
        acquired = False
        try:
            acquired = (
                submit_gate.acquire(timeout=submit_busy_wait_secs)
                if submit_busy_wait_secs > 0 else submit_gate.acquire(blocking=False)
            )
            if not acquired:
                _record_submit_event(
                    "rate_limited",
                    "submit_busy_retry",
                    status_code=429,
                    log=True,
                )
                raise HTTPException(
                    429,
                    _retry_after_payload("submit_busy_retry", 1),
                    headers={
                        "Retry-After": "1",
                        "X-Cathedral-Rejection-Reason": "submit_busy_retry",
                    },
                )
            yield
        finally:
            if acquired:
                submit_gate.release()

    def _submit_rate_limited(rl_key: tuple[str, str], now: float) -> bool:
        if min_interval <= 0:
            return False
        with last_submit_lock:
            prev = last_submit.get(rl_key)
        return prev is not None and (now - prev) < min_interval

    def _remember_submit(rl_key: tuple[str, str], now: float) -> None:
        with last_submit_lock:
            last_submit[rl_key] = now
            if len(last_submit) > 50_000:
                horizon = now - max(min_interval, 3600.0)
                for k in [k for k, t in last_submit.items() if t < horizon]:
                    last_submit.pop(k, None)

    # ---- broadcast tier (KEYSTONE TASK 3) ---------------------------------
    # CNF bodies → backend-switchable store (db | bucket) with immutable cache
    # headers + the existing HMAC token gate. Board → in-process cached snapshot
    # rebuilt only on mint/retire (+ TTL safety) so miner polls hit memory/edge,
    # never the DB. invalidate_all() (board_cache_mod) is called on every mutation
    # of the active set; reads serve the memoized payload with an ETag for 304s.
    cnf_store = CNFStore(store)
    # Register this app's CNF store so the module-level mint helper (seed_challenge)
    # can push immutable bodies to the bucket backend without an app reference.
    _register_cnf_store(store, cnf_store)

    def _board_distribution(items: list[dict[str, Any]]) -> dict[str, Any]:
        tier_weights = weights_mod.tier_weights()
        by_tier: dict[int, dict[str, Any]] = {}
        total_weighted_units = 0.0
        for item in items:
            tier = int(item.get("tier") or 1)
            weight = float(tier_weights.get(tier, tier_weights.get(1, 1.0)))
            total_weighted_units += weight
            entry = by_tier.setdefault(
                tier,
                {
                    "tier": tier,
                    "count": 0,
                    "score_weight": weight,
                    "weighted_units": 0.0,
                    "num_vars": int(item.get("num_vars") or 0),
                    "num_clauses": int(item.get("num_clauses") or 0),
                },
            )
            entry["count"] += 1
            entry["weighted_units"] = round(float(entry["weighted_units"]) + weight, 6)
        total = len(items)
        for entry in by_tier.values():
            entry["count_share"] = round(entry["count"] / total, 6) if total else 0.0
            entry["weighted_share"] = (
                round(entry["weighted_units"] / total_weighted_units, 6)
                if total_weighted_units > 0.0 else 0.0
            )
        return {
            "total_challenges": total,
            "total_weighted_units": round(total_weighted_units, 6),
            "tiers": [by_tier[tier] for tier in sorted(by_tier)],
        }

    def _generator_status() -> dict[str, Any]:
        from . import refill

        status = refill.generator_config()
        task = getattr(app.state, "refill_task", None)
        status["task_running"] = bool(task is not None and not task.done())
        status["service_role"] = service_role
        return status

    def _build_board() -> dict[str, Any]:
        items = [_challenge_public(r) for r in _active_challenges()]
        return {
            "family_id": _FAMILY,
            "count": len(items),
            "generator": _generator_status(),
            "scoring": {
                "mode": weights_mod.mode(),
                "unit": "distinct_verified_solve_weighted_by_tier",
                "tier_weights": weights_mod.tier_weights(),
            },
            "distribution": _board_distribution(items),
            "items": items,
        }

    board_cache = BoardCache(_build_board)
    board_cache_mod.register(board_cache)

    # Track 3 / item 6: timer-built materialized snapshot of the rendered board.
    # DEFAULT-OFF — only started/served when the feature flag is on (see
    # materialized_snapshot.enabled()). When on, the read is served from the last
    # timer-materialized payload (never builds on the request path); when the
    # snapshot is cold or too stale, get() returns None and the route falls back
    # to the live board_cache path below — degrade to stale-then-live, not error.
    board_snapshot = MaterializedSnapshot("board", _build_board)
    materialized_snapshot_mod.register(board_snapshot)

    def _conditional_response(payload, etag, headers, request: Request):
        inm = request.headers.get("if-none-match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            return Response(status_code=304, headers=headers)
        return JSONResponse(payload, headers=headers)

    def _serve_board_snapshot(request: Request):
        if materialized_snapshot_mod.enabled():
            served = board_snapshot.get()
            if served is not None:
                payload, etag, meta = served
                headers = snapshot_headers(etag, meta)
                headers["X-Cathedral-Board-Rebuilds"] = str(board_cache.rebuild_count)
                return _conditional_response(payload, etag, headers, request)
        # Flag off, or snapshot cold/too-stale: live cache-backed path (unchanged).
        payload, etag = board_cache.get()
        headers = board_cache_headers(etag)
        headers["X-Cathedral-Board-Rebuilds"] = str(board_cache.rebuild_count)
        return _conditional_response(payload, etag, headers, request)

    def _snapshot_bytes(payload: dict[str, Any]) -> bytes:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")

    def _snapshot_hash(payload: dict[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(_snapshot_bytes(payload)).hexdigest()

    def _snapshot_etag(payload: dict[str, Any]) -> str:
        return '"' + hashlib.sha256(_snapshot_bytes(payload)).hexdigest() + '"'

    def _sign_latest_pointer(payload: dict[str, Any]) -> dict[str, Any]:
        signed = dict(payload)
        signed["key_id"] = os.environ.get(
            weights_mod.KEY_ID_ENV, "cathedral-weight-policy")
        signing_key = os.environ.get(weights_mod.SIGNING_KEY_ENV, "").strip() or key_hex
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_key.strip()))
        signed["signature"] = base64.b64encode(
            sk.sign(weights_mod.canonical_bytes(signed))
        ).decode()
        return signed

    def _sat_snapshot_bundle() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
        board_payload, board_etag = board_cache.get()
        weight_key = os.environ.get(weights_mod.SIGNING_KEY_ENV, "").strip() or key_hex
        try:
            weights_payload = weights_mod.current_vector(
                store, signing_key_hex=weight_key
            )
        except weights_mod.VectorNotReady as exc:
            raise HTTPException(
                503,
                {
                    "detail": "signed_weights_warming",
                    "retry_after_seconds": 1,
                },
                headers={"Retry-After": "1"},
            ) from exc
        board_hash = _snapshot_hash(board_payload)
        weights_hash = _snapshot_hash(weights_payload)
        policy_version = str(weights_payload.get("policy_version") or int(time.time() * 1000))
        sequence_digest = hashlib.sha256(
            f"{policy_version}:{board_hash}:{weights_hash}".encode("utf-8")
        ).hexdigest()[:12]
        sequence = f"{policy_version}-{sequence_digest}"
        created_at = str(weights_payload.get("generated_at") or _now_iso_ms())
        board_url = f"/sat/sequences/{sequence}/board.json"
        weights_url = f"/sat/sequences/{sequence}/weights.json"
        pointer = {
            "schema": "cathedral.sat.latest.v1",
            "lane": "sat",
            "sequence": sequence,
            "created_at": created_at,
            "publisher_generation_id": os.environ.get(
                "CATHEDRAL_PUBLISHER_GENERATION_ID", "default"),
            "storage": "in_process_current_snapshot",
            "trust_root": "signed_latest_pointer_and_artifact_hashes",
            "artifacts": {
                "board": {
                    "url": board_url,
                    "content_type": "application/json",
                    "hash": board_hash,
                    "size_bytes": len(_snapshot_bytes(board_payload)),
                    "etag": board_etag,
                },
                "weights": {
                    "url": weights_url,
                    "content_type": "application/json",
                    "hash": weights_hash,
                    "size_bytes": len(_snapshot_bytes(weights_payload)),
                    "signature": "embedded",
                    "generated_at": weights_payload.get("generated_at"),
                    "expires_at": weights_payload.get("expires_at"),
                },
            },
            "miner_paths": {
                "latest": "/sat/latest.json",
                "events": "/sat/events",
                "public_board": board_url,
                "public_compat": "/v1/synthetic-boolean/active-challenges",
                "private_assignments": "/v1/synthetic-boolean/per-miner/challenges",
                "private_cnf": "/v1/synthetic-boolean/per-miner/cnf",
                "submit": "/v1/agents/submit",
                "receipt_status": "/v1/agents/receipts/{receipt_id}",
            },
            "compatibility": {
                "legacy_read_endpoints_kept": True,
                "legacy_validator_weights_kept": True,
                "historical_sequence_storage": "not_yet_published",
            },
        }
        signed_pointer = _sign_latest_pointer(pointer)
        return signed_pointer, board_payload, weights_payload, _snapshot_etag(signed_pointer)

    def _sat_snapshot_headers(etag: str, sequence: str, *, immutable: bool = False) -> dict[str, str]:
        cache_control = (
            "public, max-age=31536000, immutable"
            if immutable
            else "public, max-age=5, must-revalidate"
        )
        return {
            "Cache-Control": cache_control,
            "ETag": etag,
            "X-Cathedral-Sequence": sequence,
            "Access-Control-Allow-Origin": "*",
        }

    top_cache = top_cache_mod.TopCache()
    if _role_runs_read_background(service_role):
        top_cache.start(store)
    top_cache_mod.register(top_cache)
    recent_cache = _SoftTtlCache(
        "recent-leaderboard",
        _env_float("CATHEDRAL_RECENT_CACHE_TTL_SECS", 2.0),
    )
    explain_cache = _SoftTtlCache(
        "leaderboard-explain",
        _env_float("CATHEDRAL_EXPLAIN_CACHE_TTL_SECS", 10.0),
    )
    pm_summary_cache = _SoftTtlCache(
        "per-miner-summary",
        _env_float("CATHEDRAL_PM_SUMMARY_CACHE_TTL_SECS", 5.0),
    )
    pressure_telemetry = PressureTelemetry(pressure_config_from_env())
    _visibility_cold_async_raw = os.environ.get("CATHEDRAL_VISIBILITY_COLD_ASYNC")
    visibility_cold_async = (
        (_visibility_cold_async_raw.strip().lower() not in {"0", "false", "no", "off"})
        if _visibility_cold_async_raw is not None
        else database_path != ":memory:"
    )

    app = FastAPI(title="cathedral-thin-publisher")

    # Backend-compat: the prior backend served the API under an `/api/cathedral`
    # path prefix, and miners are configured against that. Strip the prefix
    # before routing so `/api/cathedral/v1/...` reaches the same handlers as
    # `/v1/...` — both paths serve identically, no client reconfiguration needed.
    #
    # Pure ASGI middleware (no BaseHTTPMiddleware): avoids the response-body
    # buffering that BaseHTTPMiddleware adds, which serializes concurrent
    # requests and causes 20-30s stalls under real validator load.
    _LEGACY_PREFIX = "/api/cathedral"
    _LEGACY_PREFIX_BYTES = _LEGACY_PREFIX.encode()

    class _StripLegacyPrefixMiddleware:
        """Pure ASGI middleware: strips /api/cathedral prefix before routing."""
        def __init__(self, asgi_app):
            self._app = asgi_app

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                path = scope.get("path", "")
                if path.startswith(_LEGACY_PREFIX + "/") or path == _LEGACY_PREFIX:
                    scope = dict(scope)  # shallow copy so we don't mutate shared state
                    scope["path"] = path[len(_LEGACY_PREFIX):] or "/"
                    raw = scope.get("raw_path")
                    if raw:
                        scope["raw_path"] = raw.replace(_LEGACY_PREFIX_BYTES, b"", 1)
            await self._app(scope, receive, send)

    class _SlowRequestLogMiddleware:
        """Pure ASGI middleware: logs slow request paths without query strings."""
        def __init__(self, asgi_app):
            self._app = asgi_app

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                await self._app(scope, receive, send)
                return
            try:
                threshold = float(os.environ.get(
                    "CATHEDRAL_SLOW_REQUEST_LOG_SECS", "2.0") or "0")
            except ValueError:
                threshold = 2.0
            if threshold <= 0:
                await self._app(scope, receive, send)
                return

            started = time.monotonic()
            status = None

            async def _send(message):
                nonlocal status
                if message.get("type") == "http.response.start":
                    status = message.get("status")
                await send(message)

            try:
                await self._app(scope, receive, _send)
            finally:
                elapsed = time.monotonic() - started
                if elapsed >= threshold:
                    print(
                        "[slow_request] "
                        f"method={scope.get('method', '')} "
                        f"path={scope.get('path', '')} "
                        f"status={status if status is not None else '-'} "
                        f"elapsed={elapsed:.3f}s"
                    )

    class _StatusCounterMiddleware:
        """Pure ASGI middleware: tally response status classes (5xx rates).

        Reads only http.response.start status — no body buffering, no extra
        thread-pool work — so it is safe in front of every route, including the
        Tier 0 weight feed. Feeds the validator-health endpoint and release gate.
        """
        def __init__(self, asgi_app):
            self._app = asgi_app

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                await self._app(scope, receive, send)
                return
            path = scope.get("path", "")
            status = None

            async def _send(message):
                nonlocal status
                if message.get("type") == "http.response.start":
                    status = message.get("status")
                await send(message)

            try:
                await self._app(scope, receive, _send)
            except Exception:
                # Unhandled error: the app never sent http.response.start, so
                # `status` is still None. The ASGI server turns this into a 500
                # for the client, so record a synthetic 500 here (before
                # re-raising) — otherwise the highest-severity faults would be
                # invisible to the 5xx counter.
                _record_http_status(path, 500)
                raise
            else:
                # Normal completion. If the app somehow never sent a
                # response-start status (misbehaving handler), the server still
                # returns a 500 to the client, so count it as one.
                _record_http_status(path, status if status is not None else 500)

    class _HotPathBackpressureMiddleware:
        """Reject excess heavy requests before body parsing/threadpool work."""

        _SUBMIT_PATHS = {
            "/v1/agents/submit",
            f"{_LEGACY_PREFIX}/v1/agents/submit",
            "/v2/agents/submit-bitset",
            f"{_LEGACY_PREFIX}/v2/agents/submit-bitset",
        }
        _PM_READ_PATHS = {
            "/v1/synthetic-boolean/per-miner/challenges",
            "/v1/synthetic-boolean/per-miner/cnf",
            f"{_LEGACY_PREFIX}/v1/synthetic-boolean/per-miner/challenges",
            f"{_LEGACY_PREFIX}/v1/synthetic-boolean/per-miner/cnf",
            "/v2/synthetic-boolean/per-miner/challenges",
            "/v2/synthetic-boolean/per-miner/cnf",
            "/v2/synthetic-boolean/per-miner/cnf-access",
            f"{_LEGACY_PREFIX}/v2/synthetic-boolean/per-miner/challenges",
            f"{_LEGACY_PREFIX}/v2/synthetic-boolean/per-miner/cnf",
            f"{_LEGACY_PREFIX}/v2/synthetic-boolean/per-miner/cnf-access",
        }
        _V2_RECEIPT_POLL_PREFIXES = (
            "/v2/agents/submit-bitset/receipts/",
            f"{_LEGACY_PREFIX}/v2/agents/submit-bitset/receipts/",
        )

        def __init__(self, asgi_app):
            self._app = asgi_app
            self._submit_gate = (
                threading.BoundedSemaphore(submit_max_concurrency)
                if submit_max_concurrency > 0 else None
            )
            self._pm_read_gate = (
                threading.BoundedSemaphore(pm_read_hard_cap)
                if pm_read_hard_cap > 0 else None
            )
            # Dedicated gate for V2 receipt polling: each uncached poll is a DB
            # read and live miners poll aggressively right after submit. Bounding
            # concurrency here protects the PG pool (open-v2 incident 2026-07-08).
            self._receipt_poll_gate = (
                threading.BoundedSemaphore(v2_receipt_poll_max_concurrency)
                if v2_receipt_poll_max_concurrency > 0 else None
            )

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                await self._app(scope, receive, send)
                return
            method = scope.get("method")
            path = scope.get("path", "")
            gate = None
            reason = ""
            is_submit = method == "POST" and path in self._SUBMIT_PATHS
            if is_submit:
                gate = self._submit_gate
                reason = "submit_busy_retry"
            elif method == "GET" and path in self._PM_READ_PATHS:
                gate = self._pm_read_gate
                reason = "per_miner_busy_retry"
            elif method == "GET" and path.startswith(self._V2_RECEIPT_POLL_PREFIXES):
                gate = self._receipt_poll_gate
                reason = "receipt_poll_busy_retry"

            # Track 2 (item 7): per-hotkey fairness check BEFORE the global gate.
            # A single hotkey over its budget is rejected with the distinct
            # `abuse_rate_limited` reason; well-behaved miners and the saturation
            # gate are untouched. No-op when the limiter is disabled (default).
            if is_submit and per_hotkey_limiter.active:
                hotkey = _scope_header(scope, b"x-cathedral-hotkey")
                if not per_hotkey_limiter.allow(hotkey):
                    cfg = per_hotkey_limiter.config
                    abuse_body = _PER_HOTKEY_ABUSE_REASON.encode("utf-8")
                    _record_submit_event(
                        "rate_limited",
                        _PER_HOTKEY_ABUSE_REASON,
                        challenge_id=None,
                        status_code=429,
                        log=True,
                    )
                    await send({
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(abuse_body)).encode()),
                            (b"retry-after", str(cfg.retry_after_secs).encode()),
                            (b"x-cathedral-rejection-reason", abuse_body),
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": abuse_body,
                        "more_body": False,
                    })
                    return

            if gate is None:
                await self._app(scope, receive, send)
                return

            # Phase 3: bounded non-blocking wait before rejecting. We poll the
            # semaphore with short asyncio sleeps rather than a blocking acquire so
            # the event loop is never stalled while we wait for a slot to free.
            #
            # LEAK FIX: the whole path from a successful acquire() to the final
            # release() must be exception-safe. Previously a slot acquired at the
            # end of the polling loop could be leaked if a CancelledError (client
            # disconnect) fired after acquire() but before the try/finally that
            # releases it — over many disconnects the BoundedSemaphore drained to
            # zero and every request 429'd spuriously even on an idle origin. We
            # now hold the slot inside a single try that spans BOTH the shed-poll
            # tail and the app call, releasing in finally on every exit path.
            acquired = False
            try:
                acquired = gate.acquire(blocking=False)
                if not acquired and submit_busy_wait_secs > 0:
                    import asyncio
                    deadline = time.monotonic() + submit_busy_wait_secs
                    while not acquired and time.monotonic() < deadline:
                        await asyncio.sleep(0.02)
                        acquired = gate.acquire(blocking=False)
                if not acquired:
                    _record_submit_event(
                        "rate_limited",
                        reason,
                        status_code=429,
                        log=True,
                    )
                    retry_after_secs = 1
                    body = _retry_after_body(reason, retry_after_secs)
                    await send({
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"retry-after", str(retry_after_secs).encode()),
                            (b"x-cathedral-rejection-reason", reason.encode("utf-8")),
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": body,
                        "more_body": False,
                    })
                    return
                await self._app(scope, receive, send)
            finally:
                if acquired:
                    gate.release()

    class _ServiceRoleGuardMiddleware:
        """Fail closed when this process is launched as a narrow service role."""

        _SHARED_PATHS = {
            "/health",
            "/health/live",
            "/health/ready",
            "/.well-known/cathedral-jwks.json",
        }
        _READ_GET_PATHS = {
            "/sat/latest.json",
            "/sat/events",
            "/v1/synthetic-boolean/active-challenges",
            "/v1/synthetic-boolean/challenge-broadcast",
            "/v1/synthetic-boolean/current-challenge",
            "/v1/synthetic-boolean/per-miner/status",
            "/v1/synthetic-boolean/per-miner/summary",
            "/v1/validator/weights/next",
            "/v1/dashboard/state",
            "/v1/leaderboard/recent",
            "/v1/leaderboard/top",
            "/v1/leaderboard/explain",
            # Operator/observability surface for the weight feed. Admin-token
            # gated at the handler; routed here so it stays reachable on the
            # read service that actually serves the Tier 0 weight feed.
            "/v1/admin/validator-health",
            "/v1/admin/synthetic-boolean/submit-metrics",
            "/v2/shadow/v1/agents/submit/metrics",
        }
        _READ_GET_PREFIXES = {
            "/sat/sequences/",
            "/v1/audit-scanner/",
            # Durable submit receipts (Phase 4): a read of durable state, safe to
            # serve from the read role as well as submit (miners poll their receipt).
            "/v1/agents/receipts/",
            "/v2/agents/submit-manifest/receipts/",
            "/v2/agents/submit-bitset/receipts/",
            "/v2/synthetic-boolean/per-miner/challenges",
            "/v2/synthetic-boolean/per-miner/cnf",
            "/v2/synthetic-boolean/per-miner/cnf-access",
            "/v2/validator/weights/next",
            "/v2/audit/epochs/",
            "/v2/receipts/",
        }
        _SUBMIT_GET_PATHS = {
            "/v1/admin/synthetic-boolean/submit-metrics",
            "/v2/shadow/v1/agents/submit/metrics",
            "/v1/synthetic-boolean/active-cnf",
            "/v1/synthetic-boolean/per-miner/challenges",
            "/v1/synthetic-boolean/per-miner/cnf",
            "/v1/admin/synthetic-boolean/submit-metrics",
        }
        _SUBMIT_GET_PREFIXES = {
            "/v1/challenges/",
            # The submit role returns 202 + receipt_url; let the same host resolve
            # that receipt so miners don't need a second host for status polling.
            "/v1/agents/receipts/",
            "/v2/agents/submit-manifest/receipts/",
            "/v2/agents/submit-bitset/receipts/",
            "/v2/synthetic-boolean/per-miner/challenges",
            "/v2/synthetic-boolean/per-miner/cnf",
            "/v2/synthetic-boolean/per-miner/cnf-access",
            "/v2/validator/weights/next",
            "/v2/audit/epochs/",
            "/v2/receipts/",
            "/v2/shadow/v1/agents/submit/receipts/",
        }
        _SUBMIT_POST_PATHS = {
            "/v1/agents/submit",
            "/v1/external-scores/violet",
            "/v2/agents/submit-manifest",
            "/v2/agents/submit-bitset",
            "/v2/blobs/solutions",
            "/v2/admin/verify/tick",
            "/v2/shadow/v1/agents/submit",
        }

        def __init__(self, asgi_app):
            self._app = asgi_app

        @staticmethod
        def _canonical_path(path: str) -> str:
            if path == _LEGACY_PREFIX:
                return "/"
            if path.startswith(_LEGACY_PREFIX + "/"):
                return path[len(_LEGACY_PREFIX):]
            return path

        def _allowed(self, method: str, path: str) -> bool:
            if service_role == "all":
                return True
            if path in self._SHARED_PATHS:
                return True
            if service_role == "worker":
                return False
            if service_role == "read":
                return (
                    method in {"GET", "HEAD"}
                    and (
                        path in self._READ_GET_PATHS
                        or any(path.startswith(prefix) for prefix in self._READ_GET_PREFIXES)
                    )
                )
            if service_role == "submit":
                if method in {"GET", "HEAD"}:
                    return (
                        path in self._SUBMIT_GET_PATHS
                        or any(path.startswith(prefix) for prefix in self._SUBMIT_GET_PREFIXES)
                    )
                return method == "POST" and path in self._SUBMIT_POST_PATHS
            return False

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                await self._app(scope, receive, send)
                return
            path = self._canonical_path(scope.get("path", ""))
            method = scope.get("method", "GET")
            if self._allowed(method, path):
                await self._app(scope, receive, send)
                return

            reason = f"route_not_served_by_{service_role}_role"
            body = reason.encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"x-cathedral-service-role", service_role.encode("utf-8")),
                    (b"x-cathedral-rejection-reason", body),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            })

    class _V2CnfNoStoreMiddleware:
        """Tokens/auth metadata on V2 CNF reads may never become cache entries."""

        _PATHS = {
            "/v2/synthetic-boolean/per-miner/challenges",
            "/v2/synthetic-boolean/per-miner/cnf",
            "/v2/synthetic-boolean/per-miner/cnf-access",
        }

        def __init__(self, asgi_app):
            self._app = asgi_app

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                await self._app(scope, receive, send)
                return
            path = scope.get("path", "")
            if path.startswith(_LEGACY_PREFIX + "/"):
                path = path[len(_LEGACY_PREFIX):]
            if path not in self._PATHS:
                await self._app(scope, receive, send)
                return

            async def _send_no_store(message):
                if message.get("type") == "http.response.start":
                    headers = [
                        (name, value)
                        for name, value in message.get("headers", [])
                        if name.lower() not in {b"cache-control", b"pragma"}
                    ]
                    headers.extend([
                        (b"cache-control", b"no-store"),
                        (b"pragma", b"no-cache"),
                    ])
                    message = {**message, "headers": headers}
                await send(message)

            await self._app(scope, receive, _send_no_store)

    app.add_middleware(_StripLegacyPrefixMiddleware)
    app.add_middleware(_SlowRequestLogMiddleware)

    # Per-key sliding-window rate limiter — anti-flood backpressure for miner
    # endpoints.  Validators (/health, /v1/validator/weights/next) are exempt.
    # Default 120 req/min/key; set CATHEDRAL_RATELIMIT_RPM=0 to disable.
    # Also pure ASGI (no BaseHTTPMiddleware) for the same buffering reason.
    from .ratelimit import AbuseLimitMiddleware, RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)
    # Keep this inside the service-role and abuse guards: role-mismatched or
    # actor-limited requests should not consume submit/read gate slots.
    app.add_middleware(_HotPathBackpressureMiddleware)
    # Opt-in pre-auth abuse shedding for the hottest SAT paths. Starlette wraps
    # later middleware outside earlier middleware, so this runs before the
    # saturation gate and does not consume a slot.
    app.add_middleware(AbuseLimitMiddleware)
    # Role guard stays outside all work-producing middleware: role-mismatched
    # traffic fails before rate-limit state, body parsing, or route work.
    app.add_middleware(_ServiceRoleGuardMiddleware)
    # Wrap every V2 CNF read response, including validation/errors, so a token or
    # authenticated metadata response can never be cached by a browser or edge.
    app.add_middleware(_V2CnfNoStoreMiddleware)
    # Outermost observability layer: count every final response status (incl.
    # role-guard/backpressure rejections) so the 5xx rate is complete. Cheap —
    # reads only the response-start status, no body buffering.
    app.add_middleware(_StatusCounterMiddleware)
    app.add_middleware(PressureTelemetryMiddleware, telemetry=pressure_telemetry)

    app.state.store = store
    app.state.cnf_store = cnf_store
    # CyberGym producer intake. Mounted with THIS app's own store so the route
    # exists on a full-role process and can actually be turned on by
    # configuration; the store is injected per app (not through the module-level
    # setter) so building several apps in one process cannot cross-wire them.
    # The route keeps its own default-off gate: with
    # CATHEDRAL_CYBERGYM_INGEST_ENABLED unset it answers 404, so mounting it
    # changes nothing for an unconfigured deployment. It is deliberately absent
    # from the narrow service-role allowlists above, so read/submit/worker roles
    # keep refusing it and only a full-role process can serve it.
    app.dependency_overrides[cybergym_ingest.get_publisher_store] = lambda: store
    app.include_router(cybergym_ingest.router)
    app.state.board_cache = board_cache
    app.state.top_cache = top_cache
    app.state.board_snapshot = board_snapshot
    dashboard_state_snapshot = dashboard_snapshot_mod.DashboardStateSnapshot(
        lambda: _dashboard_state_payload()
    )
    app.state.dashboard_state_snapshot = dashboard_state_snapshot
    app.state.recent_cache = recent_cache
    app.state.explain_cache = explain_cache
    app.state.pm_summary_cache = pm_summary_cache
    app.state.public_key_hex = pub_hex
    app.state.signing_key_hex = key_hex
    app.state.service_role = service_role
    app.state.refill_task = None
    app.state.seed_task = None
    app.state.arena_eval_task = None
    app.state.arena_payout_task = None
    app.state.async_verify_task = None
    app.state.v2_verify_task = None
    app.state.v2_verify_metrics = {
        "worker_id": None,
        "lock_held_by_self": False,
        "last_lock_acquired_at": None,
        "last_lock_contended_at": None,
        "last_batch_at": None,
        "last_batch_ms": None,
        "last_batch_count": 0,
        "recent_events": [],
        "tick_errors": [],
        "worker_restarts": 0,
        "last_worker_error": None,
        "lock_steals": 0,
    }
    app.state.v2_store = v2_store
    app.state.v2_blob_store = v2_blob_store
    app.state.pressure_telemetry = pressure_telemetry
    # Lane S champion machine + registry persist across eval ticks on app.state
    # (the validator constructs ONE lane and reuses it so the champion survives).
    from ..lanes.solver_arena import SolverArenaLane
    app.state.arena_lane = SolverArenaLane(registry=arena_registry)

    @app.on_event("startup")
    async def _configure_threadpool_tokens():
        # Sync submit verification still runs in AnyIO's worker pool. Keep the
        # pool above the submit gate so cheap sync endpoints are not queued
        # behind solver submissions during PM bursts.
        try:
            import anyio.to_thread

            default_tokens = max(64, submit_max_concurrency * 3 if submit_max_concurrency > 0 else 64)
            desired = _env_int("CATHEDRAL_THREADPOOL_TOKENS", default_tokens)
            if desired <= 0:
                return
            limiter = anyio.to_thread.current_default_thread_limiter()
            if limiter.total_tokens < desired:
                limiter.total_tokens = desired
                print(f"[runtime] anyio_threadpool_tokens={desired}")
        except Exception as exc:
            print(f"[runtime] threadpool_config_failed error={exc!r}")

    @app.on_event("startup")
    async def _start_weights_refresh():
        if not _role_runs_read_background(service_role):
            print(f"[weights] skipped service_role={service_role}")
            return
        # Start only; the build itself runs in the daemon thread.
        weight_key = os.environ.get(weights_mod.SIGNING_KEY_ENV, "").strip() or key_hex
        try:
            weights_mod.start_background_refresh(store, signing_key_hex=weight_key)
            print(f"[weights] bg_refresh_started service_role={service_role}")
        except Exception as exc:
            print(f"[weights] bg_refresh_start_failed error={exc!r}")

    @app.on_event("startup")
    async def _warm_board_cache():
        if not _role_runs_read_background(service_role):
            return
        try:
            board_cache.warm_async()
            print(f"[board] warm_started service_role={service_role}")
        except Exception as exc:
            print(f"[board] warm_start_failed error={exc!r}")

    @app.on_event("startup")
    async def _start_materialized_snapshots():
        # Track 3 / item 6: start the timer that re-materializes the board +
        # leaderboard-top read payloads off the request path. DEFAULT-OFF —
        # start_all() is a no-op unless CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED is
        # set, so live behavior is unchanged when the flag is unset. Only the
        # read-background role runs the builders (same gate as board/top caches).
        if not _role_runs_read_background(service_role):
            return
        if not materialized_snapshot_mod.enabled():
            return
        try:
            board_snapshot.start()
            leaderboard_top_snapshot.start()
            print(f"[materialized_snapshot] started service_role={service_role}")
        except Exception as exc:
            print(f"[materialized_snapshot] start_failed error={exc!r}")

    @app.on_event("startup")
    async def _start_dashboard_state_snapshot():
        if not _role_runs_read_background(service_role):
            return
        if not dashboard_snapshot_mod.enabled():
            return
        try:
            dashboard_state_snapshot.start()
            print(f"[dashboard_snapshot] started service_role={service_role}")
        except Exception as exc:
            print(f"[dashboard_snapshot] start_failed error={exc!r}")

    async def _run_singleton_background(label: str, lock_name: str, coro_factory):
        import asyncio

        retry_secs = max(1, int(os.environ.get("CATHEDRAL_SINGLETON_RETRY_SECS", "15")))
        while True:
            try:
                with store.advisory_lock(lock_name) as acquired:
                    if not acquired:
                        print(f"[{label}] singleton_lock_held_elsewhere")
                    else:
                        print(f"[{label}] singleton_lock_acquired")
                        await coro_factory()
                        print(f"[{label}] singleton_task_exited")
            except asyncio.CancelledError:
                print(f"[{label}] singleton_task_cancelled")
                raise
            except Exception as exc:
                print(f"[{label}] singleton_task_error error={exc!r}")
            await asyncio.sleep(retry_secs)

    # ---- G2: challenge refill loop (env-gated) ----------------------------
    @app.on_event("startup")
    async def _start_refill():
        from . import refill
        if refill.refill_enabled():
            if not _role_runs_worker(service_role):
                print(f"[refill] skipped service_role={service_role}")
                return
            import asyncio
            loop_log = lambda evt, **kw: print(f"[refill] {evt} {kw}")  # noqa: E731
            app.state.refill_task = asyncio.create_task(
                _run_singleton_background(
                    "refill",
                    "cathedral:publisher:refill",
                    lambda: refill.refill_loop(store, log=loop_log),
                ))

    # ---- Phase 5: async SAT verification worker (env-gated) ---------------
    # Drains pending durable-admission attempts off the request path. Default OFF;
    # only runs on a worker-capable role AND when both durable admission and the
    # worker flag are enabled. The singleton advisory lock keeps exactly one loop
    # active across replicas (claim is already crash-safe via locked_until_iso).
    @app.on_event("startup")
    async def _start_async_verify():
        from . import verify_worker
        verify_on = verify_worker.async_verify_enabled()
        # Loud WARNING for the foot-gun: async admission returns 202 receipts, but
        # if NO process is configured to run the drain worker those receipts stay
        # `pending` forever and the miners that earned them never get paid. Two
        # ways this happens: (a) CATHEDRAL_ASYNC_VERIFY_ENABLED was never turned on
        # (no worker anywhere); (b) this is the only role and it is not worker-
        # capable (e.g. a single submit/read role with no companion worker role).
        if submit_async_enabled and not verify_on:
            print(
                "[verify] WARNING: CATHEDRAL_SUBMIT_ASYNC_ENABLED is on but "
                "CATHEDRAL_ASYNC_VERIFY_ENABLED is not set — 202 receipts will "
                "NEVER drain to ranked and miners go UNPAID. Enable the worker "
                "(see deploy/ROLE_SPLIT_RUNBOOK.md 'Safe enable order').")
        elif (submit_async_enabled and verify_on
              and not _role_runs_worker(service_role)):
            print(
                f"[verify] WARNING: async admission on (service_role="
                f"{service_role}) but this role does not run the verify worker; "
                "ensure a worker/all role is deployed or 202 receipts go UNPAID.")
        # TRACK 1: the same drain worker handles pm-* rows (claim is kind-agnostic,
        # ordered by received_at). Warn loudly if the pm-* async lane was turned on
        # without a drain worker — pm 202 receipts would otherwise never pay out.
        if pm_submit_async_enabled and not verify_on:
            print(
                "[verify] WARNING: CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED is on but "
                "CATHEDRAL_ASYNC_VERIFY_ENABLED is not set — pm-* 202 receipts will "
                "NEVER drain and miners go UNPAID. Enable the worker, or leave "
                "pm-async off (the inline synchronous path stays in effect).")
        elif (pm_submit_async_enabled and verify_on
              and not _role_runs_worker(service_role)):
            print(
                f"[verify] WARNING: pm-* async admission on (service_role="
                f"{service_role}) but this role does not run the verify worker; "
                "ensure a worker/all role is deployed or pm-* 202 receipts go UNPAID.")
        if pm_async_shadow_enabled:
            print(
                "[verify] pm-* async SHADOW mode ON: inline result stays "
                "authoritative for payout; async verdict is recorded to shadow_* "
                "columns and divergence is logged (no payout change).")
        # The worker loop still runs when ONLY the public async flag is on; but if
        # pm-async is on while the public flag is off the worker would not start, so
        # treat pm-async as also requiring the worker loop to run.
        if not ((submit_async_enabled or pm_submit_async_enabled) and verify_on):
            return
        if not _role_runs_worker(service_role):
            print(f"[verify] skipped service_role={service_role}")
            return
        import asyncio
        worker_id = f"{service_role}:{new_uuid()[:8]}"

        def _verify_heartbeat(event, **kw):
            try:
                submit_admission.record_worker_heartbeat(
                    store,
                    worker_id=worker_id,
                    service_role=service_role,
                    now_iso=_now_iso_ms(),
                    event=event,
                    processed=int(kw.get("processed") or 0),
                    error=kw.get("error"),
                )
            except Exception as exc:
                print(f"[verify] heartbeat_failed error={exc!r}")

        app.state.async_verify_task = asyncio.create_task(
            _run_singleton_background(
                "verify",
                "cathedral:publisher:async_verify",
                lambda: verify_worker.verify_loop(
                    app.state.async_verify_tick, worker_id=worker_id,
                    log=lambda evt, **kw: print(f"[verify] {evt} {kw}"),
                    heartbeat=_verify_heartbeat),
            ))

    async def _run_v2_singleton_background(label: str, lock_name: str, coro_factory):
        import asyncio

        retry_secs = max(1, int(os.environ.get("CATHEDRAL_V2_SINGLETON_RETRY_SECS", "15")))
        contended_log_secs = max(30, int(os.environ.get("CATHEDRAL_V2_SINGLETON_CONTENDED_LOG_SECS", "300")))
        # Self-healing takeover: when the lock is held elsewhere, check whether
        # the holder is actually a crashed/replaced worker (PG session idle
        # AND its heartbeat stale/absent for longer than this) rather than a
        # live peer between ticks. See Store.steal_stale_advisory_lock.
        steal_idle_secs = max(30, int(os.environ.get("CATHEDRAL_V2_LOCK_STEAL_IDLE_SECS", "180")))
        # Crash containment: an exception escaping the lock/coro_factory body
        # (as opposed to an ordinary per-batch tick error, which _loop already
        # swallows internally) backs off 5s -> 60s instead of hammering
        # retry_secs, and resets to the floor after any healthy cycle so a
        # transient blip does not leave the loop permanently slow.
        error_backoff_floor = max(0.01, _env_float("CATHEDRAL_V2_ERROR_BACKOFF_FLOOR_SECS", 5.0))
        error_backoff_cap = max(error_backoff_floor, _env_float("CATHEDRAL_V2_ERROR_BACKOFF_CAP_SECS", 60.0))
        error_backoff = error_backoff_floor
        last_contended_log = 0.0
        while True:
            sleep_secs = retry_secs
            try:
                with v2_store.advisory_lock(lock_name) as acquired:
                    if not acquired:
                        now = time.time()
                        app.state.v2_verify_metrics["lock_held_by_self"] = False
                        app.state.v2_verify_metrics["last_lock_contended_at"] = _now_iso_ms()
                        if now - last_contended_log >= contended_log_secs:
                            print(f"[{label}] singleton_lock_held_elsewhere")
                            last_contended_log = now
                        try:
                            stolen = v2_store.steal_stale_advisory_lock(
                                lock_name, idle_secs=steal_idle_secs)
                        except Exception as steal_exc:
                            stolen = 0
                            print(f"[{label}] lock_steal_check_failed error={steal_exc!r}")
                        if stolen:
                            app.state.v2_verify_metrics["lock_steals"] = (
                                int(app.state.v2_verify_metrics.get("lock_steals") or 0) + stolen)
                            print(f"[{label}] lock_steal_terminated n={stolen}")
                    else:
                        app.state.v2_verify_metrics["lock_held_by_self"] = True
                        app.state.v2_verify_metrics["last_lock_acquired_at"] = _now_iso_ms()
                        print(f"[{label}] singleton_lock_acquired")
                        await coro_factory()
                        app.state.v2_verify_metrics["lock_held_by_self"] = False
                        print(f"[{label}] singleton_task_exited")
                        error_backoff = error_backoff_floor
            except asyncio.CancelledError:
                app.state.v2_verify_metrics["lock_held_by_self"] = False
                print(f"[{label}] singleton_task_cancelled")
                raise
            except Exception as exc:
                # ANY exception that reaches here (lock acquisition itself
                # failing, or -- in principle -- coro_factory raising) must
                # never kill this task: log it, let advisory_lock's own
                # cleanup release/discard the lock connection (best-effort),
                # back off, and loop again to re-acquire and resume.
                app.state.v2_verify_metrics["lock_held_by_self"] = False
                app.state.v2_verify_metrics["worker_restarts"] = (
                    int(app.state.v2_verify_metrics.get("worker_restarts") or 0) + 1)
                app.state.v2_verify_metrics["last_worker_error"] = repr(exc)[:200]
                print(f"[{label}] singleton_task_error error={exc!r}")
                sleep_secs = error_backoff
                error_backoff = min(error_backoff * 2.0, error_backoff_cap)
            await asyncio.sleep(sleep_secs)

    @app.on_event("startup")
    async def _start_v2_verify_worker():
        if not (solution_manifest_enabled and v2_worker_enabled):
            return
        if not _role_runs_worker(service_role):
            print(f"[v2_verify] skipped service_role={service_role}")
            return
        import asyncio
        worker_id = f"v2:{service_role}:{new_uuid()[:8]}"
        # Shared literal so the heartbeat row _loop() writes below and the
        # advisory lock _run_v2_singleton_background acquires always refer to
        # the same lock -- steal_stale_advisory_lock's second guardrail
        # (heartbeat staleness) only lines up with the right lock if these
        # two never drift apart.
        v2_verify_lock_name = "cathedral:v2:verify"

        async def _loop():
            app.state.v2_verify_metrics["worker_id"] = worker_id
            while True:
                # Best-effort liveness beat: proves to steal_stale_advisory_lock
                # that a live worker (not just an idle PG session) holds the
                # lock. write_v2_worker_heartbeat never raises; the to_thread
                # wrapper just keeps this blocking DB call off the event loop.
                try:
                    await asyncio.to_thread(
                        v2_store.write_v2_worker_heartbeat,
                        v2_verify_lock_name, worker_id, _now_iso_ms(),
                    )
                except Exception as hb_exc:
                    print(f"[v2_verify] heartbeat_failed error={hb_exc!r}")
                try:
                    batch_started = time.time()
                    results = await asyncio.to_thread(
                        v2_pipeline.process_batch,
                        v2_store,
                        v2_blob_store,
                        worker_id=worker_id,
                        batch_size=v2_worker_batch_size,
                        lock_secs=v2_worker_lock_secs,
                        max_blob_bytes=v2_worker_max_blob_bytes,
                    )
                    # Async witness-check + score for thin-submitted bitset events
                    # (status 'received'). The submit handler no longer does this
                    # inline, so it runs here. Same tick, best-effort.
                    try:
                        bitset_results = await asyncio.to_thread(
                            v2_pipeline.process_bitset_batch,
                            v2_store,
                            worker_id=worker_id,
                            batch_size=v2_worker_batch_size,
                            lock_secs=v2_worker_lock_secs,
                        )
                        if bitset_results:
                            results = list(results) + list(bitset_results)
                    except Exception as be:
                        print(f"[v2_verify] bitset_batch_error error={be!r}")
                    # Push flat per-miner results files to Hippius for each
                    # distinct (hotkey, epoch) that changed this tick. One
                    # write per miner regardless of batch size. Best-effort:
                    # publish_changed_miners never raises.
                    if results:
                        await asyncio.to_thread(
                            results_publisher.publish_changed_miners,
                            v2_store, v2_hip, results,
                        )
                    batch_ms = (time.time() - batch_started) * 1000.0
                    if results:
                        counts = {}
                        for r in results:
                            counts[str(r.get("status") or "unknown")] = counts.get(str(r.get("status") or "unknown"), 0) + 1
                        now_iso = _now_iso_ms()
                        app.state.v2_verify_metrics["last_batch_at"] = now_iso
                        app.state.v2_verify_metrics["last_batch_ms"] = round(batch_ms, 3)
                        app.state.v2_verify_metrics["last_batch_count"] = len(results)
                        events = app.state.v2_verify_metrics.get("recent_events") or []
                        events.append({
                            "ts": time.time(),
                            "verified": int(counts.get(v2_pipeline.STATUS_VERIFIED, 0)),
                            "rejected": int(counts.get(v2_pipeline.STATUS_REJECTED, 0)),
                            "total": int(len(results)),
                        })
                        app.state.v2_verify_metrics["recent_events"] = events[-512:]
                        print(
                            "[v2_verify] batch "
                            f"n_verified={counts.get(v2_pipeline.STATUS_VERIFIED, 0)} "
                            f"n_rejected={counts.get(v2_pipeline.STATUS_REJECTED, 0)} "
                            f"n_total={len(results)} batch_ms={batch_ms:.1f}"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    errors = app.state.v2_verify_metrics.get("tick_errors") or []
                    errors.append({"ts": time.time(), "error": repr(exc)})
                    app.state.v2_verify_metrics["tick_errors"] = errors[-128:]
                    print(f"[v2_verify] tick_failed error={exc!r}")
                await asyncio.sleep(v2_worker_interval_secs)

        if v2_worker_parallel_claims:
            print("[v2_verify] parallel_claims_enabled")
            app.state.v2_verify_task = asyncio.create_task(_loop())
        else:
            app.state.v2_verify_task = asyncio.create_task(
                _run_v2_singleton_background(
                    "v2_verify",
                    v2_verify_lock_name,
                    _loop,
                )
            )

    # ---- Lane S: arena eval loop (env-gated, TASK 1) ----------------------
    # Periodically scores registered pending solvers and, on a record-fall,
    # emits a signed v6 row crediting the new champion's owner. Default OFF.
    # In prod the adapter resolver must plug a real attested container runner;
    # until that is wired it returns None (every solver stays pending — no false
    # record-falls, the safe default). The gate exercises arena_eval_tick
    # directly with deterministic stub adapters.
    @app.on_event("startup")
    async def _start_arena_eval():
        from . import arena_eval
        if not arena_eval.arena_eval_enabled():
            return
        if not _role_runs_worker(service_role):
            print(f"[arena] skipped service_role={service_role}")
            return
        import asyncio
        salt = f"epoch_{datetime.now(timezone.utc):%Y%m%d}:{_FAMILY}"

        def _prod_adapter_for(spec):
            # No real container runner is wired into the thin publisher yet; a
            # solver stays pending until one is. Flagged for Fred — turning the
            # loop on without a runner is a no-op, never a crash or false payout.
            return None

        app.state.arena_eval_task = asyncio.create_task(
            _run_singleton_background(
                "arena",
                "cathedral:publisher:arena_eval",
                lambda: arena_eval.arena_eval_loop(
                    store, app.state.arena_lane,
                    adapter_for=_prod_adapter_for, private_key_hex=key_hex,
                    epoch_salt=salt, log=lambda evt, **kw: print(f"[arena] {evt} {kw}")),
            ))

    # ---- Lane I: payout loop (env-gated, TASK 2) --------------------------
    # Settles breaker instances on pay-on-disagreement-proven-hardness. Default
    # OFF. Like the arena eval loop, the champion/closers providers return the
    # real attested run capability in prod; until that runner is wired the
    # champion provider returns None (no settlement runs — the safe default).
    @app.on_event("startup")
    async def _start_arena_payout():
        from . import arena_payout
        if not arena_payout.arena_payout_enabled():
            return
        if not _role_runs_worker(service_role):
            print(f"[lane-i] skipped service_role={service_role}")
            return
        import asyncio
        salt = f"epoch_{datetime.now(timezone.utc):%Y%m%d}:{_FAMILY}"

        app.state.arena_payout_task = asyncio.create_task(
            _run_singleton_background(
                "lane-i",
                "cathedral:publisher:arena_payout",
                lambda: arena_payout.arena_payout_loop(
                    store, app.state.arena_lane,
                    round_source=lambda: int(os.environ.get("CATHEDRAL_ARENA_ROUND", "0")),
                    champion_provider=lambda: None,   # no real runner wired yet
                    closers_provider=lambda: [],
                    private_key_hex=key_hex, epoch_salt=salt,
                    log=lambda evt, **kw: print(f"[lane-i] {evt} {kw}")),
            ))

    # ---- G1b: self-seed from the live feed (env-gated) --------------------
    # Runs the backfill INSIDE the app process — survives as long as the
    # container does, checkpoints the watermark per page, and resumes after any
    # redeploy. No external ssh/cron. Gated by CATHEDRAL_SEED_ON_BOOT so it
    # only runs on the staging service during cutover prep.
    @app.on_event("startup")
    async def _start_seed():
        if os.environ.get("CATHEDRAL_SEED_ON_BOOT", "").lower() not in ("1", "true", "yes"):
            return
        if not _role_runs_worker(service_role):
            print(f"[seed] skipped service_role={service_role}")
            return
        import asyncio
        from . import seed_live

        base = os.environ.get("CATHEDRAL_SEED_BASE_URL", "https://api.cathedral.computer")
        days = int(os.environ.get("CATHEDRAL_SEED_DAYS", "7"))

        async def _seed_runner():
            import argparse
            # Catch up, then top up periodically so the staging store tracks
            # live until the swap. Each pass resumes from the durable watermark.
            while True:
                try:
                    args = argparse.Namespace(
                        db=None, base_url=base, days=days, page_limit=500,
                        pace=0.0, timeout=90, max_pages=100_000, dry_run=False)
                    # run the blocking HTTP/SQLite seed off the event loop
                    summary = await asyncio.to_thread(
                        seed_live.run_with_store, store, args,
                        lambda *m: print(f"[seed] {' '.join(str(x) for x in m)}"))
                    print(f"[seed] pass done: {summary}")
                except Exception as e:  # never let a transient feed error kill the loop
                    print(f"[seed] pass error (will retry): {e!r}")
                await asyncio.sleep(int(os.environ.get("CATHEDRAL_SEED_TOPUP_SECS", "120")))

        app.state.seed_task = asyncio.create_task(
            _run_singleton_background(
                "seed",
                "cathedral:publisher:seed",
                _seed_runner,
            ))

    # ---- Retention: bounded stale-ledger pruning (default off) ------------
    @app.on_event("startup")
    async def _start_retention():
        from . import retention
        if not retention.retention_enabled():
            return
        if not _role_runs_worker(service_role):
            print(f"[retention] skipped service_role={service_role}")
            return
        import asyncio
        app.state.retention_task = asyncio.create_task(
            _run_singleton_background(
                "retention",
                "cathedral:publisher:retention",
                lambda: retention.retention_loop(
                    store, log=lambda evt, **kw: print(f"[retention] {evt} {kw}")),
            ))

    @app.on_event("shutdown")
    async def _stop_refill():
        import asyncio

        for attr in (
            "refill_task", "seed_task", "arena_eval_task", "arena_payout_task",
            "retention_task", "v2_verify_task",
        ):
            task = getattr(app.state, attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        if hasattr(app.state, "top_cache"):
            app.state.top_cache.stop()
        for attr in ("board_snapshot", "leaderboard_top_snapshot"):
            snap = getattr(app.state, attr, None)
            if snap is not None:
                try:
                    snap.stop()
                except Exception:
                    pass

    # ---- helpers ----------------------------------------------------------
    def _challenge_public(r: Any) -> dict[str, Any]:
        cid = r["challenge_id"]
        label = r["difficulty_label"] or ""
        score_multiplier = float(r["score_multiplier"])
        kind = "audit_cnf" if cid.startswith("audit-") or str(label).startswith("audit") else "random_3sat"
        return {
            "family_id": r["family_id"],
            "challenge_id": cid,
            "status": r["status"],
            "tier": r["tier"],
            "difficulty_label": r["difficulty_label"],
            "score_multiplier": score_multiplier,
            "kind": kind,
            "storage": "sqlite_text",
            "cnf_sha256": r["cnf_sha256"],
            "cnf_bytes": r["cnf_bytes"],
            "num_vars": r["num_vars"],
            "num_clauses": r["num_clauses"],
            "announced_time_limit_secs": 604800,
            "solve_on_submit_enabled": True,
            "win_rule": (
                "Shadow audit instance: valid witnesses are harvested, no emission score."
                if score_multiplier <= 0.0 else
                "First submitted valid SAT receipt wins."
            ),
            "active_cnf_path": f"/api/cathedral/v1/synthetic-boolean/active-cnf?challenge_id={cid}",
            "submit_path": "/api/cathedral/v1/agents/submit",
        }

    def _mint_token(challenge_id: str) -> str:
        exp = int(time.time()) + _CNF_TOKEN_TTL
        msg = f"{challenge_id}:{exp}".encode()
        mac = hmac.new(token_secret, msg, hashlib.sha256).hexdigest()[:32]
        return f"{exp}.{mac}"

    def _check_token(challenge_id: str, token: str) -> bool:
        try:
            exp_s, mac = token.split(".", 1)
            exp = int(exp_s)
        except Exception:
            return False
        if exp < int(time.time()):
            return False
        expect = hmac.new(token_secret, f"{challenge_id}:{exp}".encode(),
                          hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(mac, expect)  # constant-time

    def _verify_hotkey_claim(
        hotkey: str, signature_b64: str, submitted_at: str,
        *, challenge_id: str | None = None, dimacs_solution_sha256: str | None = None,
        alt_submitted_at: str | None = None,
        allow_fallback_shapes: bool = True,
        card_id: str = _FAMILY,
    ) -> str:
        """Verify an sr25519 claim or raise HTTPException; return the timestamp
        that actually verified.
        """
        ts_candidates = [t for t in (submitted_at, alt_submitted_at) if t]
        if not ts_candidates:
            raise HTTPException(400, "invalid submitted_at")
        any_in_skew = False
        for ts_str in ts_candidates:
            ts = _parse_iso(ts_str)
            if ts is None or abs(time.time() - ts) > _SKEW_SECS:
                continue
            any_in_skew = True
            shapes = [
                dict(challenge_id=challenge_id, dimacs_solution_sha256=dimacs_solution_sha256),
            ]
            if allow_fallback_shapes:
                shapes.extend([
                    dict(challenge_id="", dimacs_solution_sha256=""),
                    dict(challenge_id=None, dimacs_solution_sha256=None),
                ])
            for shape in shapes:
                msg = canonical_claim_bytes(
                    bundle_hash=_empty_bundle_hash(), card_id=card_id,
                    miner_hotkey=hotkey, submitted_at=ts_str, **shape)
                if verifier.verify(hotkey, msg, signature_b64):
                    return ts_str
        if not any_in_skew:
            raise HTTPException(400, "submitted_at outside acceptable clock-skew window")
        raise HTTPException(401, "invalid hotkey signature")

    _ACTIVE_CHALLENGE_COLUMNS = (
        "challenge_id, family_id, tier, status, difficulty_label, "
        "score_multiplier, cnf_sha256, cnf_bytes, num_vars, num_clauses"
    )

    def _active_challenges(tier: int | None = None) -> list[Any]:
        # local only: seeded external mirrors (feed-continuity artifacts, no
        # CNF body) must never reach the miner-facing board — a miner that
        # picks one gets a 404 on the CNF fetch and wastes the attempt.
        if tier is None:
            return store.query(
                f"SELECT {_ACTIVE_CHALLENGE_COLUMNS} FROM lane_challenges "
                "WHERE status='active' AND cnf_source='local' "
                "ORDER BY challenge_id ASC")
        return store.query(
            f"SELECT {_ACTIVE_CHALLENGE_COLUMNS} FROM lane_challenges "
            "WHERE status='active' AND cnf_source='local' AND tier=? "
            "ORDER BY challenge_id ASC",
            (tier,))

    # ---- Off-chain TEE GPU capacity intake (default-off, non-emission) ----
    from . import tee_gpu
    tee_gpu.register_routes(app, store)

    # ---- Solver attestation receipt surface (default-off, fail-closed) ----
    @app.post("/v1/attest/nonce")
    def attest_nonce(
        challenge_id: str = Form(...),
        miner_pubkey_b64: str = Form(...),
        submitted_at: str = Form(None),
        ttl_secs: int = Form(300),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(default="", alias="X-Cathedral-Submitted-At"),
    ):
        from . import attest
        if not attest.attest_enabled():
            raise HTTPException(404, "not_found")
        if not challenge_id:
            raise HTTPException(400, "missing_challenge_id")
        if not miner_pubkey_b64:
            raise HTTPException(400, "missing_miner_pubkey_b64")
        submitted_at = submitted_at or x_cathedral_submitted_at or _now_iso_ms()
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, submitted_at,
            challenge_id=challenge_id, dimacs_solution_sha256="",
            alt_submitted_at=x_cathedral_submitted_at or None,
            allow_fallback_shapes=False,
        )
        ttl = max(1, min(int(ttl_secs), 3600))
        nonce = attest.issue_nonce(
            store, token_secret, x_cathedral_hotkey, challenge_id,
            miner_pubkey_b64=miner_pubkey_b64, ttl_secs=ttl)
        return {
            "nonce": nonce,
            "challenge_id": challenge_id,
            "miner_hotkey": x_cathedral_hotkey,
            "expires_in_secs": ttl,
            "report_data_recipe": "route_b_solver_receipt_v1",
        }

    def _attest_intel_verifier():
        from . import attest
        return attest.configured_intel_verifier()

    def _bearer_value(authorization: str | None) -> str:
        supplied = (authorization or "").strip()
        if supplied.lower().startswith("bearer "):
            supplied = supplied.split(" ", 1)[1].strip()
        return supplied

    def _attest_status_token_configured() -> str:
        return os.environ.get("CATHEDRAL_ATTEST_STATUS_TOKEN", "").strip()

    def _attest_status_public() -> bool:
        return os.environ.get("CATHEDRAL_ATTEST_STATUS_PUBLIC", "").strip().lower() in {
            "1", "true", "yes", "on"}

    @app.post("/v1/attest")
    async def attest_verify_endpoint(
        request: Request,
        x_cathedral_hotkey: str = Header(default=""),
        x_cathedral_signature: str = Header(default=""),
        x_cathedral_submitted_at: str = Header(default="", alias="X-Cathedral-Submitted-At"),
    ):
        from . import attest
        if not attest.attest_enabled():
            raise HTTPException(404, "not_found")
        intel = _attest_intel_verifier()
        if intel is None:
            raise HTTPException(503, "attestation_verifier_not_configured")
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "malformed_json")
        if not isinstance(payload, dict):
            raise HTTPException(400, "malformed_json")
        if not (x_cathedral_hotkey and x_cathedral_signature and x_cathedral_submitted_at):
            raise HTTPException(401, "missing_hotkey_signature")
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, x_cathedral_submitted_at,
            challenge_id=str(payload.get("challenge_id") or ""),
            dimacs_solution_sha256="",
            allow_fallback_shapes=False,
        )
        res = attest.verify_attestation(
            store,
            payload,
            private_key_hex=key_hex,
            intel=intel,
            authenticated_hotkey=x_cathedral_hotkey,
        )
        return {
            "ok": res.ok,
            "reason": res.reason,
            "multiplier": res.multiplier,
            "eval_run_id": res.eval_run_id,
            "verifier_backend": getattr(intel, "backend", "unknown"),
        }

    @app.get("/v1/attest/status/{eval_run_id}")
    def attest_status(eval_run_id: str, authorization: str | None = Header(None)):
        from . import attest
        if not attest.attest_enabled():
            raise HTTPException(404, "not_found")
        if not _attest_status_public():
            token = _attest_status_token_configured()
            if not token:
                raise HTTPException(503, "attest_status_token_not_configured")
            if not hmac.compare_digest(_bearer_value(authorization), token):
                raise HTTPException(401, "invalid_attest_status_token")
        rows_ = store.query(
            "SELECT id, row_json, attested FROM eval_runs WHERE id=?", (eval_run_id,))
        if not rows_:
            raise HTTPException(404, "eval_run_not_found")
        att_rows = store.query(
            "SELECT id, miner_hotkey, challenge_id, solver_digest, multiplier, "
            "verified_at_iso FROM attestations WHERE eval_run_id=? "
            "ORDER BY verified_at_iso DESC",
            (eval_run_id,))
        return {
            "eval_run_id": eval_run_id,
            "attested": bool(rows_[0]["attested"]),
            "attestations": [dict(r) for r in att_rows],
        }

    def _current_weight_context() -> dict[str, Any]:
        """Current payment weights for display-only leaderboard annotations."""
        weight_key = os.environ.get(weights_mod.SIGNING_KEY_ENV, "").strip() or key_hex
        try:
            vec = weights_mod.cached_vector(store, signing_key_hex=weight_key)
        except Exception as exc:
            print(f"[leaderboard] weight context unavailable: {exc!r}")
            vec = None
        if vec is None:
            return {
                "generated_at": None,
                "policy_reason": None,
                "ranked": [],
                "by_hotkey": {},
                "status": "warming",
            }
        ranked = [
            {
                **row,
                "miner_hotkey": str(row.get("miner_hotkey") or ""),
                "current_weight": float(row.get("weight") or 0.0),
            }
            for row in vec.get("weights", [])
            if row.get("miner_hotkey")
        ]
        ranked.sort(key=lambda row: row["current_weight"], reverse=True)
        by_hotkey: dict[str, dict[str, Any]] = {}
        for rank, row in enumerate(ranked, start=1):
            row["current_weight_rank"] = rank
            by_hotkey[row["miner_hotkey"]] = row
        return {
            "generated_at": vec.get("generated_at"),
            "policy_reason": vec.get("policy_reason"),
            "ranked": ranked,
            "by_hotkey": by_hotkey,
        }

    def _weight_annotations(
        weight_ctx: dict[str, Any], hotkeys: list[str]
    ) -> dict[str, dict[str, Any]]:
        by_hotkey = weight_ctx.get("by_hotkey") or {}
        out: dict[str, dict[str, Any]] = {}
        for hk in hotkeys:
            row = by_hotkey.get(hk)
            if row:
                out[hk] = {
                    "current_weight": row["current_weight"],
                    "current_weight_rank": row["current_weight_rank"],
                }
            else:
                out[hk] = {
                    "current_weight": 0.0 if weight_ctx.get("generated_at") else None,
                    "current_weight_rank": None,
                }
        return out

    def _public_row_score_multiplier() -> float:
        """Legacy /recent compatibility for the PM-primary rollout.

        New validators use the signed vector from /v1/validator/weights/next.
        Older validators still aggregate signed receipt rows from /leaderboard/recent.
        When PM is primary, public-board receipt rows must therefore carry zero
        score; otherwise old validators keep paying the retired public-board
        lane as if nothing changed.
        """
        try:
            from . import launch_profile
            from . import per_miner as pm
            if (
                (pm.perminer_enabled() or launch_profile.converged())
                and not pm.perminer_shadow()
                and weights_mod.perminer_scoring_mode() == "pm_primary"
            ):
                return weights_mod.perminer_public_baseline()
        except Exception as exc:
            print(f"[leaderboard] public row compatibility score fallback: {exc!r}")
        return 1.0

    def _task_id_public(challenge_id: str, tier: int) -> str:
        return hashlib.sha256(f"{challenge_id}:{int(tier)}".encode("utf-8")).hexdigest()[:16]

    def _known_pm_task_ids() -> set[str]:
        cached = getattr(app.state, "known_pm_task_ids_cache", None)
        now = time.monotonic()
        if cached and now < float(cached.get("expires_at", 0.0)):
            return set(cached.get("task_ids") or ())
        rows_ = store.query(
            "SELECT DISTINCT challenge_id, tier FROM per_miner_solves "
            "WHERE verified=1"
        )
        task_ids = {
            _task_id_public(str(r["challenge_id"]), int(r["tier"]))
            for r in rows_
            if str(r["challenge_id"] or "").startswith("pm-")
        }
        app.state.known_pm_task_ids_cache = {
            "task_ids": tuple(task_ids),
            "expires_at": now + 30.0,
        }
        return task_ids

    def _rewrite_recent_rows_for_legacy_pm_primary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Serve old-validator /recent economics consistent with PM-primary.

        Existing eval rows are immutable audit records. During the PM-primary
        rollout, however, old validators may still aggregate /recent instead of
        the signed vector. Rewrite only the response copy, then re-sign, so the
        compatibility feed cannot keep paying public-board rows at full value.
        """
        baseline = _public_row_score_multiplier()
        if baseline == 1.0:
            return items
        pm_task_ids = _known_pm_task_ids()
        out: list[dict[str, Any]] = []
        for item in items:
            row = dict(item)
            if row.get("task_type") == _FAMILY:
                cid = str(row.get("challenge_id") or "")
                task_id = str(row.get("task_id_public") or "")
                is_pm = cid.startswith("pm-") or task_id in pm_task_ids
                if not is_pm and float(row.get("weighted_score") or 0.0) > baseline:
                    row["weighted_score"] = float(baseline)
                    row["score_parts"] = {"binary_correct": float(baseline)}
                    if int(row.get("eval_output_schema_version", 0)) == 6:
                        row["challenge_value"] = float(baseline)
                    card = row.get("output_card")
                    if isinstance(card, dict):
                        card = dict(card)
                        card["weighted_score"] = float(baseline)
                        row["output_card"] = card
                    row["cathedral_signature"] = wire.sign_row(row, key_hex)
            out.append(row)
        return out

    def _nullable_float(value: Any) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if out == out else None

    def _nullable_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _nullable_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if value is None or value == "":
            return None
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "y"}:
                return True
            if lowered in {"0", "false", "no", "n"}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        return None

    def _age_seconds(ts: str | None) -> float | None:
        parsed = _parse_iso(str(ts)) if ts else None
        return round(time.time() - parsed, 3) if parsed is not None else None

    def _pick_annotation(sources: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
        for source in sources:
            if not isinstance(source, dict):
                continue
            nested = source.get("chain")
            for candidate in (source, nested if isinstance(nested, dict) else {}):
                for key in keys:
                    if key in candidate and candidate[key] is not None and candidate[key] != "":
                        return candidate[key]
        return None

    def _chain_visibility(*sources: dict[str, Any]) -> dict[str, Any]:
        source_list = [s for s in sources if isinstance(s, dict)]
        uid = _nullable_int(_pick_annotation(source_list, ("uid", "chain_uid")))
        registered = _nullable_bool(_pick_annotation(
            source_list, ("registered", "is_registered", "chain_registered")
        ))
        payable = _nullable_bool(_pick_annotation(
            source_list, ("payable", "is_payable", "chain_payable")
        ))
        incentive = _nullable_float(_pick_annotation(
            source_list, ("incentive", "chain_incentive")
        ))
        emission = _nullable_float(_pick_annotation(
            source_list, ("emission", "emissions", "chain_emission", "chain_emissions")
        ))
        updated_at = _pick_annotation(
            source_list, ("chain_updated_at", "chain_fetched_at", "updated_at", "fetched_at")
        )
        has_chain = any(v is not None for v in (uid, registered, payable, incentive, emission))
        return {
            "uid": uid,
            "registered": registered,
            "payable": payable,
            "incentive": incentive,
            "emission": emission,
            "source": "upstream_annotation" if has_chain else "unavailable",
            "updated_at": updated_at,
            "staleness_seconds": _age_seconds(str(updated_at)) if updated_at else None,
        }

    def _source_meta(
        *,
        path: str,
        status: str,
        generated_at: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        out = {
            "path": path,
            "status": status,
            "generated_at": generated_at,
            "staleness_seconds": _age_seconds(generated_at),
        }
        if note:
            out["note"] = note
        return out

    def _pm_visibility_from_summary(pm_row: dict[str, Any] | None) -> dict[str, Any]:
        if not pm_row:
            return {
                "status": "not_found",
                "source": "v1/synthetic-boolean/per-miner/summary",
                "weighted_units": None,
                "unique_verified_solves": None,
                "last_solved_at": None,
            }
        return {
            "status": "available",
            "source": "v1/synthetic-boolean/per-miner/summary",
            "weighted_units": pm_row.get("weighted_units"),
            "unique_verified_solves": pm_row.get("unique_verified_solves"),
            "verified_solves": pm_row.get("verified_solves"),
            "last_solved_at": pm_row.get("last_solved_at"),
        }

    def _pm_visibility_from_contribution(contribution: dict[str, Any] | None) -> dict[str, Any]:
        if not contribution:
            return {
                "status": "not_requested",
                "source": "v1/leaderboard/explain",
                "weighted_units": None,
                "unique_verified_solves": None,
                "last_solved_at": None,
            }
        totals = contribution.get("last_24h_totals") or {}
        return {
            "status": "available" if contribution.get("eligible", True) else "ineligible",
            "source": "v1/leaderboard/explain",
            "enabled": contribution.get("enabled"),
            "eligible": contribution.get("eligible"),
            "ineligibility_reason": contribution.get("ineligibility_reason"),
            "weighted_units": totals.get("weighted_units"),
            "unique_verified_solves": totals.get("unique_verified_solves"),
            "verified_solves": totals.get("verified_solves"),
            "last_solved_at": totals.get("last_solved_at"),
        }

    def _miner_visibility_row(
        miner_hotkey: str,
        weight_ctx: dict[str, Any],
        *,
        receipt: dict[str, Any] | None = None,
        pm_summary_row: dict[str, Any] | None = None,
        pm_contribution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        by_hotkey = weight_ctx.get("by_hotkey") or {}
        weight_row = by_hotkey.get(miner_hotkey) or {}
        ann = _weight_annotations(weight_ctx, [miner_hotkey]).get(miner_hotkey, {})
        weight_known = miner_hotkey in by_hotkey
        current_weight = ann.get("current_weight", 0.0) if weight_ctx.get("generated_at") else None
        chain = _chain_visibility(weight_row, receipt or {})
        pm_visibility = (
            _pm_visibility_from_contribution(pm_contribution)
            if pm_contribution is not None
            else _pm_visibility_from_summary(pm_summary_row)
        )
        recent_activity = {
            "source": "v1/leaderboard/top?view=receipts",
            "rank_kind": "activity_only_not_payment",
            "receipt_rank_24h": (receipt or {}).get("receipt_rank"),
            "receipt_total_score_24h": (receipt or {}).get("total_score"),
            "receipt_distinct_solves_24h": (receipt or {}).get("distinct_solves"),
            "last_seen": (receipt or {}).get("last_seen"),
        }
        payment_status = "available" if weight_known else (
            "absent_from_signed_vector" if weight_ctx.get("generated_at") else "unavailable"
        )
        return {
            "miner_hotkey": miner_hotkey,
            "uid": chain["uid"],
            "registered": chain["registered"],
            "payable": chain["payable"],
            "current_signed_weight": current_weight,
            "current_signed_weight_rank": ann.get("current_weight_rank") if weight_known else None,
            "current_signed_weight_status": payment_status,
            "chain_incentive": chain["incentive"],
            "chain_emission": chain["emission"],
            "chain": chain,
            "perminer_contribution": pm_visibility,
            "recent_activity": recent_activity,
            "sources": {
                "payment": _source_meta(
                    path="v1/validator/weights/next",
                    status=payment_status,
                    generated_at=weight_ctx.get("generated_at"),
                    note="signed Cathedral weight; validator input",
                ),
                "chain": {
                    "status": "available" if chain["source"] != "unavailable" else "unavailable",
                    "source": chain["source"],
                    "updated_at": chain["updated_at"],
                    "staleness_seconds": chain["staleness_seconds"],
                    "note": "publisher only reports chain annotations when an upstream feed provides them",
                },
                "recent_activity": _source_meta(
                    path="v1/leaderboard/top?view=receipts",
                    status="available" if receipt else "not_found",
                    generated_at=(receipt or {}).get("last_seen"),
                    note="activity/audit signal, not payment order",
                ),
                "perminer": {
                    "status": pm_visibility.get("status"),
                    "source": pm_visibility.get("source"),
                    "generated_at": pm_visibility.get("last_solved_at"),
                    "staleness_seconds": _age_seconds(pm_visibility.get("last_solved_at")),
                },
            },
        }

    def _flatten_visibility(visibility: dict[str, Any]) -> dict[str, Any]:
        return {
            "uid": visibility.get("uid"),
            "registered": visibility.get("registered"),
            "payable": visibility.get("payable"),
            "current_signed_weight": visibility.get("current_signed_weight"),
            "current_signed_weight_rank": visibility.get("current_signed_weight_rank"),
            "current_signed_weight_status": visibility.get("current_signed_weight_status"),
            "chain_incentive": visibility.get("chain_incentive"),
            "chain_emission": visibility.get("chain_emission"),
            "perminer_weighted_units": (
                visibility.get("perminer_contribution") or {}
            ).get("weighted_units"),
            "perminer_unique_verified_solves": (
                visibility.get("perminer_contribution") or {}
            ).get("unique_verified_solves"),
            "recent_activity_rank_24h": (
                visibility.get("recent_activity") or {}
            ).get("receipt_rank_24h"),
            "recent_activity_last_seen": (
                visibility.get("recent_activity") or {}
            ).get("last_seen"),
        }

    def _recent_payload(
        cur_ran_at: str | None,
        cur_id: str | None,
        limit: int,
    ) -> dict[str, Any]:
        items = _rewrite_recent_rows_for_legacy_pm_primary(
            store.recent_rows(cur_ran_at, cur_id, limit)
        )
        if items:
            last = items[-1]
            nxt_ran_at, nxt_id = last["ran_at"], last["id"]
        else:
            nxt_ran_at, nxt_id = cur_ran_at, cur_id
        return {
            "items": items,
            "view": "recent_signed_receipts",
            "rank_kind": "none",
            "explanation": (
                "Recent is an audit stream, not the earning leaderboard. "
                "Use /v1/leaderboard/top?view=weights for current payment rank."
            ),
            "earning_weight_source": "v1/validator/weights/next",
            "earning_weights_generated_at": None,
            "current_weights": {},
            "current_weights_status": "not_included_on_validator_feed",
            "next_since": nxt_ran_at,
            "next_since_ran_at": nxt_ran_at,
            "next_since_id": nxt_id,
            "merkle_epoch_latest": None,
        }

    def _recent_warming_payload(limit: int) -> dict[str, Any]:
        return {
            "items": [],
            "view": "recent_signed_receipts",
            "rank_kind": "none",
            "explanation": "Recent visibility cache is warming; retry shortly.",
            "earning_weight_source": "v1/validator/weights/next",
            "earning_weights_generated_at": None,
            "current_weights": {},
            "current_weights_status": "warming",
            "next_since": None,
            "next_since_ran_at": None,
            "next_since_id": None,
            "merkle_epoch_latest": None,
            "visibility_cache_status": "warming",
            "data_status": "warming",
            "requested_limit": int(limit),
        }

    recent_snapshot_limit = _env_int("CATHEDRAL_RECENT_SNAPSHOT_LIMIT", 50)
    recent_no_cursor_max_limit = _env_int(
        "CATHEDRAL_RECENT_NO_CURSOR_MAX_LIMIT", recent_snapshot_limit
    )
    # Old validators may still consume /leaderboard/recent directly. A cold
    # warming payload is safe for dashboards, but it looks like "no rows" to
    # validator-style clients, so keep the no-cursor compatibility path
    # synchronous unless an operator explicitly opts back into cold async.
    recent_cold_async = _env_bool("CATHEDRAL_RECENT_COLD_ASYNC", False)
    recent_snapshot = MaterializedSnapshot(
        "leaderboard-recent",
        lambda: _recent_payload(None, None, recent_snapshot_limit),
    )
    materialized_snapshot_mod.register(recent_snapshot)
    app.state.leaderboard_recent_snapshot = recent_snapshot

    # ---- M1: feed ---------------------------------------------------------
    @app.get("/v1/leaderboard/recent")
    async def leaderboard_recent(
        request: Request,
        since: str | None = Query(None),
        since_ran_at: str | None = Query(None),
        since_id: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ):
        # legacy ?since= compat: a single watermark seeds the ran_at cursor.
        cur_ran_at = since_ran_at or since
        cur_id = since_id
        cache_status = "cursor"
        if cur_ran_at is None and cur_id is None:
            effective_limit = min(int(limit), max(1, recent_no_cursor_max_limit))
            if materialized_snapshot_mod.enabled() and effective_limit == recent_snapshot_limit:
                served = recent_snapshot.get()
                if served is not None:
                    payload, etag, meta = served
                    headers = snapshot_headers(etag, meta)
                    headers["Access-Control-Allow-Origin"] = "*"
                    headers["X-Cathedral-Cache"] = "materialized"
                    return _conditional_response(payload, etag, headers, request)
            # The no-cursor cold build is synchronous when recent_cold_async is
            # off; a DB error/timeout in _recent_payload would otherwise raise out
            # of the handler as a 500. Degrade to a warming payload (bounded, 200)
            # so a stalled live scan never 500s the public leaderboard — mirrors
            # the cursor path below.
            try:
                payload, cache_status = recent_cache.get(
                    effective_limit,
                    lambda: _recent_payload(None, None, effective_limit),
                    cold_async=recent_cold_async,
                    cold_value=lambda: _recent_warming_payload(effective_limit),
                )
            except Exception as exc:
                print(f"[leaderboard_recent] no-cursor build failed: {exc!r}")
                payload = _recent_warming_payload(effective_limit)
                cache_status = "error_warming"
        else:
            # The cursor path hits Postgres on every call (validators poll it
            # continuously). store.recent_rows() is blocking psycopg2, so run it
            # off the event loop — otherwise a single slow scan freezes this
            # worker and starves the hot path (active-challenges, health),
            # cascading to edge 504s. A bounded statement_timeout (see store.py)
            # surfaces as an exception here; degrade to a warming payload so the
            # validator retries instead of receiving a 500.
            try:
                payload = await asyncio.to_thread(
                    _recent_payload, cur_ran_at, cur_id, limit
                )
            except Exception as exc:
                print(f"[leaderboard_recent] cursor pull failed: {exc!r}")
                payload = _recent_warming_payload(limit)
                cache_status = "error_warming"
        return JSONResponse(
            payload,
            headers={
                "Cache-Control": "public, max-age=5",
                "Access-Control-Allow-Origin": "*",
                "X-Cathedral-Cache": cache_status,
            },
        )

    def _leaderboard_top_payload(view: str) -> dict[str, Any]:
        rows_, built_at, window_h = top_cache.get()
        weight_ctx = _current_weight_context()
        receipt_by_hotkey = {
            str(r.get("miner_hotkey")): {**r, "receipt_rank": i}
            for i, r in enumerate(rows_, start=1)
            if r.get("miner_hotkey")
        }
        try:
            pm_summary = _perminer_summary(250)
            pm_by_hotkey = {
                str(r.get("miner_hotkey")): r
                for r in pm_summary.get("miners", [])
                if r.get("miner_hotkey")
            }
        except Exception as exc:
            print(f"[leaderboard] per-miner summary unavailable: {exc!r}")
            pm_by_hotkey = {}
        requested_view = (view or "weights").strip().lower()
        normalized_view = "weights" if requested_view in {"weight", "weights", "earning", "earnings"} else "receipts"
        if normalized_view == "weights" and weight_ctx["ranked"]:
            miners = []
            for row in weight_ctx["ranked"][:100]:
                hk = row["miner_hotkey"]
                receipt = receipt_by_hotkey.get(hk, {})
                visibility = _miner_visibility_row(
                    hk,
                    weight_ctx,
                    receipt=receipt,
                    pm_summary_row=pm_by_hotkey.get(hk),
                )
                miners.append({
                    "miner_hotkey": hk,
                    "current_weight": row["current_weight"],
                    "current_weight_rank": row["current_weight_rank"],
                    "rank_kind": "current_payment_weight",
                    "receipt_rank_24h": receipt.get("receipt_rank"),
                    "receipt_total_score_24h": receipt.get("total_score"),
                    "receipt_distinct_solves_24h": receipt.get("distinct_solves"),
                    "last_seen": receipt.get("last_seen"),
                    "display_name": receipt.get("display_name"),
                    **_flatten_visibility(visibility),
                    "visibility": visibility,
                })
            rank_kind = "current_payment_weight"
        else:
            miners = []
            for i, row in enumerate(rows_, start=1):
                hk = str(row.get("miner_hotkey") or "")
                ann = _weight_annotations(weight_ctx, [hk]).get(hk, {})
                visibility = _miner_visibility_row(
                    hk,
                    weight_ctx,
                    receipt={**row, "receipt_rank": i},
                    pm_summary_row=pm_by_hotkey.get(hk),
                )
                miners.append({
                    **row,
                    "receipt_rank": i,
                    "rank_kind": "receipt_total_score_24h",
                    "current_weight": ann.get("current_weight"),
                    "current_weight_rank": ann.get("current_weight_rank"),
                    **_flatten_visibility(visibility),
                    "visibility": visibility,
                })
            rank_kind = "receipt_total_score_24h"
        return {
            "miners": miners,
            "view": normalized_view,
            "rank_kind": rank_kind,
            "default_view": "weights",
            "views": {
                "weights": "current Cathedral payment weights; closest API view to Taostats emission",
                "receipts": "24h solve receipt activity; audit/activity view, not payment order",
            },
            "explanation": (
                "Weights show the current payment order. Receipts show recent solve activity. "
                "They can differ during migration and when scoring uses the solve ledger."
            ),
            "earning_weight_source": "v1/validator/weights/next",
            "earning_weights_generated_at": weight_ctx["generated_at"],
            "visibility_schema": "cathedral_miner_truth_v1",
            "sources": {
                "payment": {
                    "path": "v1/validator/weights/next",
                    "status": "available" if weight_ctx.get("generated_at") else "unavailable",
                    "generated_at": weight_ctx.get("generated_at"),
                    "note": "signed Cathedral weight; validator input",
                },
                "recent_activity": {
                    "path": "v1/leaderboard/recent",
                    "status": "activity_only_not_payment",
                },
                "chain": {
                    "status": "upstream_annotation_only",
                    "note": "uid/registered/payable/incentive/emission are null unless a chain feed annotates rows",
                },
                "perminer": {
                    "path": "v1/synthetic-boolean/per-miner/summary",
                    "status": "summary",
                },
            },
            "window_hours": window_h,
            "built_at": built_at,
            "count": len(miners),
            "cache_ttl_secs": 45,
        }

    # Track 3 / item 6: timer-built materialized snapshot of the default
    # leaderboard-top read (view=weights, the dashboard hot path). DEFAULT-OFF —
    # only served when materialized_snapshot.enabled(); otherwise the route builds
    # inline exactly as before. The builder pins view=weights because that is the
    # canonical materialized payload; other views fall through to the live build.
    leaderboard_top_snapshot = MaterializedSnapshot(
        "leaderboard-top", lambda: _leaderboard_top_payload("weights")
    )
    materialized_snapshot_mod.register(leaderboard_top_snapshot)
    app.state.leaderboard_top_snapshot = leaderboard_top_snapshot

    def _leaderboard_top_default_view(view: str) -> bool:
        return (view or "weights").strip().lower() in {
            "weight", "weights", "earning", "earnings"
        }

    @app.get("/v1/leaderboard/top")
    async def leaderboard_top(
        request: Request,
        window: str = Query("24h"),
        view: str = Query("weights"),
    ):
        """Fast pre-aggregated miner ranking. Cached ~45s in-process.
        Defaults to current earning weights. Use view=receipts for the old
        top 100 miners ranked by total weighted_score over the window.
        window=24h only for now (others fall back to 24h).
        """
        # Materialized-snapshot fast path (flag-gated): serve the timer-built
        # default-view payload without touching the per-request build. Only the
        # canonical default view is materialized; receipts/other views fall to live.
        if materialized_snapshot_mod.enabled() and _leaderboard_top_default_view(view):
            served = leaderboard_top_snapshot.get()
            if served is not None:
                payload, etag, meta = served
                headers = snapshot_headers(etag, meta)
                headers["Access-Control-Allow-Origin"] = "*"
                return _conditional_response(payload, etag, headers, request)
        # Flag off, non-default view, or snapshot cold/too-stale: live build path.
        # Degrade to a bounded payload, never a 500, if the live aggregation fails.
        try:
            payload = _leaderboard_top_payload(view)
            cache_status = "live"
        except Exception as exc:
            print(f"[leaderboard_top] live build failed: {exc!r}")
            payload = {"kind": "leaderboard_top", "view": view,
                       "data_status": "degraded", "miners": []}
            cache_status = "error_degraded"
        return JSONResponse(
            payload,
            headers={"Cache-Control": "public, max-age=30",
                     "Access-Control-Allow-Origin": "*",
                     "X-Cathedral-Cache": cache_status},
        )

    def _leaderboard_explain_payload(miner_hotkey: str) -> dict[str, Any]:
        payload = weights_mod.explain_miner_score(store, miner_hotkey)
        weight_ctx = _current_weight_context()
        ann = _weight_annotations(weight_ctx, [miner_hotkey]).get(miner_hotkey, {})
        payload["current_signed_weight"] = ann.get("current_weight")
        payload["current_signed_weight_rank"] = ann.get("current_weight_rank")
        payload["current_signed_weight_generated_at"] = weight_ctx.get("generated_at")
        payload["current_signed_weight_policy_reason"] = weight_ctx.get("policy_reason")
        contribution = None
        try:
            payload.setdefault("perminer", {})
            contribution = _perminer_public_contribution(miner_hotkey)
            payload["perminer"]["contribution"] = contribution
        except Exception as exc:
            payload.setdefault("perminer", {})
            payload["perminer"]["contribution_error"] = f"{type(exc).__name__}"
        try:
            rows_, _built_at, _window_h = top_cache.get()
            receipt = next(
                ({**r, "receipt_rank": i} for i, r in enumerate(rows_, start=1)
                 if str(r.get("miner_hotkey") or "") == miner_hotkey),
                None,
            )
        except Exception:
            receipt = None
        visibility = _miner_visibility_row(
            miner_hotkey,
            weight_ctx,
            receipt=receipt,
            pm_contribution=contribution,
        )
        payload["visibility_schema"] = "cathedral_miner_truth_v1"
        payload["visibility"] = visibility
        payload.update(_flatten_visibility(visibility))
        return payload

    @app.get("/v1/leaderboard/explain")
    async def leaderboard_explain(miner_hotkey: str = Query(..., min_length=1)):
        """Explain how the current scoring policy treats one miner hotkey."""

        def _warming_payload() -> dict[str, Any]:
            return {
                "miner_hotkey": miner_hotkey,
                "visibility_cache_status": "warming",
                "data_status": "warming",
                "visibility_schema": "cathedral_miner_truth_v1",
                "uid": None,
                "registered": None,
                "payable": None,
                "current_signed_weight": None,
                "current_signed_weight_rank": None,
                "current_signed_weight_status": "warming",
                "current_signed_weight_generated_at": None,
                "current_signed_weight_policy_reason": None,
                "chain_incentive": None,
                "chain_emission": None,
                "perminer": {
                    "contribution": {
                        "kind": "per_miner",
                        "enabled": None,
                        "miner_hotkey": miner_hotkey,
                        "eligible": None,
                        "ineligibility_reason": None,
                        "status": "warming",
                    },
                },
                "visibility": {
                    "miner_hotkey": miner_hotkey,
                    "uid": None,
                    "registered": None,
                    "payable": None,
                    "current_signed_weight": None,
                    "current_signed_weight_rank": None,
                    "current_signed_weight_status": "warming",
                    "chain_incentive": None,
                    "chain_emission": None,
                    "chain": {"source": "warming"},
                    "perminer_contribution": {"status": "warming"},
                    "recent_activity": {"rank_kind": "activity_only_not_payment"},
                    "sources": {
                        "payment": {"path": "v1/validator/weights/next", "status": "warming"},
                        "chain": {"status": "warming"},
                        "recent_activity": {"status": "warming"},
                        "perminer": {"status": "warming"},
                    },
                },
            }

        payload, cache_status = explain_cache.get(
            miner_hotkey,
            lambda: _leaderboard_explain_payload(miner_hotkey),
            cold_async=visibility_cold_async,
            cold_value=_warming_payload,
        )
        return JSONResponse(
            payload,
            headers={
                "Cache-Control": "public, max-age=30",
                "Access-Control-Allow-Origin": "*",
                "X-Cathedral-Cache": cache_status,
            },
        )

    # ---- External score intake (publisher-side only) ----------------------
    @app.post("/v1/external-scores/violet")
    async def external_scores_violet(
        request: Request,
        authorization: str | None = Header(None),
        x_cathedral_external_token: str | None = Header(None),
        x_cathedral_external_signature: str | None = Header(None),
    ):
        """Accept Violet's signed/authenticated score report for composition.

        This endpoint never sets weights directly. It stores source scores for
        weights.py to blend into the single Cathedral-signed vector that thin
        validators already verify and apply.

        Body is bounded by CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES (default 1 MiB).
        Rejects with 413 if declared Content-Length exceeds cap or if streaming
        consumption exceeds cap. Preserves exact bytes for JSON parse and HMAC.
        Malformed negative Content-Length is rejected as 400.
        """
        if not external_scores.ingest_enabled():
            raise HTTPException(404, "external_scores_ingest_not_enabled")
        # Read body bounded by CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES (default 1 MiB)
        max_body_bytes = _env_bytes("CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES", 1024 * 1024)
        body = await _read_bounded_body(request, max_body_bytes)
        # Parse JSON early so we can extract and validate source safely.
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            raise HTTPException(400, "invalid_json_report")
        # Payload must be an object/dict; reject arrays, null, and scalars.
        if not isinstance(payload, dict):
            raise HTTPException(400, "invalid_report_contract")
        # Extract source early, before auth, so we can enforce source-scoped credentials.
        try:
            source = external_scores._source(
                payload.get("source") or payload.get("mechanism"),
                default="violet_audio"
            )
        except external_scores.ExternalScoreError as exc:
            raise HTTPException(400, exc.reason)
        # Validate source is allowed for this endpoint.
        if source not in external_scores.ALLOWED_ENDPOINT_SOURCES:
            raise HTTPException(400, "invalid_source_for_violet_endpoint")
        # (#3) If these scores actually feed the real signed vector, require some
        # credential. When blending is live, accept either a dedicated token for
        # this source OR the shared token. Fail 503 if neither is configured.
        if weights_mod.external_scores_enabled():
            has_shared = external_scores.token_configured()
            has_dedicated = external_scores.source_token_configured(source)
            if not (has_shared or has_dedicated):
                raise HTTPException(503, "external_scores_token_required_while_blending")
        # Authorize with source-scoped auth: dedicated token for this source if
        # it exists, otherwise fall back to shared token. Fails closed if no
        # credential matches.
        if not external_scores.bearer_authorized_for_source(source, authorization, x_cathedral_external_token):
            raise HTTPException(401, "invalid_external_scores_token")
        # Verify HMAC with source-specific enforcement: mandatory secrets for
        # certain sources (e.g., cathedral_confidential_tdx).
        is_valid, fail_503 = external_scores.verify_hmac_for_source(
            source, body, x_cathedral_external_signature
        )
        if fail_503:
            raise HTTPException(503, "external_scores_hmac_secret_required")
        if not is_valid:
            raise HTTPException(401, "invalid_external_scores_signature")
        try:
            report = external_scores.normalize_report(payload, default_source="violet_audio")
        except external_scores.ExternalScoreError as exc:
            if exc.reason == "score_audience_not_configured":
                raise HTTPException(503, exc.reason)
            if exc.reason != "report_too_old":
                raise HTTPException(400, exc.reason)
            try:
                report = external_scores.normalize_stale_idempotent_retry(
                    store,
                    payload,
                    default_source="violet_audio",
                )
            except external_scores.ExternalScoreError as retry_exc:
                if retry_exc.reason == "score_audience_not_configured":
                    raise HTTPException(503, retry_exc.reason)
                raise HTTPException(400, retry_exc.reason)
        report = external_scores.bind_authenticated_body(report, body)
        try:
            accepted = external_scores.store_report(store, report)
        except external_scores.ExternalScoreError as exc:
            # A validly authenticated report can still lose the per-source
            # monotonic epoch race. Preserve that exact, non-retryable verdict
            # instead of misreporting it as an infrastructure outage.
            if exc.reason in {"epoch_too_old", "epoch_conflict"}:
                raise HTTPException(409, exc.reason)
            raise HTTPException(400, exc.reason)
        except Exception as exc:
            print(f"[external_scores] store failed: {exc!r}")
            raise HTTPException(503, "external_scores_store_failed")
        return JSONResponse(
            accepted,
            status_code=202,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    # ---- M1b: signed final-scores vector (the v4 scoring interface) -------
    # ONE number per miner + burn, Ed25519-signed, same wire shape deployed
    # validators already verify. All composition (recency window, multi-lane
    # blending, arena payouts) happens orchestrator-side in weights.py — a
    # validator just verifies and applies. The row feed above stays as the
    # public audit trail, not the scoring input.
    @app.get("/v1/validator/weights/next")
    async def validator_weights_next():
        # async def: never uses the thread pool.  current_vector returns from the
        # in-memory cache (populated by the background refresh thread) in
        # microseconds — a plain dict lookup + brief lock acquire.  Making the
        # handler async means concurrent refill fork-gen threads never starve it
        # even when the thread pool is saturated (fork poll holds pool slots).
        # Cold vector builds must never run here; they stall read-origin health.
        weight_key = os.environ.get(weights_mod.SIGNING_KEY_ENV, "").strip() or key_hex
        try:
            vec = weights_mod.cached_vector(store, signing_key_hex=weight_key)
        except Exception:
            import traceback
            print("[weights] vector cache read failed:\n" + traceback.format_exc())
            raise HTTPException(status_code=503, detail="no vector available")
        if vec is None:
            return JSONResponse(
                {"detail": "weights_warming", "status": "warming"},
                status_code=503,
                headers={"Retry-After": "2", "Cache-Control": "no-store"},
            )
        # Origin-side fail-closed: never serve an expired signed vector as 200.
        # Default-on; flip CATHEDRAL_WEIGHTS_ORIGIN_FAILCLOSED=0 to revert to the
        # prior always-serve behavior. Under healthy operation the vector is
        # refreshed every ~60s and expires in ~30min, so this only fires when the
        # refresh is genuinely wedged — exactly when validators must get a retry,
        # not stale consensus.
        if _env_bool("CATHEDRAL_WEIGHTS_ORIGIN_FAILCLOSED", True) and \
                _weights_vector_expired(vec):
            return JSONResponse(
                {
                    "detail": "weights_expired",
                    "status": "expired",
                    "generated_at": vec.get("generated_at") if isinstance(vec, dict) else None,
                    "expires_at": vec.get("expires_at") if isinstance(vec, dict) else None,
                },
                status_code=503,
                headers={"Retry-After": "2", "Cache-Control": "no-store"},
            )
        return JSONResponse(vec)

    @app.get("/.well-known/cathedral-jwks.json")
    def jwks():
        return JSONResponse(jwks_doc)

    def _health_base(kind: str, db: str) -> dict[str, Any]:
        return {
            "status": "ok" if db != "error" else "error",
            "kind": kind,
            "service_role": service_role,
            "db": db,
            "hippius": "ok",
            "polaris": "ok",
            "signing_key": "loaded",
            "sr25519_backend": getattr(verifier, "backend", "bittensor"),
        }

    @app.get("/health/live")
    async def health_live():
        return _health_base("live", "not_checked")

    # Readiness DB probe: OFF the event loop, single-flight, briefly cached.
    # The old inline `store.query("SELECT 1")` was a blocking psycopg2 call in
    # an async handler: under the 2026-07-08 open-v2 flood, one slow probe
    # (pool churn / fresh connect, up to connect_timeout) stalled the whole
    # event loop, so readiness itself timed out (edge 000/520) while the origin
    # was otherwise admitting fine. Same failure class the submit path fixed
    # with its dedicated executor. Probe rules: at most ONE DB ping in flight
    # (1-thread executor + single-flight flag), concurrent callers serve the
    # last-known state (stale-while-revalidate), result cached ~2s, probe
    # bounded by a timeout so readiness answers promptly even when the DB
    # hangs.
    _ready_ttl_secs = max(0.5, _env_float("CATHEDRAL_READY_CACHE_SECS", 2.0))
    _ready_timeout_secs = max(0.5, _env_float("CATHEDRAL_READY_TIMEOUT_SECS", 3.0))
    # Disk headroom gate (2026-07-09 incident): Postgres dies ungracefully at
    # 0 bytes free (WAL write PANIC -> crash -> failed recovery -> 8.5h outage).
    # Failing readiness while headroom remains lets the edge watcher auto-abort
    # an open window BEFORE the DB is damaged. 0 disables the check.
    _ready_min_disk_free_mb = max(0.0, _env_float("CATHEDRAL_READY_MIN_DISK_FREE_MB", 2048.0))
    _ready_disk_path = os.environ.get("CATHEDRAL_READY_DISK_PATH", "/") or "/"
    _ready_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readyz")
    _ready_lock = threading.Lock()
    # Start stale-but-ok: build_app already ran store.migrate() against the DB,
    # so "reachable at startup" is the honest initial state; at=0 forces the
    # first request to refresh.
    _ready_state: dict[str, Any] = {"at": 0.0, "ok": True, "error": "", "refreshing": False}

    def _ready_db_probe() -> tuple[bool, str]:
        try:
            store.query("SELECT 1 AS ok")
        except Exception as exc:
            return False, type(exc).__name__
        if _ready_min_disk_free_mb > 0:
            try:
                free_mb = shutil.disk_usage(_ready_disk_path).free / (1024 * 1024)
            except Exception:
                free_mb = None  # broken statfs must never fail readiness
            if free_mb is not None and free_mb < _ready_min_disk_free_mb:
                return False, f"DiskLow:{int(free_mb)}MB<{int(_ready_min_disk_free_mb)}MB"
        return True, ""

    @app.get("/health/ready")
    async def health_ready():
        refresh = False
        with _ready_lock:
            stale = (time.monotonic() - _ready_state["at"]) >= _ready_ttl_secs
            if stale and not _ready_state["refreshing"]:
                _ready_state["refreshing"] = True
                refresh = True
            ok, err = _ready_state["ok"], _ready_state["error"]
        if refresh:
            try:
                ok, err = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        _ready_executor, _ready_db_probe),
                    timeout=_ready_timeout_secs,
                )
            except asyncio.TimeoutError:
                ok, err = False, "ReadyProbeTimeout"
            except Exception as exc:
                ok, err = False, type(exc).__name__
            finally:
                with _ready_lock:
                    _ready_state.update(
                        at=time.monotonic(), ok=ok, error=err, refreshing=False)
        if ok:
            return _health_base("ready", "ok")
        payload = _health_base("ready", "error")
        payload["error"] = err
        return JSONResponse(payload, status_code=503)

    @app.get("/health")
    async def health():
        payload = _health_base("live", "not_checked")
        payload["ready_path"] = "/health/ready"
        return payload

    # ---- M2: Lane A read --------------------------------------------------
    @app.get("/v1/synthetic-boolean/active-challenges")
    async def active_challenges_list(request: Request):
        # Broadcast tier: serve the cached board snapshot (rebuilt only on
        # mint/retire + TTL), with ETag/Cache-Control so an edge/CDN caches it
        # and a conditional GET short-circuits to 304 — reads don't touch the DB.
        return _serve_board_snapshot(request)

    @app.get("/v1/synthetic-boolean/challenge-broadcast")
    async def challenge_broadcast(request: Request):
        # Explicit alias for miners/dashboards that want the cache/CDN board
        # broadcast rather than a per-request active-set query.
        return _serve_board_snapshot(request)

    @app.get("/sat/latest.json")
    async def sat_latest(request: Request):
        latest, _board, _weights, etag = _sat_snapshot_bundle()
        headers = _sat_snapshot_headers(etag, str(latest["sequence"]))
        inm = request.headers.get("if-none-match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            return Response(status_code=304, headers=headers)
        return JSONResponse(latest, headers=headers)

    @app.get("/sat/sequences/{sequence}/board.json")
    async def sat_sequence_board(sequence: str, request: Request):
        latest, board_payload, _weights, _latest_etag = _sat_snapshot_bundle()
        current_sequence = str(latest["sequence"])
        if sequence != current_sequence:
            raise HTTPException(
                404,
                {
                    "detail": "snapshot_sequence_not_available",
                    "current_sequence": current_sequence,
                },
            )
        etag = _snapshot_etag(board_payload)
        headers = _sat_snapshot_headers(etag, sequence, immutable=True)
        inm = request.headers.get("if-none-match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            return Response(status_code=304, headers=headers)
        return JSONResponse(board_payload, headers=headers)

    @app.get("/sat/sequences/{sequence}/weights.json")
    async def sat_sequence_weights(sequence: str, request: Request):
        latest, _board, weights_payload, _latest_etag = _sat_snapshot_bundle()
        current_sequence = str(latest["sequence"])
        if sequence != current_sequence:
            raise HTTPException(
                404,
                {
                    "detail": "snapshot_sequence_not_available",
                    "current_sequence": current_sequence,
                },
            )
        etag = _snapshot_etag(weights_payload)
        headers = _sat_snapshot_headers(etag, sequence, immutable=True)
        inm = request.headers.get("if-none-match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            return Response(status_code=304, headers=headers)
        return JSONResponse(weights_payload, headers=headers)

    @app.get("/sat/events")
    async def sat_events(request: Request, once: bool = Query(False)):
        heartbeat_secs = max(1.0, _env_float("CATHEDRAL_SSE_HEARTBEAT_SECS", 30.0))

        def _snapshot_event_frame(latest: dict[str, Any]) -> str:
            event = {
                "kind": "cathedral.sat.snapshot.ready",
                "sequence": latest["sequence"],
                "lane": "sat",
                "type": "snapshot",
                "latest_url": "/sat/latest.json",
                "artifact_hash": _snapshot_hash(latest),
            }
            return (
                "retry: 30000\n"
                f"id: {latest['sequence']}\n"
                "event: cathedral.sat.snapshot\n"
                f"data: {json.dumps(event, sort_keys=True, separators=(',', ':'))}\n\n"
            )

        async def _stream():
            last_sequence = request.headers.get("last-event-id", "")
            while True:
                latest, _board, _weights, _latest_etag = _sat_snapshot_bundle()
                sequence = str(latest["sequence"])
                if sequence != last_sequence:
                    yield _snapshot_event_frame(latest)
                    last_sequence = sequence
                else:
                    yield ": keepalive\n\n"
                if once or await request.is_disconnected():
                    return
                await asyncio.sleep(heartbeat_secs)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
            },
        )

    @app.get("/v1/synthetic-boolean/current-challenge")
    async def current_challenge(tier: int | None = Query(None), difficulty: str | None = Query(None)):
        if tier is not None and tier < 0:
            raise HTTPException(400, "tier must be >= 0")
        payload, _etag = board_cache.get()
        actives = list(payload.get("items") or [])
        if tier is not None:
            actives = [r for r in actives if int(r.get("tier") or 0) == tier]
        if difficulty is not None:
            labeled = [r for r in actives if r.get("difficulty_label") == difficulty]
            if labeled:
                actives = labeled
        if not actives:
            raise HTTPException(404, "no_active_challenge")
        return actives[0]

    @app.get("/v1/synthetic-boolean/active-cnf")
    def active_cnf(
        challenge_id: str | None = Query(None),
        tier: int | None = Query(None),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str | None = Header(None),
    ):
        if x_cathedral_submitted_at is None:
            raise HTTPException(401, "missing X-Cathedral-Submitted-At")
        if challenge_id and tier is not None:
            raise HTTPException(400, "use either challenge_id or tier, not both")
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, x_cathedral_submitted_at,
            challenge_id="", dimacs_solution_sha256="",
        )
        if challenge_id:
            rows_ = store.query(
                "SELECT * FROM lane_challenges WHERE challenge_id=? AND status='active'",
                (challenge_id,))
        else:
            rows_ = _active_challenges(tier)
        if not rows_:
            raise HTTPException(404, "no_active_challenge")
        c = rows_[0]
        cid = c["challenge_id"]
        token = _mint_token(cid)
        return {
            "challenge_id": cid, "tier": c["tier"], "cnf_sha256": c["cnf_sha256"],
            "cnf_url": _public_cnf_url(f"/v1/challenges/{cid}/cnf?t={token}"),
        }

    @app.get("/v1/challenges/{challenge_id}/cnf")
    def fetch_cnf(challenge_id: str, t: str = Query(...)):
        # opaque 404 on bad/expired token or unknown challenge — no signal leak.
        # HMAC token gate is preserved in BOTH cnf backends; only AFTER it passes
        # do we resolve where the immutable body lives (db inline | bucket 302).
        if not _check_token(challenge_id, t):
            raise HTTPException(404, "not found")
        result = cnf_store.serve(challenge_id)
        if result.mode == "not_found":
            raise HTTPException(404, "not found")
        if result.mode == "redirect":
            # bucket backend: 302 to a presigned URL so bytes stream from the
            # edge/CDN, not the publisher or the DB (the flood evaporates there).
            return Response(status_code=302,
                            headers={"Location": result.url, **(result.headers or {})})
        # db backend: inline body with immutable cache headers for edge caching.
        return PlainTextResponse(result.text, headers=result.headers or {})

    # ---- M2a: public readiness probe (non-scored toy self-test) -----------
    # Miners fetch this to verify their fetch->solve->verify pipeline before
    # mining. No auth, no token, never scored. Backend-compat with the prior
    # publisher (clients that gate mining on this 404'd on v4 otherwise).
    @app.get("/v1/synthetic-boolean/readiness-probe")
    def readiness_probe():
        base = os.environ.get("CATHEDRAL_PUBLIC_BASE_URL",
                              "https://api.cathedral.computer").rstrip("/")
        return {
            "capability": _FAMILY, "purpose": "readiness_probe",
            "emissions_eligible": False, "weighted_score": 0.0,
            "public_input": {
                "format": "dimacs",
                "cnf_url": f"{base}/api/cathedral/v1/synthetic-boolean/readiness-probe/cnf",
                "cnf_sha256": _READINESS_SHA, "num_vars": 3, "num_clauses": 3,
            },
            "answer_format": {"type": "FINAL_ANSWER", "json_keys": ["dimacs_solution"]},
        }

    @app.get("/v1/synthetic-boolean/readiness-probe/cnf")
    def readiness_probe_cnf():
        return PlainTextResponse(_READINESS_CNF, media_type="text/plain; charset=utf-8")

    @app.post("/v1/synthetic-boolean/readiness-probe/verify")
    async def readiness_probe_verify(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        sol = body.get("dimacs_solution") if isinstance(body, dict) else None
        check = verify_dimacs_solution(_READINESS_CNF, sol if isinstance(sol, str) else None)
        return {
            "valid": check.ok,
            "rejection_reason": None if check.ok else check.rejection_reason,
            "clause_count": 3, "weighted_score": 0.0, "emissions_eligible": False,
        }

    # ---- M2c: Audit scanner bridge (default-off, replay-scored only) ------
    # This is the production-style bridge for the local Subnet Breaker scanner
    # contract. It is not wired into SAT payment weights here; it gives miners
    # a signed, replay-backed submission path that can be enabled deliberately.
    def _audit_scanner_enabled() -> bool:
        return os.environ.get("CATHEDRAL_AUDIT_SCANNER_ENABLED", "").strip().lower() in {
            "1", "true", "yes", "on"}

    def _audit_scanner_example_solutions_enabled() -> bool:
        return os.environ.get(
            "CATHEDRAL_AUDIT_SCANNER_EXAMPLE_SOLUTIONS_ENABLED",
            "",
        ).strip().lower() in {"1", "true", "yes", "on"}

    def _require_audit_scanner_enabled() -> None:
        if not _audit_scanner_enabled():
            raise HTTPException(404, "audit_scanner_not_enabled")

    def _audit_scanner_module():
        from game.arena import scanner as audit_scanner

        return audit_scanner

    def _audit_scanner_ledger_path() -> str:
        return os.environ.get(
            "CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH",
            "audit_scanner_submissions.jsonl",
        )

    def _audit_scanner_submission_from_payload(
        payload: dict[str, Any],
        *,
        signed_hotkey: str | None = None,
    ):
        audit_scanner = _audit_scanner_module()
        task_id = str(payload.get("task_id", ""))
        task = audit_scanner.task_by_id(task_id)
        if task is None:
            raise HTTPException(404, "unknown_audit_scanner_task")
        payload_hotkey = str(payload.get("miner_hotkey") or signed_hotkey or "")
        if signed_hotkey and payload_hotkey and payload_hotkey != signed_hotkey:
            raise HTTPException(400, "miner_hotkey_mismatch")
        sub = audit_scanner.ScannerSubmission(
            task_id=task_id,
            miner_hotkey=signed_hotkey or payload_hotkey,
            nonce=str(payload.get("nonce", "")),
            proof_family=str(payload.get("proof_family", "")),
            witness=payload.get("witness"),
            trace=payload.get("trace") if isinstance(payload.get("trace"), list) else [],
            claim=payload.get("claim") or {},
            report=str(payload.get("report", "")),
        )
        return audit_scanner, task, sub

    def _audit_scanner_trace_id(entry: dict[str, Any]) -> str:
        body = {
            "task_id": entry.get("task_id"),
            "miner_hotkey": entry.get("miner_hotkey"),
            "artifact_sha256": entry.get("artifact_sha256"),
            "accepted": bool(entry.get("accepted")),
            "created_at": entry.get("created_at"),
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return "trace-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def _audit_scanner_public_verifier(entry: dict[str, Any]) -> dict[str, Any]:
        verifier = entry.get("verifier") if isinstance(entry.get("verifier"), dict) else {}
        try:
            score = float(verifier.get("score", entry.get("score") or 0.0))
        except (TypeError, ValueError):
            score = 0.0
        return {
            "schema": verifier.get("schema", "cathedral.scanner_verdict.v1"),
            "accepted": bool(verifier.get("accepted", entry.get("accepted"))),
            "score": score,
            "gates": dict(verifier.get("gates") or entry.get("gates") or {}),
            "reasons": list(verifier.get("reasons") or entry.get("reasons") or []),
            "replay_target_id": (
                verifier.get("replay_target_id")
                or entry.get("replay_target_id")
                or ""
            ),
            "artifact_sha256": (
                verifier.get("artifact_sha256")
                or entry.get("artifact_sha256")
                or ""
            ),
        }

    def _audit_scanner_public_entry(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "cathedral.audit_scanner.public_submission.v1",
            "created_at": entry.get("created_at"),
            "task_id": entry.get("task_id") or "",
            "target_netuid": entry.get("target_netuid"),
            "target_name": entry.get("target_name") or "",
            "replay_target_id": entry.get("replay_target_id") or "",
            "expected_family": entry.get("expected_family") or "",
            "miner_hotkey": entry.get("miner_hotkey") or "",
            "accepted": bool(entry.get("accepted")),
            "score": float(entry.get("score") or 0.0),
            "reasons": list(entry.get("reasons") or []),
            "gates": dict(entry.get("gates") or {}),
            "artifact_sha256": entry.get("artifact_sha256") or "",
            "claim_sha256": entry.get("claim_sha256") or "",
            "claim_present": bool(entry.get("claim_present")),
            "claim_valid": bool(entry.get("claim_valid")),
            "verifier": _audit_scanner_public_verifier(entry),
            "redaction": {
                "artifact_body_exported": False,
                "witness_exported": False,
                "report_body_exported": False,
                "trace_body_exported": False,
                "observed_values_exported": False,
            },
        }

    def _audit_scanner_public_trace(entry: dict[str, Any]) -> dict[str, Any]:
        audit_scanner = _audit_scanner_module()
        accepted = bool(entry.get("accepted"))
        return {
            "schema": getattr(audit_scanner, "SCHEMA_AUDIT_TRACE", "cathedral.audit_trace.v1"),
            "trace_id": _audit_scanner_trace_id(entry),
            "label": "accepted" if accepted else "rejected",
            "training_use": (
                "positive_replay_witness"
                if accepted else "negative_replay_failure"
            ),
            "created_at": entry.get("created_at"),
            "miner_hotkey": entry.get("miner_hotkey") or "",
            "task_id": entry.get("task_id") or "",
            "target_netuid": entry.get("target_netuid"),
            "target_name": entry.get("target_name") or "",
            "replay_target_id": entry.get("replay_target_id") or "",
            "expected_family": entry.get("expected_family") or "",
            "artifact_sha256": entry.get("artifact_sha256") or "",
            "claim_sha256": entry.get("claim_sha256") or "",
            "verifier": _audit_scanner_public_verifier(entry),
            "redaction": {
                "artifact_body_exported": False,
                "witness_exported": False,
                "report_body_exported": False,
                "trace_body_exported": False,
                "observed_values_exported": False,
            },
        }

    def _verify_audit_scanner_claim(
        payload: dict[str, Any],
        *,
        x_cathedral_hotkey: str,
        x_cathedral_signature: str,
        x_cathedral_submitted_at: str | None,
    ):
        if x_cathedral_submitted_at is None:
            raise HTTPException(401, "missing X-Cathedral-Submitted-At")
        audit_scanner, task, sub = _audit_scanner_submission_from_payload(
            payload,
            signed_hotkey=x_cathedral_hotkey,
        )
        artifact_sha = audit_scanner._sha(sub.as_artifact())
        verified_at = _verify_hotkey_claim(
            x_cathedral_hotkey,
            x_cathedral_signature,
            x_cathedral_submitted_at,
            challenge_id=task.task_id,
            dimacs_solution_sha256=artifact_sha,
            allow_fallback_shapes=False,
            card_id=_AUDIT_SCANNER_CARD,
        )
        return audit_scanner, task, sub, artifact_sha, verified_at

    def _audit_scanner_schema_contract() -> dict[str, Any]:
        audit_scanner = _audit_scanner_module()
        return {
            "schema": "cathedral.audit_scanner.contract.v1",
            "enabled": _audit_scanner_enabled(),
            "card_id": _AUDIT_SCANNER_CARD,
            "payment_weights": False,
            "scoring": {
                "linear_metric": "task.bounty_weight",
                "boolean_gate": [
                    "hotkey_signature_valid",
                    "task_matches",
                    "nonce_matches",
                    "proof_family_matches",
                    "required_witness_fields_present",
                    "deterministic_replay_succeeds",
                    "not_duplicate_credit",
                ],
                "reports_score": False,
                "claims_score": False,
                "category_scoring": "metadata_only_replay_required",
            },
            "signature_contract": {
                "card_id": _AUDIT_SCANNER_CARD,
                "challenge_id": "task_id",
                "dimacs_solution_sha256": "sha256(canonical_submission_artifact)",
                "artifact_hash_helper": "game.arena.scanner._sha(submission.as_artifact())",
                "headers": [
                    "X-Cathedral-Hotkey",
                    "X-Cathedral-Signature",
                    "X-Cathedral-Submitted-At",
                ],
            },
            "submission_schema": {
                "schema": getattr(audit_scanner, "SCHEMA_SUBMISSION", "cathedral.scanner.submission.v1"),
                "required_fields": [
                    "task_id",
                    "miner_hotkey",
                    "nonce",
                    "proof_family",
                    "witness",
                ],
                "optional_fields": [
                    "trace",
                    "claim",
                    "report",
                ],
                "witness_shape": "object keyed by task.required_fields",
                "trace_shape": "array of tool/action metadata; stored privately, public traces export hashes/labels only",
                "claim_schema": getattr(audit_scanner, "SCHEMA_CLAIM", "cathedral.scanner.claim.v1"),
                "accepted_claim_categories": list(getattr(audit_scanner, "CLAIM_CATEGORIES", ())),
            },
            "redaction_policy": {
                "public_submissions_export_raw_artifact": False,
                "public_traces_export_raw_witness": False,
                "public_traces_export_raw_report": False,
                "public_traces_export_trace_body": False,
                "example_solution_exported_by_default": False,
            },
            "endpoints": {
                "status": "/v1/audit-scanner/status",
                "schema": "/v1/audit-scanner/schema",
                "catalog": "/v1/audit-scanner/catalog",
                "families": "/v1/audit-scanner/families",
                "task": "/v1/audit-scanner/task?index=0",
                "example": "/v1/audit-scanner/example?index=0",
                "replay": "/v1/audit-scanner/replay",
                "submit": "/v1/audit-scanner/submit",
                "leaderboard": "/v1/audit-scanner/leaderboard",
                "benchmark": "/v1/audit-scanner/benchmark",
                "differential": "/v1/audit-scanner/differential",
                "submissions": "/v1/audit-scanner/submissions?limit=50",
                "traces": "/v1/audit-scanner/traces?limit=50",
                "state": "/v1/audit-scanner/state?miner_hotkey=...",
            },
        }

    @app.get("/v1/audit-scanner/status")
    def audit_scanner_status():
        return {
            "schema": "cathedral.audit_scanner.status.v1",
            "enabled": _audit_scanner_enabled(),
            "card_id": _AUDIT_SCANNER_CARD,
            "payment_weights": False,
            "scoring": "local_replay_only_until_promoted_to_weight_policy",
            "signature_contract": {
                "card_id": _AUDIT_SCANNER_CARD,
                "challenge_id": "task_id",
                "dimacs_solution_sha256": "artifact_sha256",
                "headers": [
                    "X-Cathedral-Hotkey",
                    "X-Cathedral-Signature",
                "X-Cathedral-Submitted-At",
                ],
            },
            "example_policy": {
                "default": "redacted",
                "raw_solution_requires": (
                    "CATHEDRAL_AUDIT_SCANNER_EXAMPLE_SOLUTIONS_ENABLED=1 "
                    "and include_solution=true"
                ),
            },
            "endpoints": {
                "schema": "/v1/audit-scanner/schema",
                "catalog": "/v1/audit-scanner/catalog",
                "families": "/v1/audit-scanner/families",
                "task": "/v1/audit-scanner/task?index=0",
                "request": "/v1/audit-scanner/request",
                "replay": "/v1/audit-scanner/replay",
                "submit": "/v1/audit-scanner/submit",
                "leaderboard": "/v1/audit-scanner/leaderboard",
                "benchmark": "/v1/audit-scanner/benchmark",
                "differential": "/v1/audit-scanner/differential",
                "submissions": "/v1/audit-scanner/submissions?limit=50",
                "traces": "/v1/audit-scanner/traces?limit=50",
                "state": "/v1/audit-scanner/state?miner_hotkey=...",
            },
        }

    @app.get("/v1/audit-scanner/schema")
    def audit_scanner_schema():
        return _audit_scanner_schema_contract()

    @app.get("/v1/audit-scanner/families")
    def audit_scanner_families():
        _require_audit_scanner_enabled()
        taxonomy = _audit_scanner_module().family_taxonomy()
        taxonomy["payment_weights"] = False
        taxonomy["scoring"] = "claim_family_routes_work_replay_scores_work"
        taxonomy["category_scoring"] = "claim_category_is_metadata_only"
        taxonomy["reward_gate"] = "deterministic_replay"
        return taxonomy

    @app.get("/v1/audit-scanner/catalog")
    def audit_scanner_catalog(limit: int | None = Query(None, ge=1, le=50)):
        _require_audit_scanner_enabled()
        audit_scanner = _audit_scanner_module()
        tasks = audit_scanner.benchmark_catalog(limit=limit)
        return {"schema": "cathedral.audit_scanner.catalog.v1",
                "count": len(tasks),
                "tasks": [t.manifest() for t in tasks]}

    @app.get("/v1/audit-scanner/task")
    def audit_scanner_task(index: int = Query(0, ge=0)):
        _require_audit_scanner_enabled()
        return _audit_scanner_module().issue_task(index).manifest()

    @app.get("/v1/audit-scanner/example")
    def audit_scanner_example(
        index: int = Query(0, ge=0),
        include_solution: bool = Query(False),
    ):
        _require_audit_scanner_enabled()
        audit_scanner = _audit_scanner_module()
        task = audit_scanner.issue_task(index)
        sub = audit_scanner.example_accepted_submission(task)
        verdict = audit_scanner.verify_submission(task, sub)
        artifact = sub.as_artifact()
        artifact_sha = audit_scanner._sha(artifact)
        if include_solution:
            if not _audit_scanner_example_solutions_enabled():
                raise HTTPException(403, "audit_scanner_example_solution_not_enabled")
            return {
                "schema": "cathedral.audit_scanner.example.v1",
                "task": task.manifest(),
                "submission": artifact,
                "artifact_sha256": artifact_sha,
                "verdict": verdict.as_dict(),
                "solution_exported": True,
                "payment_weights": False,
            }
        return {
            "schema": "cathedral.audit_scanner.example.v1",
            "task": task.manifest(),
            "submission": {
                "schema": artifact.get("schema", "cathedral.scanner_submission.v1"),
                "task_id": artifact.get("task_id", task.task_id),
                "miner_hotkey": "replace_with_your_hotkey",
                "nonce": artifact.get("nonce", ""),
                "proof_family": artifact.get("proof_family", task.expected_family),
                "claim_schema": artifact.get("claim_schema", getattr(audit_scanner, "SCHEMA_CLAIM", "")),
                "claim_sha256": artifact.get("claim_sha256", ""),
                "report_sha256": artifact.get("report_sha256", ""),
                "witness": None,
                "trace": [],
                "claim": {
                    "schema": getattr(audit_scanner, "SCHEMA_CLAIM", ""),
                    "category": task.expected_family,
                    "description": "redacted example; submit your own replayable witness",
                },
            },
            "artifact_sha256": artifact_sha,
            "verdict": _audit_scanner_public_verifier({
                "accepted": verdict.accepted,
                "score": verdict.score,
                "gates": verdict.gates,
                "reasons": verdict.reasons,
                "replay_target_id": verdict.replay_target_id,
                "artifact_sha256": artifact_sha,
            }),
            "solution_exported": False,
            "redaction": {
                "witness_exported": False,
                "report_body_exported": False,
                "trace_body_exported": False,
                "observed_values_exported": False,
            },
            "payment_weights": False,
        }

    @app.post("/v1/audit-scanner/request")
    async def audit_scanner_request(request: Request):
        _require_audit_scanner_enabled()
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return _audit_scanner_module().intake_scan_request(
            payload if isinstance(payload, dict) else {})

    @app.post("/v1/audit-scanner/replay")
    async def audit_scanner_replay(
        request: Request,
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str | None = Header(None),
    ):
        _require_audit_scanner_enabled()
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        audit_scanner, task, sub, artifact_sha, verified_at = _verify_audit_scanner_claim(
            payload if isinstance(payload, dict) else {},
            x_cathedral_hotkey=x_cathedral_hotkey,
            x_cathedral_signature=x_cathedral_signature,
            x_cathedral_submitted_at=x_cathedral_submitted_at,
        )
        verdict = audit_scanner.verify_submission(task, sub).as_dict()
        verdict.update({
            "ledger_written": False,
            "scored": False,
            "signed_artifact_sha256": artifact_sha,
            "signature_verified_at": verified_at,
            "card_id": _AUDIT_SCANNER_CARD,
        })
        return verdict

    @app.post("/v1/audit-scanner/submit")
    async def audit_scanner_submit(
        request: Request,
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str | None = Header(None),
    ):
        _require_audit_scanner_enabled()
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        audit_scanner, task, sub, artifact_sha, verified_at = _verify_audit_scanner_claim(
            payload if isinstance(payload, dict) else {},
            x_cathedral_hotkey=x_cathedral_hotkey,
            x_cathedral_signature=x_cathedral_signature,
            x_cathedral_submitted_at=x_cathedral_submitted_at,
        )
        verdict = audit_scanner.record_submission(
            _audit_scanner_ledger_path(),
            task,
            sub,
        )
        verdict.update({
            "ledger_written": True,
            "scored": bool(verdict["accepted"]),
            "signed_artifact_sha256": artifact_sha,
            "signature_verified_at": verified_at,
            "card_id": _AUDIT_SCANNER_CARD,
            "payment_weights": False,
        })
        return verdict

    @app.get("/v1/audit-scanner/leaderboard")
    def audit_scanner_leaderboard():
        _require_audit_scanner_enabled()
        return _audit_scanner_module().leaderboard(_audit_scanner_ledger_path())

    @app.get("/v1/audit-scanner/benchmark")
    def audit_scanner_benchmark():
        _require_audit_scanner_enabled()
        return _audit_scanner_module().benchmark(_audit_scanner_ledger_path())

    @app.get("/v1/audit-scanner/differential")
    def audit_scanner_differential():
        _require_audit_scanner_enabled()
        from game.arena import replay_differential

        report = replay_differential.differential_report()
        report["payment_weights"] = False
        report["scoring"] = "verifier_quality_gate_only"
        return report

    @app.get("/v1/audit-scanner/submissions")
    def audit_scanner_submissions(limit: int = Query(50, ge=1, le=500)):
        _require_audit_scanner_enabled()
        entries = _audit_scanner_module().read_ledger(_audit_scanner_ledger_path())
        rows = list(reversed(entries))[:limit]
        return {
            "schema": "cathedral.audit_scanner.submissions.v1",
            "count": len(rows),
            "total": len(entries),
            "limit": limit,
            "order": "newest_first",
            "entries": [_audit_scanner_public_entry(entry) for entry in rows],
            "contains_witnesses": False,
            "contains_reports": False,
            "contains_trace_bodies": False,
            "payment_weights": False,
        }

    @app.get("/v1/audit-scanner/traces")
    def audit_scanner_traces(
        miner_hotkey: str = Query("", max_length=128),
        limit: int = Query(50, ge=1, le=500),
    ):
        _require_audit_scanner_enabled()
        audit_scanner = _audit_scanner_module()
        entries = audit_scanner.read_ledger(_audit_scanner_ledger_path())
        if miner_hotkey:
            entries = [
                entry for entry in entries
                if entry.get("miner_hotkey") == miner_hotkey
            ]
        rows = list(reversed(entries))[:limit]
        traces = [_audit_scanner_public_trace(entry) for entry in rows]
        accepted_count = sum(1 for trace in traces if trace["label"] == "accepted")
        return {
            "schema": getattr(
                audit_scanner,
                "SCHEMA_AUDIT_TRACE_DATASET",
                "cathedral.audit_trace_dataset.v1",
            ),
            "trace_schema": getattr(audit_scanner, "SCHEMA_AUDIT_TRACE", "cathedral.audit_trace.v1"),
            "count": len(traces),
            "accepted": accepted_count,
            "rejected": len(traces) - accepted_count,
            "total": len(entries),
            "limit": limit,
            "order": "newest_first",
            "miner_hotkey": miner_hotkey,
            "label_source": "deterministic_replay_verdict",
            "scoring": "accepted replay is positive label; rejected replay is negative label",
            "redaction_policy": (
                "public publisher traces export hashes and labels only; raw "
                "witnesses, reports, submitted trace bodies, and observed values stay private"
            ),
            "traces": traces,
            "contains_witnesses": False,
            "contains_reports": False,
            "contains_trace_bodies": False,
            "payment_weights": False,
        }

    @app.get("/v1/audit-scanner/state")
    def audit_scanner_state(miner_hotkey: str = Query(..., min_length=1)):
        _require_audit_scanner_enabled()
        return _audit_scanner_module().miner_state(
            _audit_scanner_ledger_path(),
            miner_hotkey,
        )

    # ---- M2b: Per-miner challenge endpoints (CATHEDRAL_PERMINER_ENABLED) ----
    # These endpoints are completely new — no existing miner client calls them.
    # Flag-off: both routes return 404 immediately, zero change to existing paths.
    # Flag-on: miners can opt in by calling these instead of active-challenges.
    #
    # Authentication: same X-Cathedral-Hotkey + X-Cathedral-Signature + Submitted-At
    # pattern as active-cnf (the 6-field claim shape, challenge_id="", solution="").
    # The hotkey IS the identity of the challenge set — no hotkey, no set.

    recorded_pm_assignment_pages: set[tuple[str, int, int, int]] = set()
    recorded_pm_assignment_lock = threading.Lock()

    def _perminer_record_listing_assignments() -> bool:
        return os.environ.get(
            "CATHEDRAL_PERMINER_RECORD_LISTING_ASSIGNMENTS", ""
        ).strip().lower() in {"1", "true", "yes", "on"}

    def _record_one_perminer_assignment(
        hotkey: str,
        epoch: int,
        challenge_id: str,
        tier: int,
        seq: int,
    ) -> None:
        from . import per_miner as pm

        assigned_at = _now_iso_ms()
        difficulty_weight = pm.weight_for(tier)

        def _do(conn):
            conn.execute(
                "INSERT OR IGNORE INTO per_miner_assignments"
                "(challenge_id, miner_hotkey, epoch, tier, seq, difficulty_weight, assigned_at_iso) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (challenge_id, hotkey, epoch, int(tier), int(seq),
                 float(difficulty_weight), assigned_at))
        store.write(_do)

    def _record_perminer_assignments(
        hotkey: str,
        epoch: int,
        items: list[dict[str, Any]],
        *,
        offset: int,
        limit: int,
    ) -> None:
        page_key = (hotkey, int(epoch), int(offset), int(limit))
        with recorded_pm_assignment_lock:
            if page_key in recorded_pm_assignment_pages:
                return
        assigned_at = _now_iso_ms()

        def _do(conn):
            for item in items:
                conn.execute(
                    "INSERT OR IGNORE INTO per_miner_assignments"
                    "(challenge_id, miner_hotkey, epoch, tier, seq, difficulty_weight, assigned_at_iso) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item["challenge_id"], hotkey, epoch, int(item["tier"]),
                     int(item["seq"]), float(item["difficulty_weight"]), assigned_at))
        store.write(_do)
        with recorded_pm_assignment_lock:
            if len(recorded_pm_assignment_pages) > 100_000:
                recorded_pm_assignment_pages.clear()
            recorded_pm_assignment_pages.add(page_key)

    def _resolve_perminer_tier_seq(
        pm,
        hotkey: str,
        epoch: int,
        challenge_id: str,
        tier: int | None,
        seq: int | None,
    ) -> tuple[int, int] | None:
        return pm.resolve_tier_seq_for(
            hotkey, epoch, challenge_id, tier=tier, seq=seq)

    def _require_perminer_ready(pm) -> None:
        try:
            pm.require_seed_secret()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    def _require_v2_perminer_ready(pm) -> None:
        # V2 twin of _require_perminer_ready. pm.require_seed_secret() gates on
        # the unprefixed CATHEDRAL_PERMINER_ENABLED, which startup env pinning
        # (v2_pipeline.pin_v2_pm_env) deliberately leaves unset -- through it
        # the missing-seed check would silently pass and per-miner ids would
        # fall back to the ephemeral per-process seed. Gate on the V2 flag;
        # seed_secret_configured() sees the pinned (or v2_pm_env-bridged)
        # secret either way.
        if v2_pipeline.v2_perminer_enabled() and not pm.seed_secret_configured():
            raise HTTPException(503, "per_miner_seed_secret_missing")

    def _perminer_epoch_for(pm, challenge_id: str | None = None) -> int:
        current = pm.current_epoch()
        if not challenge_id:
            return current
        epoch = pm.challenge_epoch(challenge_id) or current
        if epoch not in {current, current - 1}:
            raise HTTPException(410, "per_miner_challenge_expired")
        return epoch

    def _assignment_identity_for_hotkey(hotkey: str) -> str:
        identity = weights_mod.scoring_identity_for_hotkey(
            store,
            hotkey,
            require_mapped=False,
        )
        return identity or hotkey

    def _since_24h_iso() -> str:
        dt = datetime.now(timezone.utc) - timedelta(hours=24)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    def _tier_mix_from_rows(rows_: list[dict[str, Any]], *, count_key: str = "solves") -> list[dict[str, Any]]:
        return [
            {
                "tier": int(r["tier"]),
                count_key: int(r["solves"] or 0),
                "weighted_units": round(float(r["units"] or 0.0), 6),
            }
            for r in rows_
        ]

    def _row_get(row: Any, key: str, default: Any = None) -> Any:
        if row is None:
            return default
        try:
            return row[key]
        except Exception:
            return default

    def _reason_counts_for(
        *,
        miner_hotkey: str | None = None,
        epoch: int | None = None,
        since_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        # Exclude pm-* async SHADOW twins so the (default-off) shadow diagnostic
        # never alters miner-facing attempt/reason stats. When shadow is off there
        # are no such rows and this clause is a no-op.
        clauses = ["status != 'ranked'",
                   "(challenge_kind IS NULL OR challenge_kind != 'per_miner_shadow')"]
        params: list[Any] = []
        if miner_hotkey is not None:
            clauses.append("miner_hotkey=?")
            params.append(miner_hotkey)
        if epoch is not None:
            clauses.append("epoch=?")
            params.append(epoch)
        if since_iso is not None:
            clauses.append("recorded_at_iso > ?")
            params.append(since_iso)
        sql = (
            "SELECT COALESCE(rejection_reason, 'unknown') AS reason, COUNT(*) AS attempts "
            "FROM per_miner_attempts WHERE " + " AND ".join(clauses) +
            " GROUP BY COALESCE(rejection_reason, 'unknown') ORDER BY attempts DESC, reason"
        )
        return [
            {"reason": str(r["reason"]), "attempts": int(r["attempts"] or 0)}
            for r in store.query(sql, tuple(params))
        ]

    def _pm_solve_stats_for(miner_hotkey: str, *, epoch: int | None, since_iso: str | None) -> dict[str, Any]:
        if epoch is None and since_iso is None:
            return {
                "accepted_solves": 0,
                "verified_solves": 0,
                "unique_verified_solves": 0,
                "eligible_solves": 0,
                "weighted_units": 0.0,
                "last_solved_at": None,
                "tier_mix": [],
            }
        clauses = ["miner_hotkey=?", "verified=1"]
        params: list[Any] = [miner_hotkey]
        if epoch is not None:
            clauses.append("epoch=?")
            params.append(epoch)
        if since_iso is not None:
            clauses.append("solved_at_iso > ?")
            params.append(since_iso)
        where = " AND ".join(clauses)
        # per_miner_solves is keyed by (challenge_id, miner_hotkey), so COUNT(*)
        # is already the unique solve count for this miner/window. Avoid
        # COUNT(DISTINCT ...) here; this endpoint is dashboard visibility, not a
        # place to spend DB CPU under load.
        totals = store.query(
            "SELECT COUNT(*) AS unique_solves, "
            "COUNT(*) AS verified_solves, SUM(difficulty_weight) AS units, "
            "MAX(solved_at_iso) AS last_solved_at "
            "FROM per_miner_solves WHERE " + where,
            tuple(params),
        )
        tiers = store.query(
            "SELECT tier, COUNT(*) AS solves, "
            "SUM(difficulty_weight) AS units FROM per_miner_solves "
            "WHERE " + where + " GROUP BY tier ORDER BY tier",
            tuple(params),
        )
        t = totals[0] if totals else None
        unique = int(_row_get(t, "unique_solves", 0) or 0)
        verified = int(_row_get(t, "verified_solves", 0) or 0)
        return {
            "accepted_solves": verified,
            "verified_solves": verified,
            "unique_verified_solves": unique,
            "eligible_solves": unique,
            "weighted_units": round(float(_row_get(t, "units", 0.0) or 0.0), 6),
            "last_solved_at": _row_get(t, "last_solved_at"),
            "tier_mix": _tier_mix_from_rows(tiers),
        }

    def _pm_attempt_totals_for(
        miner_hotkey: str,
        *,
        epoch: int | None = None,
        since_iso: str | None = None,
    ) -> dict[str, Any]:
        if epoch is None and since_iso is None:
            return {
                "attempts": 0,
                "accepted_attempts": 0,
                "rejected_attempts": 0,
                "rejection_reasons": [],
            }
        # Exclude pm-* async SHADOW twins (default-off diagnostic) — see _reason_counts_for.
        clauses = ["miner_hotkey=?",
                   "(challenge_kind IS NULL OR challenge_kind != 'per_miner_shadow')"]
        params: list[Any] = [miner_hotkey]
        if epoch is not None:
            clauses.append("epoch=?")
            params.append(epoch)
        if since_iso is not None:
            clauses.append("recorded_at_iso > ?")
            params.append(since_iso)
        where = " AND ".join(clauses)
        row = store.query(
            "SELECT COUNT(*) AS attempts, "
            "SUM(CASE WHEN status='ranked' THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN status!='ranked' THEN 1 ELSE 0 END) AS rejected "
            "FROM per_miner_attempts WHERE " + where,
            tuple(params),
        )
        r = row[0] if row else None
        return {
            "attempts": int(_row_get(r, "attempts", 0) or 0),
            "accepted_attempts": int(_row_get(r, "accepted", 0) or 0),
            "rejected_attempts": int(_row_get(r, "rejected", 0) or 0),
            "rejection_reasons": _reason_counts_for(
                miner_hotkey=miner_hotkey,
                epoch=epoch,
                since_iso=since_iso,
            ),
        }

    def _pm_assignment_stats_for(assignment_identity: str, epoch: int | None) -> dict[str, Any]:
        if epoch is None:
            return {"assigned_challenges": 0, "tier_mix": []}
        rows_ = store.query(
            "SELECT tier, COUNT(*) AS solves, SUM(difficulty_weight) AS units "
            "FROM per_miner_assignments WHERE miner_hotkey=? AND epoch=? "
            "GROUP BY tier ORDER BY tier",
            (assignment_identity, epoch),
        )
        return {
            "assigned_challenges": sum(int(r["solves"] or 0) for r in rows_),
            "tier_mix": _tier_mix_from_rows(rows_, count_key="assigned"),
        }

    def _perminer_contribution_for(
        miner_hotkey: str,
        *,
        assignment_identity: str | None = None,
        eligible: bool = True,
        ineligibility_reason: str | None = None,
        expose_assignment_identity: bool = True,
        include_assignment_supply: bool = True,
        include_attempts: bool = True,
    ) -> dict[str, Any]:
        from . import per_miner as pm

        enabled = pm.perminer_enabled()
        epoch = pm.current_epoch() if enabled else None
        since_24h = _since_24h_iso()
        identity = assignment_identity if assignment_identity is not None else miner_hotkey
        current_totals = _pm_solve_stats_for(miner_hotkey, epoch=epoch, since_iso=None)
        last_24h_totals = _pm_solve_stats_for(miner_hotkey, epoch=None, since_iso=since_24h)
        if include_attempts:
            current_totals.update(_pm_attempt_totals_for(miner_hotkey, epoch=epoch))
            last_24h_totals.update(_pm_attempt_totals_for(miner_hotkey, since_iso=since_24h))

        payload = {
            "kind": "per_miner",
            "enabled": enabled,
            "shadow": pm.perminer_shadow() if enabled else False,
            "current_epoch": epoch,
            "miner_hotkey": miner_hotkey,
            "eligible": bool(eligible),
            "ineligibility_reason": ineligibility_reason,
            "scoring": {
                "mode": weights_mod.perminer_scoring_mode(),
                "bonus_multiplier": weights_mod.perminer_bonus_multiplier(),
                "history_floor": weights_mod.perminer_history_floor(),
                "coldkey_required": weights_mod.perminer_require_coldkey(),
            },
            "current_epoch_totals": current_totals,
            "last_24h_totals": last_24h_totals,
        }
        if expose_assignment_identity:
            payload["assignment_identity"] = identity
        if include_assignment_supply:
            payload["assignment_supply"] = _pm_assignment_stats_for(identity, epoch)
        return payload

    def _perminer_public_contribution(miner_hotkey: str) -> dict[str, Any]:
        identity = weights_mod.scoring_identity_for_hotkey(
            store,
            miner_hotkey,
            require_mapped=False,
        )
        return _perminer_contribution_for(
            miner_hotkey,
            assignment_identity=identity or miner_hotkey,
            eligible=True,
            ineligibility_reason=None,
            expose_assignment_identity=False,
            include_assignment_supply=False,
            include_attempts=False,
        )

    def _perminer_summary(limit: int) -> dict[str, Any]:
        from . import per_miner as pm

        enabled = pm.perminer_enabled()
        epoch = pm.current_epoch() if enabled else None
        since_24h = _since_24h_iso()
        # per_miner_solves is keyed by (challenge_id, miner_hotkey), so COUNT(*)
        # is equivalent to unique challenges per miner and is much cheaper than
        # COUNT(DISTINCT challenge_id) on the hot 24h PM summary path.
        rows_ = store.query(
            "SELECT miner_hotkey, unique_solves, verified_solves, units, "
            "last_solved_at, COUNT(*) OVER () AS active_miners "
            "FROM ("
            "  SELECT miner_hotkey, COUNT(*) AS unique_solves, "
            "  COUNT(*) AS verified_solves, SUM(difficulty_weight) AS units, "
            "  MAX(solved_at_iso) AS last_solved_at "
            "  FROM per_miner_solves WHERE solved_at_iso > ? AND verified=1 "
            "  GROUP BY miner_hotkey"
            ") AS pm_summary "
            "ORDER BY units DESC, unique_solves DESC, miner_hotkey LIMIT ?",
            (since_24h, limit),
        )
        assigned_miners = []
        assignment_accounting = (
            "listing" if _perminer_record_listing_assignments()
            else "cnf_fetch"
        )
        if epoch is not None and _perminer_record_listing_assignments():
            assigned_miners = store.query(
                "SELECT COUNT(*) AS n, SUM(assignments) AS assignments FROM ("
                "  SELECT miner_hotkey, COUNT(*) AS assignments "
                "  FROM per_miner_assignments WHERE epoch=? GROUP BY miner_hotkey"
                ") AS assignment_summary",
                (epoch,),
            )
        # PM-primary honesty: never imply PM is contributing when it isn't.
        # "verified scores" proxy = distinct miners with verified per-miner solves
        # in the trailing 24h; under pm_primary those solves are what feed the
        # vector, so 0 verified == not contributing (degraded), stated explicitly.
        _pm_mode = weights_mod.perminer_scoring_mode()
        _pm_verified_count = int(rows_[0]["active_miners"] or 0) if rows_ else 0
        _pm_primary_configured = _pm_mode == "pm_primary"
        _pm_primary_contributing = _pm_primary_configured and _pm_verified_count > 0
        return {
            "kind": "per_miner_summary",
            "enabled": enabled,
            "shadow": pm.perminer_shadow() if enabled else False,
            "current_epoch": epoch,
            "last_24h_since": since_24h,
            "pm_scoring_mode": _pm_mode,
            "pm_primary_configured": _pm_primary_configured,
            "pm_primary_contributing": _pm_primary_contributing,
            "pm_verified_scores_count": _pm_verified_count,
            "pm_primary_degraded_reason": (
                None if (not _pm_primary_configured or _pm_primary_contributing)
                else "no_verified_per_miner_scores"),
            "scoring": {
                "mode": weights_mod.perminer_scoring_mode(),
                "bonus_multiplier": weights_mod.perminer_bonus_multiplier(),
                "history_floor": weights_mod.perminer_history_floor(),
                "coldkey_required": weights_mod.perminer_require_coldkey(),
            },
            "assignment_accounting": assignment_accounting,
            "current_epoch_assignment_miners": int(assigned_miners[0]["n"] or 0) if assigned_miners else 0,
            "current_epoch_assigned_challenges": int(assigned_miners[0]["assignments"] or 0) if assigned_miners else 0,
            "active_miners_24h": _pm_verified_count,
            "miners": [
                {
                    "miner_hotkey": str(r["miner_hotkey"]),
                    "unique_verified_solves": int(r["unique_solves"] or 0),
                    "verified_solves": int(r["verified_solves"] or 0),
                    "eligible_solves": int(r["unique_solves"] or 0),
                    "weighted_units": round(float(r["units"] or 0.0), 6),
                    "last_solved_at": r["last_solved_at"],
                }
                for r in rows_
            ],
        }

    def _perminer_summary_warming(limit: int) -> dict[str, Any]:
        try:
            from . import per_miner as pm

            enabled = pm.perminer_enabled()
            shadow = pm.perminer_shadow() if enabled else False
            epoch = pm.current_epoch() if enabled else None
        except Exception:
            enabled = False
            shadow = False
            epoch = None
        return {
            "kind": "per_miner_summary",
            "enabled": enabled,
            "shadow": shadow,
            "current_epoch": epoch,
            "last_24h_since": _since_24h_iso(),
            "visibility_cache_status": "warming",
            "data_status": "warming",
            "metrics_status": "unavailable",
            "pm_scoring_mode": weights_mod.perminer_scoring_mode(),
            "pm_primary_configured": weights_mod.perminer_scoring_mode() == "pm_primary",
            "pm_primary_contributing": None,
            "pm_verified_scores_count": None,
            "pm_primary_degraded_reason": None,
            "scoring": {
                "mode": weights_mod.perminer_scoring_mode(),
                "bonus_multiplier": weights_mod.perminer_bonus_multiplier(),
                "history_floor": weights_mod.perminer_history_floor(),
                "coldkey_required": weights_mod.perminer_require_coldkey(),
            },
            "current_epoch_assignment_miners": None,
            "current_epoch_assigned_challenges": None,
            "active_miners_24h": None,
            "requested_limit": int(limit),
            "miners": [],
        }

    def _publisher_admin_token_configured() -> str:
        return os.environ.get("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", "").strip()

    def _require_publisher_admin(authorization: str | None) -> None:
        token = _publisher_admin_token_configured()
        if not token:
            raise HTTPException(503, "publisher_admin_token_not_configured")
        if not hmac.compare_digest(_bearer_value(authorization), token):
            raise HTTPException(401, "invalid_admin_token")

    def _require_v2_admin(authorization: str | None) -> None:
        token = os.environ.get("CATHEDRAL_V2_ADMIN_TOKEN", "").strip()
        if not token:
            raise HTTPException(503, "v2_admin_token_not_configured")
        if not hmac.compare_digest(_bearer_value(authorization), token):
            raise HTTPException(401, "invalid_v2_admin_token")

    @app.get("/v1/synthetic-boolean/per-miner/status")
    def per_miner_status(
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str | None = Header(None),
    ):
        """Authenticated PM status for the calling miner.

        This is the miner-facing visibility endpoint: it reports assigned
        supply, accepted solves, rejected attempts, reasons, tier mix, current
        epoch totals, and trailing 24h totals. It is read-only and does not mint
        assignments.
        """
        from . import per_miner as pm

        if not pm.perminer_enabled():
            raise HTTPException(404, "per_miner_not_enabled")
        _require_perminer_ready(pm)
        if x_cathedral_submitted_at is None:
            raise HTTPException(401, "missing X-Cathedral-Submitted-At")
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, x_cathedral_submitted_at,
            challenge_id="", dimacs_solution_sha256="",
        )
        assignment_identity = _assignment_identity_for_hotkey(x_cathedral_hotkey)
        return JSONResponse(
            _perminer_contribution_for(
                x_cathedral_hotkey,
                assignment_identity=assignment_identity,
                eligible=True,
            ),
            headers={"Cache-Control": "private, max-age=5", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v1/synthetic-boolean/per-miner/summary")
    async def per_miner_summary(limit: int = Query(50, ge=1, le=250)):
        """Public aggregate PM status for dashboards/operators.

        Contains only public hotkeys and aggregate counts; no signatures, CNFs,
        solutions, or secrets.
        """
        payload, cache_status = pm_summary_cache.get(
            int(limit),
            lambda: _perminer_summary(limit),
            cold_async=visibility_cold_async,
            cold_value=lambda: _perminer_summary_warming(limit),
        )
        # Honest state: if the background build keeps failing we serve the cold
        # placeholder/last-known-good as "degraded", never an eternal "warming".
        if cache_status == "degraded" and isinstance(payload, dict) \
                and payload.get("data_status") in (None, "warming"):
            payload = {**payload, "data_status": "degraded"}
        return JSONResponse(
            payload,
            headers={
                "Cache-Control": "public, max-age=10",
                "Access-Control-Allow-Origin": "*",
                "X-Cathedral-Cache": cache_status,
            },
        )

    # ---- TRACK 1: async submit-queue visibility ---------------------------
    # Drain-queue health for the durable admission lanes (public + pm-*). Surfaces
    # pending count, oldest-pending age, worker lag (now - oldest received_at), and
    # accepted/sec + rejected/sec over a short window so an operator can confirm the
    # drain worker is keeping up before/at cutover. Reads the ledger directly so it
    # works on any role; returns zeros when nothing is queued.
    def _async_queue_metrics(window_secs: float = 60.0) -> dict[str, Any]:
        now_iso = _now_iso_ms()
        try:
            q = submit_admission.queue_metrics(store, now_iso=now_iso)
            q["metrics_status"] = "ok"
        except Exception as exc:
            print(f"[submit_metrics] queue_metrics_failed error={exc!r}")
            q = {
                "metrics_status": "degraded",
                "metrics_error": "queue_metrics_failed",
                "total_pending": None,
                "by_status": {},
                "oldest_by_status": {},
                "oldest_received_at": None,
                "worker_lag_secs": None,
                "by_kind": {},
            }
        since_iso = _now_iso_ms_plus(-window_secs)
        # P2 fix: split the drain rates by LIVE vs SHADOW kind. A shadow drain
        # also stamps status + verified_at_iso (into shadow_* + a terminal status),
        # so counting all kinds together let an operator running shadow-ONLY mode
        # believe LIVE pm was draining when only the (default-off) shadow diagnostic
        # was. The headline accepted/rejected rates now count LIVE async kinds
        # (public + per_miner) only; shadow is reported separately so it stays
        # visible without inflating the live numbers.
        try:
            rate_snapshot = submit_admission.queue_rates(
                store, since_iso=since_iso, window_secs=window_secs)
            rates = store.query(
                "SELECT challenge_kind AS kind, status, COUNT(*) AS n "
                "FROM per_miner_attempts "
                "WHERE verified_at_iso IS NOT NULL AND verified_at_iso > ? "
                "AND challenge_kind IS NOT NULL "
                "GROUP BY challenge_kind, status", (since_iso,))
            rates_status = "ok"
        except Exception as exc:
            print(f"[submit_metrics] queue_rates_failed error={exc!r}")
            rate_snapshot = {
                "admitted_in_window": None,
                "terminal_in_window": None,
                "ranked_in_window": None,
                "rejected_terminal_in_window": None,
                "admitted_per_sec": None,
                "terminal_per_sec": None,
                "ranked_per_sec": None,
                "rejected_terminal_per_sec": None,
                "rates_by_kind": {},
            }
            rates = []
            rates_status = "degraded"
        accepted = rejected = 0
        shadow_accepted = shadow_rejected = 0
        for r in rates:
            st = str(r["status"])
            kind = str(r["kind"])
            n = int(r["n"] or 0)
            is_shadow = kind == submit_admission.KIND_PER_MINER_SHADOW
            if st == submit_admission.STATUS_RANKED:
                if is_shadow:
                    shadow_accepted += n
                else:
                    accepted += n
            elif st == submit_admission.STATUS_REJECTED:
                if is_shadow:
                    shadow_rejected += n
                else:
                    rejected += n
        win = max(1.0, float(window_secs))
        q["window_secs"] = win
        # LIVE async drain rates (public + per_miner) — shadow excluded.
        q["accepted_per_sec"] = round(accepted / win, 4)
        q["rejected_per_sec"] = round(rejected / win, 4)
        q["accepted_in_window"] = accepted
        q["rejected_in_window"] = rejected
        # Shadow drain reported separately so it never inflates the live rates.
        q["shadow_accepted_in_window"] = shadow_accepted
        q["shadow_rejected_in_window"] = shadow_rejected
        q["shadow_accepted_per_sec"] = round(shadow_accepted / win, 4)
        q["shadow_rejected_per_sec"] = round(shadow_rejected / win, 4)
        q.update(rate_snapshot)
        q["rates_status"] = rates_status
        try:
            q["workers"] = submit_admission.worker_metrics(
                store, now_iso=now_iso,
                stale_secs=max(10.0, float(_vw_flags.lock_secs())),
            )
        except Exception as exc:
            print(f"[submit_metrics] worker_metrics_failed error={exc!r}")
            q["workers"] = {
                "active_workers": None,
                "workers": [],
                "stale_after_secs": max(10.0, float(_vw_flags.lock_secs())),
                "metrics_status": "degraded",
            }
        q["backpressure"] = {
            "enabled": submit_queue_backpressure_enabled,
            "max_pending": (
                int(submit_queue_backpressure.get("max_pending") or 0)
                if submit_queue_backpressure else 0
            ),
            "max_worker_lag_secs": (
                float(submit_queue_backpressure.get("max_worker_lag_secs") or 0.0)
                if submit_queue_backpressure else 0.0
            ),
            "worker_stale_secs": (
                float(submit_queue_backpressure.get("worker_stale_secs") or 0.0)
                if submit_queue_backpressure else 0.0
            ),
            "retry_after_secs": submit_queue_backpressure_retry_after,
        }
        q["pm_async_enabled"] = pm_submit_async_enabled
        q["pm_async_shadow"] = pm_async_shadow_enabled
        q["public_async_enabled"] = submit_async_enabled
        return q

    def _dashboard_weight_freshness() -> dict[str, Any]:
        from . import health_thresholds as ht

        weight_key = os.environ.get(weights_mod.SIGNING_KEY_ENV, "").strip() or key_hex
        try:
            vec = weights_mod.cached_vector(store, signing_key_hex=weight_key)
        except Exception as exc:
            print(f"[dashboard_snapshot] cached_vector_failed error={exc!r}")
            vec = None
        generated_at = vec.get("generated_at") if isinstance(vec, dict) else None
        age = _age_seconds(generated_at)
        metadata = vec.get("policy_metadata", {}) if isinstance(vec, dict) else {}
        return {
            "data_status": "available" if isinstance(vec, dict) else "warming",
            "vector_present": isinstance(vec, dict),
            "vector_id": vec.get("vector_id") if isinstance(vec, dict) else None,
            "generated_at": generated_at,
            "expires_at": vec.get("expires_at") if isinstance(vec, dict) else None,
            "network": vec.get("network") if isinstance(vec, dict) else None,
            "netuid": vec.get("netuid") if isinstance(vec, dict) else None,
            "policy_reason": vec.get("policy_reason") if isinstance(vec, dict) else None,
            "policy_version": vec.get("policy_version") if isinstance(vec, dict) else None,
            "freshness": ht.vector_status(age),
            "source_block": (
                vec.get("source_block")
                or vec.get("block")
                or (metadata.get("source_block") if isinstance(metadata, dict) else None)
                or (metadata.get("block") if isinstance(metadata, dict) else None)
            ) if isinstance(vec, dict) else None,
        }

    def _dashboard_rejection_reasons() -> dict[str, Any]:
        submit_snapshot = _submit_metrics_snapshot()
        submit_reasons = [
            {"reason": str(reason), "count": int(count or 0)}
            for reason, count in sorted(
                submit_snapshot.get("by_reason", {}).items(),
                key=lambda item: (-int(item[1] or 0), str(item[0])),
            )
        ]
        return {
            "data_status": "available",
            "submit_reasons_since_start": submit_reasons,
            "pm_attempt_reasons": dashboard_snapshot_mod.rejection_reason_counts(store),
        }

    def _dashboard_state_payload() -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        leaderboard = dashboard_snapshot_mod.section(
            "earnings_leaderboard",
            lambda: _leaderboard_top_payload("weights"),
            {"data_status": "unavailable", "miners": []},
            errors,
        )
        pm_health = dashboard_snapshot_mod.section(
            "pm_health",
            lambda: _perminer_summary(_env_int("CATHEDRAL_DASHBOARD_PM_LIMIT", 100)),
            {"data_status": "unavailable", "miners": []},
            errors,
        )
        queue_lag = dashboard_snapshot_mod.section(
            "queue_lag",
            _async_queue_metrics,
            {"data_status": "unavailable", "total_pending": None, "worker_lag_secs": None},
            errors,
        )
        weights_freshness = dashboard_snapshot_mod.section(
            "weights_freshness",
            _dashboard_weight_freshness,
            {"data_status": "unavailable", "freshness": {"level": "unknown"}},
            errors,
        )
        endpoint_pressure = dashboard_snapshot_mod.section(
            "endpoint_pressure",
            lambda: {
                "http": _http_status_snapshot(),
                "submit": _submit_metrics_snapshot(),
                "pressure": pressure_telemetry.snapshot(),
            },
            {"data_status": "unavailable"},
            errors,
        )
        rejection_reasons = dashboard_snapshot_mod.section(
            "rejection_reasons",
            _dashboard_rejection_reasons,
            {"data_status": "unavailable", "items": []},
            errors,
        )
        return {
            "schema": dashboard_snapshot_mod.SCHEMA,
            "data_status": "partial" if errors else "ok",
            "source_epoch": (
                pm_health.get("current_epoch")
                if isinstance(pm_health, dict) else None
            ),
            "source_block": (
                weights_freshness.get("source_block")
                if isinstance(weights_freshness, dict) else None
            ),
            "earnings_leaderboard": leaderboard,
            "pm_health": pm_health,
            "queue_lag": queue_lag,
            "weights_freshness": weights_freshness,
            "endpoint_pressure": endpoint_pressure,
            "rejection_reasons": rejection_reasons,
            "sources": {
                "earnings_leaderboard": "/v1/leaderboard/top?view=weights",
                "pm_health": "/v1/synthetic-boolean/per-miner/summary",
                "queue_lag": "/v1/admin/synthetic-boolean/submit-metrics",
                "weights_freshness": "/v1/validator/weights/next",
                "endpoint_pressure": "in-process status/pressure telemetry",
                "rejection_reasons": "submit metrics + per_miner_attempts aggregate",
            },
            "errors": errors,
        }

    @app.get("/v1/dashboard/state")
    async def dashboard_state():
        if not dashboard_snapshot_mod.enabled():
            payload = dashboard_snapshot_mod.unavailable_payload(
                "disabled",
                reason="set CATHEDRAL_DASHBOARD_SNAPSHOT_ENABLED=true",
            )
            return JSONResponse(
                payload,
                status_code=503,
                headers=dashboard_snapshot_mod.response_headers(payload),
            )
        payload = dashboard_state_snapshot.get()
        if payload is None:
            payload = dashboard_snapshot_mod.unavailable_payload(
                "warming",
                reason="dashboard snapshot is cold or stale",
            )
            return JSONResponse(
                payload,
                status_code=503,
                headers=dashboard_snapshot_mod.response_headers(payload),
            )
        return JSONResponse(
            dashboard_snapshot_mod.public_payload(payload),
            headers=dashboard_snapshot_mod.response_headers(payload),
        )

    @app.get("/v1/admin/synthetic-boolean/submit-metrics")
    def submit_metrics_admin(authorization: str | None = Header(None)):
        """Operator-only submit pressure and rejection telemetry."""
        _require_publisher_admin(authorization)
        snapshot = _submit_metrics_snapshot()
        snapshot["queue"] = _async_queue_metrics()
        snapshot["pressure"] = pressure_telemetry.snapshot()
        return JSONResponse(
            snapshot,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v1/admin/validator-health")
    async def validator_health(authorization: str | None = Header(None)):
        """Read-only operator surface: weight-feed freshness + 5xx + submit.

        Single pane the validator release gate (and an operator dashboard) can
        poll. It does NOT build the signed vector — it peeks the warm cache via
        weights_mod.cached_vector(), so this probe can never add load to, or
        stall behind, the Tier 0 weight-feed build path. Returns whatever signal
        is available; a cold process (no cached vector) surfaces level=unknown
        rather than failing.
        """
        _require_publisher_admin(authorization)

        from . import health_thresholds as ht

        weight_key = os.environ.get(weights_mod.SIGNING_KEY_ENV, "").strip() or key_hex
        vec_generated_at: str | None = None
        vec_age: float | None = None
        vector_present = False
        try:
            vec = weights_mod.cached_vector(store, signing_key_hex=weight_key)
        except Exception as exc:  # never let the health probe raise
            vec = None
            print(f"[validator-health] cached_vector_failed error={exc!r}")
        if isinstance(vec, dict):
            vector_present = True
            vec_generated_at = vec.get("generated_at")
            vec_age = _age_seconds(vec_generated_at)

        http_snapshot = _http_status_snapshot()
        payload = {
            "schema": "cathedral.validator_health.v1",
            "checked_at": _now_iso_ms(),
            "service_role": service_role,
            "weights_feed": {
                "vector_present": vector_present,
                "generated_at": vec_generated_at,
                "freshness": ht.vector_status(vec_age),
                "feed_total_requests": http_snapshot["weights_feed_total"],
                "feed_5xx": http_snapshot["weights_feed_5xx"],
                "feed_rate_5xx": http_snapshot["weights_feed_rate_5xx"],
            },
            "http_status": http_snapshot,
            "submit": _submit_metrics_snapshot(),
            "pressure": pressure_telemetry.snapshot(),
            "tempo_seconds": ht.TEMPO_SECONDS,
        }
        return JSONResponse(
            payload,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v1/synthetic-boolean/per-miner/challenges")
    def per_miner_challenges(
        request: Request,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str | None = Header(None),
    ):
        """Return the per-miner instance set for the authenticated hotkey.

        FLAG: CATHEDRAL_PERMINER_ENABLED must be on; otherwise 404.
        Response is the miner's M unique challenge descriptors for this epoch.
        The CNF body is not included — fetch via /per-miner/cnf.

        This endpoint is the per-miner replacement for active-challenges.
        Existing miners can continue using active-challenges (unchanged).
        """
        from . import per_miner as pm
        if not pm.perminer_enabled():
            raise HTTPException(404, "per_miner_not_enabled")
        _require_perminer_ready(pm)
        if x_cathedral_submitted_at is None:
            raise HTTPException(401, "missing X-Cathedral-Submitted-At")
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, x_cathedral_submitted_at,
            challenge_id="", dimacs_solution_sha256="",
        )
        mark_verified_hotkey(request, x_cathedral_hotkey)
        epoch = _perminer_epoch_for(pm)
        assignment_identity = _assignment_identity_for_hotkey(x_cathedral_hotkey)
        effective_limit = pm.assignment_page_limit(limit)
        items = pm.miner_instance_set(
            assignment_identity, epoch, offset=offset, limit=effective_limit)
        if _perminer_record_listing_assignments():
            _record_perminer_assignments(
                assignment_identity,
                epoch,
                items,
                offset=offset,
                limit=effective_limit,
            )
        return {
            "family_id": _FAMILY,
            "kind": "per_miner",
            "epoch": epoch,
            "miner_hotkey": x_cathedral_hotkey,
            "assignment_identity": assignment_identity,
            "offset": offset,
            "requested_limit": limit,
            "limit": effective_limit,
            "max_limit": pm.assignment_page_limit_max(),
            "next_offset": offset + effective_limit,
            "count": len(items),
            "items": items,
            "submit_path": "/api/cathedral/v1/agents/submit",
            "cnf_path": "/v1/synthetic-boolean/per-miner/cnf",
            "cnf_params": ["challenge_id", "tier", "seq"],
            "assignment_persistence": (
                "listing" if _perminer_record_listing_assignments()
                else "cnf_fetch"
            ),
        }

    @app.get("/v1/synthetic-boolean/per-miner/cnf")
    def per_miner_cnf(
        request: Request,
        challenge_id: str = Query(...),
        tier: int | None = Query(None, ge=1),
        seq: int | None = Query(None, ge=0),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str | None = Header(None),
    ):
        """Return the CNF body for a specific per-miner instance.

        FLAG: CATHEDRAL_PERMINER_ENABLED must be on; otherwise 404.
        The challenge_id must be one of the calling miner's own instances
        for the current epoch — attempting to fetch another miner's instance
        with your hotkey returns 404 (the id won't be in your set).
        """
        from . import per_miner as pm
        if not pm.perminer_enabled():
            raise HTTPException(404, "per_miner_not_enabled")
        _require_perminer_ready(pm)
        if x_cathedral_submitted_at is None:
            raise HTTPException(401, "missing X-Cathedral-Submitted-At")
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, x_cathedral_submitted_at,
            challenge_id="", dimacs_solution_sha256="",
        )
        mark_verified_hotkey(request, x_cathedral_hotkey)
        epoch = _perminer_epoch_for(pm, challenge_id)
        assignment_identity = _assignment_identity_for_hotkey(x_cathedral_hotkey)
        tier_seq = _resolve_perminer_tier_seq(
            pm, assignment_identity, epoch, challenge_id, tier, seq)
        if tier_seq is not None:
            tier, seq = tier_seq
            cid, cnf_text, _ = pm.generate_instance(assignment_identity, epoch, tier, seq)
            if cid == challenge_id:
                _record_one_perminer_assignment(
                    assignment_identity, epoch, challenge_id, tier, seq)
                return PlainTextResponse(cnf_text, media_type="text/plain; charset=utf-8",
                                        headers={"X-Perminer-Challenge-Id": cid,
                                                 "X-Perminer-Tier": str(tier),
                                                 "X-Perminer-Seq": str(seq),
                                                 "X-Perminer-Epoch": str(epoch)})
        raise HTTPException(404, "assignment_required_fetch_challenges_first")

    # ---- TRACK 1: pm-* async SHADOW admission helper ----------------------
    # Persist a SHADOW pending row (challenge_kind=per_miner_shadow) carrying the
    # inline verify verdict in `rejection_reason` ("__ranked__" for an inline accept,
    # else the inline reject reason). The async worker re-verifies it independently
    # and writes the result to shadow_* columns only — never to the live payout
    # ledger — so go-live can prove async-vs-inline parity before cutover. Idempotent
    # on idempotency_key so replays do not create a second shadow row.
    def _admit_pm_shadow(
        *, challenge_id, miner_hotkey, signature, submitted_at, received_at_iso,
        sol_sha, dimacs_solution, epoch, assignment_identity, inline_marker,
    ):
        # P1 fix: the shadow twin MUST use a namespaced idempotency key so it can
        # never collide with the LIVE pm-async key for the same payload. Without
        # this, a miner retrying the same solution after cutover (shadow off, live
        # on) would have admit_pending() match the stale shadow row and replay its
        # receipt instead of creating the live authoritative pm receipt.
        idem = submit_admission.shadow_idempotency_key(
            miner_hotkey, challenge_id, sol_sha)
        receipt_id = "shd_" + new_uuid().replace("-", "")
        now_iso = _now_iso_ms()

        def _do(conn):
            existing = conn.execute(
                "SELECT id FROM per_miner_attempts WHERE idempotency_key=? LIMIT 1",
                (idem,)).fetchone()
            if existing is not None:
                return  # idempotent: shadow twin already queued
            conn.execute(
                "INSERT OR IGNORE INTO per_miner_attempts("
                "id, challenge_id, miner_hotkey, epoch, status, rejection_reason, "
                "dimacs_solution_sha256, submitted_at, recorded_at_iso, signature, "
                "idempotency_key, received_at_iso, challenge_kind, solution_body, "
                "assignment_identity, attempt_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (receipt_id, challenge_id, miner_hotkey, epoch,
                 submit_admission.STATUS_PENDING, inline_marker, sol_sha,
                 submitted_at, now_iso, signature, idem, received_at_iso,
                 submit_admission.KIND_PER_MINER_SHADOW, dimacs_solution,
                 assignment_identity))
        try:
            store.write(_do)
        except Exception as exc:  # shadow must never break the authoritative inline path
            print(f"[verify] pm_shadow_admit_failed error={exc!r}")

    # ---- M2: Lane A submit (solve-on-submit) ------------------------------
    @app.post("/v1/agents/submit")
    def agents_submit(
        request: Request,
        card_id: str = Form(...),
        display_name: str = Form(""),
        submitted_at: str = Form(None),
        challenge_id: str = Form(None),
        dimacs_solution: str = Form(None),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(default="", alias="X-Cathedral-Submitted-At"),
        x_cathedral_submit_mode: str = Header(default="", alias="X-Cathedral-Submit-Mode"),
        _slot: None = Depends(_submit_slot),
    ):
        # Fairness clock: server time at handler entry, BEFORE any verification.
        received_at_iso = _now_iso_ms()
        if card_id != _FAMILY:
            raise HTTPException(400, f"only card_id={_FAMILY} accepted (see skill.md)")
        if not dimacs_solution or not challenge_id:
            raise HTTPException(400, "this publisher requires solve-on-submit "
                                     "(challenge_id + dimacs_solution); see skill.md")
        want_sync_submit = x_cathedral_submit_mode.strip().lower() == "sync"
        from . import per_miner as pm
        is_pm_challenge = challenge_id.startswith("pm-") and pm.perminer_enabled()
        async_admission_would_queue = (
            (not is_pm_challenge and submit_async_enabled)
            or (is_pm_challenge and pm_submit_async_enabled and not pm_async_shadow_enabled)
        )
        if async_admission_would_queue and not want_sync_submit:
            limit = (
                pm_submit_max_solution_bytes if is_pm_challenge
                else submit_max_solution_bytes
            )
            if limit > 0 and len(dimacs_solution.encode("utf-8")) > limit:
                _record_submit_event(
                    "rejected", "solution_too_large",
                    challenge_id=challenge_id, status_code=413, log=True)
                raise HTTPException(
                    413, "solution_too_large",
                    headers={"X-Cathedral-Rejection-Reason": "solution_too_large"})
        # The miner may have signed the timestamp it sent in the form field or
        # the X-Cathedral-Submitted-At header — fall back across both.
        submitted_at = submitted_at or x_cathedral_submitted_at or _now_iso_ms()

        # per-(hotkey, challenge) rate limit (fires before lock check).
        rl_key = (x_cathedral_hotkey, challenge_id)
        now = time.time()
        if _submit_rate_limited(rl_key, now):
            _record_submit_event(
                "rate_limited",
                "rate_limited",
                challenge_id=challenge_id,
                status_code=429,
                log=True,
            )
            raise HTTPException(
                429,
                "rate_limited",
                headers={
                    "Retry-After": str(max(1, min_interval)),
                    "X-Cathedral-Rejection-Reason": "rate_limited",
                },
            )

        sol_sha = sha256_hex(dimacs_solution)
        submitted_at = _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, submitted_at,
            challenge_id=challenge_id, dimacs_solution_sha256=sol_sha,
            alt_submitted_at=x_cathedral_submitted_at or None,
            allow_fallback_shapes=False,
        )
        mark_verified_hotkey(request, x_cathedral_hotkey)

        # ---- Per-miner submit path (CATHEDRAL_PERMINER_ENABLED) ----
        # Detected by challenge_id prefix "pm-". Flag-off: this block is never
        # entered even if a miner sends a pm- id (it won't exist in lane_challenges
        # and will 409 — a safe hard stop, not silent misbehaviour).
        from . import per_miner as pm
        if challenge_id.startswith("pm-") and pm.perminer_enabled():
            _require_perminer_ready(pm)
            _remember_submit(rl_key, now)

            epoch = _perminer_epoch_for(pm, challenge_id)
            assignment_identity = _assignment_identity_for_hotkey(x_cathedral_hotkey)
            tier_seq = _resolve_perminer_tier_seq(
                pm, assignment_identity, epoch, challenge_id, None, None)

            # ---- TRACK 1: pm-* durable async admission (default-off) ----
            # CHEAP inline checks already ran above: signature (_verify_hotkey_claim),
            # body-size, and challenge ownership/recovery (tier_seq). The HEAVY work
            # (CNF re-materialization + DIMACS verify + anti-copy witness check) moves
            # to the async worker. We persist a pending receipt keyed by idempotency
            # and return 202 immediately; the worker re-derives the miner's own CNF
            # from assignment_identity and records the terminal accept/reject into the
            # SAME ledger scoring reads. Replays of the same solution return the SAME
            # receipt (no second attempt / no double payout). The signature is NOT
            # burned at admission — the worker's atomic accept/reject burns it, keeping
            # exactly-once replay semantics identical to the inline path.
            want_sync_pm = want_sync_submit
            if (pm_submit_async_enabled and not pm_async_shadow_enabled
                    and not want_sync_pm and tier_seq is not None):
                # Body-size limit (cheap inline check). A pathological body is a hard
                # 413 here — never persisted, never queued.
                if len(dimacs_solution) > pm_submit_max_solution_bytes:
                    _record_submit_event(
                        "rejected", "solution_too_large",
                        challenge_id=challenge_id, status_code=413, log=True)
                    raise HTTPException(
                        413, "solution_too_large",
                        headers={"X-Cathedral-Rejection-Reason": "solution_too_large"})
                worker_ready, _worker_metrics = _async_worker_ready()
                if not worker_ready:
                    # PM challenges are small and deterministic. If async workers
                    # are down, keep miners earning by using the legacy sync verifier
                    # instead of failing the private lane closed.
                    _record_submit_event(
                        "fallback",
                        "pm_async_worker_unavailable_sync",
                        challenge_id=challenge_id,
                        log=True,
                    )
                    want_sync_pm = True
                if not want_sync_pm:
                    # Re-materialize the assignment ledger row (cheap; cid->tier/seq only)
                    # so the worker can recover tier/seq even if the read replica is behind.
                    _record_one_perminer_assignment(
                        assignment_identity, epoch, challenge_id, tier_seq[0], tier_seq[1])
                    idem = submit_admission.idempotency_key(
                        x_cathedral_hotkey, challenge_id, sol_sha)
                    receipt_id = "sub_" + new_uuid().replace("-", "")
                    outcome, row = submit_admission.admit_pending(
                        store,
                        receipt_id=receipt_id,
                        idem_key=idem,
                        miner_hotkey=x_cathedral_hotkey,
                        challenge_id=challenge_id,
                        dimacs_solution_sha256=sol_sha,
                        dimacs_solution=dimacs_solution,
                        submitted_at=submitted_at,
                        received_at_iso=received_at_iso,
                        signature=x_cathedral_signature,
                        epoch=epoch,
                        assignment_identity=assignment_identity,
                        queue_backpressure=submit_queue_backpressure,
                    )
                    if outcome == "backpressure":
                        _raise_submit_queue_backpressure(challenge_id, row)
                    receipt = submit_admission.receipt_from_row(row)
                    if outcome == "replayed":
                        _record_submit_event(
                            "accepted", "idempotent_replay",
                            challenge_id=challenge_id, status_code=200)
                        return JSONResponse(status_code=200, content=receipt)
                    _record_submit_event(
                        "accepted", "admitted_pending",
                        challenge_id=challenge_id, status_code=202)
                    return JSONResponse(status_code=202, content=receipt)
            # When ownership/recovery failed (tier_seq is None) async mode falls
            # through to the inline reject below — it is a cheap check, no heavy work
            # runs, and the error contract stays byte-for-byte the synchronous one.
            # ---- End pm-* durable async admission; inline path continues below ----

            if tier_seq is None:
                check = None
                ok, reason = False, "assignment_required_fetch_challenges_first"
            else:
                # The assignment row is a ledger/cache entry, not the authority;
                # deterministic recovery above is the ownership check.
                _record_one_perminer_assignment(
                    assignment_identity, epoch, challenge_id, tier_seq[0], tier_seq[1])
                cnf = pm.get_miner_cnf(assignment_identity, epoch, tier_seq[0], tier_seq[1])
                if cnf is None:
                    check = None
                    ok, reason = False, "challenge_id_not_in_miner_set"
                else:
                    _cid, cnf_text = cnf
                    check = verify_dimacs_solution(cnf_text, dimacs_solution)
                    if not check.ok:
                        ok, reason = False, check.rejection_reason
                    else:
                        ok, reason = pm.verify_miner_submission_for(
                            assignment_identity, epoch, tier_seq[0], tier_seq[1],
                            challenge_id, check.assignment)

            # ---- TRACK 1: pm-* async SHADOW (default-off) ----
            # The inline result above stays authoritative for payout. When shadow is
            # on we ALSO persist a pending shadow row stamped with the inline verify
            # verdict; the worker independently re-verifies it into the shadow_*
            # columns and logs any async-vs-inline divergence. NO payout change: the
            # shadow row never touches per_miner_solves / agent_submissions / eval_runs.
            if pm_async_shadow_enabled and tier_seq is not None:
                if len(dimacs_solution) <= pm_submit_max_solution_bytes:
                    inline_marker = "__ranked__" if ok else (reason or "rejected")
                    _admit_pm_shadow(
                        challenge_id=challenge_id,
                        miner_hotkey=x_cathedral_hotkey,
                        signature=x_cathedral_signature,
                        submitted_at=submitted_at,
                        received_at_iso=received_at_iso,
                        sol_sha=sol_sha,
                        dimacs_solution=dimacs_solution,
                        epoch=epoch,
                        assignment_identity=assignment_identity,
                        inline_marker=inline_marker,
                    )

            sub_id = new_uuid()

            def _record_pm_attempt(reason: str) -> None:
                recorded_at = _now_iso_ms()

                def _attempt(conn):
                    conn.execute(
                        "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, "
                        "epoch, status, rejection_reason, dimacs_solution_sha256, "
                        "submitted_at, recorded_at_iso, signature) "
                        "VALUES (?, ?, ?, ?, 'rejected', ?, ?, ?, ?, ?)",
                        (sub_id, challenge_id, x_cathedral_hotkey, epoch, reason,
                         sol_sha, submitted_at, recorded_at, x_cathedral_signature))
                store.write(_attempt)

            if not ok:
                def _pm_rej(conn):
                    recorded_at = _now_iso_ms()
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO submit_signatures(signature, seen_at) VALUES (?, ?)",
                        (x_cathedral_signature, recorded_at))
                    if not cur.rowcount:
                        return False
                    conn.execute(
                        "INSERT INTO agent_submissions(id, miner_hotkey, sat_challenge_id, "
                        "status, rejection_reason, current_score, seq_no, submitted_at, signature) "
                        "VALUES (?, ?, ?, 'rejected', ?, 0.0, 1, ?, ?)",
                        (sub_id, x_cathedral_hotkey, challenge_id, reason,
                         submitted_at, x_cathedral_signature))
                    conn.execute(
                        "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, "
                        "epoch, status, rejection_reason, dimacs_solution_sha256, "
                        "submitted_at, recorded_at_iso, signature) "
                        "VALUES (?, ?, ?, ?, 'rejected', ?, ?, ?, ?, ?)",
                        (sub_id, challenge_id, x_cathedral_hotkey, epoch, reason,
                         sol_sha, submitted_at, recorded_at, x_cathedral_signature))
                    return True
                if not store.write(_pm_rej):
                    _record_submit_event(
                        "rejected",
                        "replayed_signature",
                        challenge_id=challenge_id,
                        status_code=409,
                        log=True,
                    )
                    raise HTTPException(
                        409,
                        "replayed_signature",
                        headers={"X-Cathedral-Rejection-Reason": "replayed_signature"},
                    )
                _record_submit_event(
                    "rejected",
                    reason,
                    challenge_id=challenge_id,
                    status_code=400,
                    log=True,
                )
                raise HTTPException(
                    400,
                    {"detail": reason, "challenge_id": challenge_id},
                    headers={"X-Cathedral-Rejection-Reason": reason},
                )

            # Accepted: tier/seq came from the assignment ledger or compatibility scan.
            tier = tier_seq[0] if tier_seq else 1
            seq = tier_seq[1] if tier_seq else 0
            pm_weight = pm.weight_for(tier)
            now_iso = _now_iso_ms()
            row_uuid = new_uuid()
            answer_hash = sha256_hex(",".join(str(x) for x in (check.assignment if check else [])))
            verifier_details_hash = sha256_hex(f"{challenge_id}:{sol_sha}")

            def _pm_accept(conn):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO submit_signatures(signature, seen_at) VALUES (?, ?)",
                    (x_cathedral_signature, now_iso))
                if not cur.rowcount:
                    return "replayed_signature"
                solved = conn.execute(
                    "INSERT OR IGNORE INTO per_miner_solves"
                    "(challenge_id, miner_hotkey, epoch, tier, seq, difficulty_weight, "
                    "verified, solved_at_iso) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (challenge_id, x_cathedral_hotkey, epoch, tier, seq, pm_weight, 1, now_iso))
                if not solved.rowcount:
                    return "already_solved"
                conn.execute(
                    "INSERT INTO agent_submissions(id, miner_hotkey, sat_challenge_id, "
                    "status, rejection_reason, current_score, seq_no, submitted_at, signature) "
                    "VALUES (?, ?, ?, 'ranked', NULL, ?, 1, ?, ?)",
                    (sub_id, x_cathedral_hotkey, challenge_id, pm_weight, submitted_at,
                     x_cathedral_signature))
                conn.execute(
                    "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, "
                    "epoch, status, rejection_reason, dimacs_solution_sha256, "
                    "submitted_at, recorded_at_iso, signature) "
                    "VALUES (?, ?, ?, ?, 'ranked', NULL, ?, ?, ?, ?)",
                    (sub_id, challenge_id, x_cathedral_hotkey, epoch, sol_sha,
                     submitted_at, now_iso, x_cathedral_signature))
                conn.execute(
                    "INSERT INTO per_miner_witnesses(challenge_id, miner_hotkey, epoch, "
                    "tier, seq, dimacs_solution_sha256, answer_hash, dimacs_solution, "
                    "recorded_at_iso) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (challenge_id, x_cathedral_hotkey, epoch, tier, seq, sol_sha,
                     answer_hash, dimacs_solution, now_iso))
                emitted = rows.build_solve_rows(
                    row_uuid=row_uuid, miner_hotkey=x_cathedral_hotkey,
                    agent_id=new_uuid(), challenge_id=challenge_id, tier=tier,
                    weighted_score=pm_weight, answer_hash=answer_hash,
                    verifier_details_hash=verifier_details_hash, ran_at=now_iso,
                    epoch_salt=epoch_salt, solve_rank=1, solved=True,
                    private_key_hex=key_hex,
                )
                for r in emitted:
                    conn.execute(
                        "INSERT OR IGNORE INTO eval_runs "
                        "(id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (r["id"], r["ran_at"], int(r["eval_output_schema_version"]),
                         r["miner_hotkey"], r["task_type"], json.dumps(r)))
                return None

            err = store.write(_pm_accept)
            if err == "replayed_signature":
                _record_submit_event(
                    "rejected",
                    "replayed_signature",
                    challenge_id=challenge_id,
                    status_code=409,
                    log=True,
                )
                raise HTTPException(
                    409,
                    "replayed_signature",
                    headers={"X-Cathedral-Rejection-Reason": "replayed_signature"},
                )
            if err == "already_solved":
                _record_pm_attempt("already_solved")
                _record_submit_event(
                    "rejected",
                    "already_solved",
                    challenge_id=challenge_id,
                    status_code=409,
                    log=True,
                )
                raise HTTPException(
                    409,
                    "already_solved",
                    headers={"X-Cathedral-Rejection-Reason": "already_solved"},
                )
            _record_submit_event("accepted", "ranked", challenge_id=challenge_id, status_code=200)
            return {
                "status": "ranked", "id": sub_id, "eval_run_id": row_uuid,
                "challenge_id": challenge_id,
                "weighted_score": pm_weight,
                "solve_rank": 1, "attestation_status": "pending",
            }
        # ---- End per-miner submit path ----

        rows_ = store.query(
            "SELECT * FROM lane_challenges WHERE challenge_id=?", (challenge_id,))
        if not rows_:
            _record_submit_event(
                "rejected",
                "challenge_not_active",
                challenge_id=challenge_id,
                status_code=409,
                log=True,
            )
            raise HTTPException(
                409,
                "challenge_not_active",
                headers={"X-Cathedral-Rejection-Reason": "challenge_not_active"},
            )
        chal = rows_[0]
        if chal["status"] != "active":
            _record_submit_event(
                "rejected",
                "challenge_already_locked",
                challenge_id=challenge_id,
                status_code=409,
                log=True,
            )
            raise HTTPException(
                409,
                "challenge_already_locked",
                headers={"X-Cathedral-Rejection-Reason": "challenge_already_locked"},
            )

        _remember_submit(rl_key, now)  # consume the slot only past the gates

        # ---- Phase 4: durable admission (public lane, default-off) ----
        # When CATHEDRAL_SUBMIT_ASYNC_ENABLED is on AND the client did not force
        # the legacy path (X-Cathedral-Submit-Mode: sync), do only cheap work here:
        # persist a pending receipt keyed by idempotency and return 202. The async
        # verify_worker loads the CNF, runs verify_dimacs_solution, and records the
        # ranked/rejected result + signed feed rows in received_at order. Replays of
        # the same solution return the SAME receipt (no second attempt / no double
        # payout). The signature is NOT burned at admission — burn happens in the
        # worker's atomic accept/reject, preserving exactly-once replay semantics.
        want_sync = want_sync_submit
        if submit_async_enabled and not want_sync:
            _require_async_worker_ready(challenge_id)
            idem = submit_admission.idempotency_key(
                x_cathedral_hotkey, challenge_id, sol_sha)
            receipt_id = "sub_" + new_uuid().replace("-", "")
            outcome, row = submit_admission.admit_pending(
                store,
                receipt_id=receipt_id,
                idem_key=idem,
                miner_hotkey=x_cathedral_hotkey,
                challenge_id=challenge_id,
                dimacs_solution_sha256=sol_sha,
                dimacs_solution=dimacs_solution,
                submitted_at=submitted_at,
                received_at_iso=received_at_iso,
                signature=x_cathedral_signature,
                epoch=0,
                queue_backpressure=submit_queue_backpressure,
            )
            if outcome == "backpressure":
                _raise_submit_queue_backpressure(challenge_id, row)
            receipt = submit_admission.receipt_from_row(row)
            if outcome == "replayed":
                # Idempotent replay: echo the existing receipt with its current
                # status. 200 (not 202) signals "already known" to the client.
                _record_submit_event(
                    "accepted", "idempotent_replay",
                    challenge_id=challenge_id, status_code=200)
                return JSONResponse(status_code=200, content=receipt)
            _record_submit_event(
                "accepted", "admitted_pending",
                challenge_id=challenge_id, status_code=202)
            return JSONResponse(status_code=202, content=receipt)
        # ---- End durable admission; fall through to legacy synchronous path ----

        check = verify_dimacs_solution(chal["cnf_text"], dimacs_solution)
        sub_id = new_uuid()
        if not check.ok:
            # one txn: burn the signature + record the rejection together.
            def _rej(conn):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO submit_signatures(signature, seen_at) VALUES (?, ?)",
                    (x_cathedral_signature, _now_iso_ms()))
                if not cur.rowcount:
                    return False
                conn.execute(
                    "INSERT INTO agent_submissions(id, miner_hotkey, sat_challenge_id, "
                    "status, rejection_reason, current_score, seq_no, submitted_at, signature) "
                    "VALUES (?, ?, ?, 'rejected', ?, 0.0, 1, ?, ?)",
                    (sub_id, x_cathedral_hotkey, challenge_id, check.rejection_reason,
                     submitted_at, x_cathedral_signature))
                return True
            if not store.write(_rej):
                _record_submit_event(
                    "rejected",
                    "replayed_signature",
                    challenge_id=challenge_id,
                    status_code=409,
                    log=True,
                )
                raise HTTPException(
                    409,
                    "replayed_signature",
                    headers={"X-Cathedral-Rejection-Reason": "replayed_signature"},
                )
            _record_submit_event(
                "rejected",
                check.rejection_reason,
                challenge_id=challenge_id,
                status_code=400,
                log=True,
            )
            raise HTTPException(
                400,
                {"detail": check.rejection_reason, "challenge_id": challenge_id},
                headers={"X-Cathedral-Rejection-Reason": check.rejection_reason},
            )

        # accept the solve. Default = open-window (live since 2026-06-04): a
        # challenge takes one solve per distinct hotkey while active, each with
        # its true first-seen rank; saturation/age retirement is the refill
        # loop's job. lock_wins preserves the legacy winner-take-all.
        #
        # ATOMICITY: dedup + claim + scoring + submission + signed feed rows all
        # commit in ONE transaction. A crash anywhere rolls back everything —
        # no burned-signature-without-rows lockout, no claimed-but-unrewarded
        # solve. (Store's RLock is reentrant, so scoring may read via the store
        # from inside this txn and sees the just-inserted claim.)
        now_iso = _now_iso_ms()
        row_uuid = new_uuid()
        answer_hash = sha256_hex(",".join(str(x) for x in check.assignment))
        verifier_details_hash = sha256_hex(f"{challenge_id}:{sol_sha}")
        lock_wins = scoring.submit_mode() == "lock_wins"

        def _accept(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO submit_signatures(signature, seen_at) VALUES (?, ?)",
                (x_cathedral_signature, now_iso))
            if not cur.rowcount:
                return ("replayed_signature", None, None)
            if lock_wins:
                locked = conn.execute(
                    "UPDATE lane_challenges SET status='locked' "
                    "WHERE challenge_id=? AND status='active'", (challenge_id,))
                if locked.rowcount != 1:
                    return ("challenge_already_locked", None, None)
                rank = scoring.claim_solve(conn, challenge_id, x_cathedral_hotkey, now_iso) or 1
            else:
                # Re-check active INSIDE the transaction: the pre-tx status read
                # goes stale if the refill loop retires the challenge between read
                # and write, which would otherwise pay a solve on a dead challenge.
                # The UPDATE also row-locks lane_challenges on Postgres, serializing
                # concurrent distinct-hotkey solves so their first-seen ranks stay
                # unique (SQLite already serializes writes under BEGIN IMMEDIATE).
                active = conn.execute(
                    "UPDATE lane_challenges SET updated_at_iso=? "
                    "WHERE challenge_id=? AND status='active'", (now_iso, challenge_id))
                if active.rowcount != 1:
                    return ("challenge_not_active", None, None)
                rank = scoring.claim_solve(conn, challenge_id, x_cathedral_hotkey, now_iso)
                if rank is None:
                    return ("already_solved", None, None)
            # row value = flat 1.0 (the audit trail). Economics live in the
            # signed vector (weights.py), composed from this solve ledger.
            score_multiplier = float(chal["score_multiplier"])
            ws = (
                scoring.weighted_score_for(store, x_cathedral_hotkey)
                * score_multiplier
                * _public_row_score_multiplier()
            )
            conn.execute(
                "INSERT INTO agent_submissions(id, miner_hotkey, sat_challenge_id, "
                "status, rejection_reason, current_score, seq_no, submitted_at, signature) "
                "VALUES (?, ?, ?, 'ranked', NULL, ?, ?, ?, ?)",
                (sub_id, x_cathedral_hotkey, challenge_id, ws, rank,
                 submitted_at, x_cathedral_signature))
            emitted = rows.build_solve_rows(
                row_uuid=row_uuid, miner_hotkey=x_cathedral_hotkey,
                agent_id=new_uuid(), challenge_id=challenge_id, tier=chal["tier"],
                weighted_score=ws, answer_hash=answer_hash,
                verifier_details_hash=verifier_details_hash, ran_at=now_iso,
                epoch_salt=epoch_salt, solve_rank=rank, solved=True,
                private_key_hex=key_hex,
            )
            for r in emitted:
                conn.execute(
                    "INSERT OR IGNORE INTO eval_runs "
                    "(id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (r["id"], r["ran_at"], int(r["eval_output_schema_version"]),
                     r["miner_hotkey"], r["task_type"], json.dumps(r)))
            return (None, rank, ws)

        err, rank, ws = store.write(_accept)
        if err is None and lock_wins:
            # the challenge just flipped active -> locked: refresh the board.
            board_cache_mod.invalidate_all()
        if err == "replayed_signature":
            _record_submit_event(
                "rejected",
                "replayed_signature",
                challenge_id=challenge_id,
                status_code=409,
                log=True,
            )
            raise HTTPException(
                409,
                "replayed_signature",
                headers={"X-Cathedral-Rejection-Reason": "replayed_signature"},
            )
        if err == "challenge_already_locked":
            _record_submit_event(
                "rejected",
                "challenge_already_locked",
                challenge_id=challenge_id,
                status_code=409,
                log=True,
            )
            raise HTTPException(
                409,
                "challenge_already_locked",
                headers={"X-Cathedral-Rejection-Reason": "challenge_already_locked"},
            )
        if err == "challenge_not_active":
            _record_submit_event(
                "rejected",
                "challenge_not_active",
                challenge_id=challenge_id,
                status_code=409,
                log=True,
            )
            raise HTTPException(
                409,
                "challenge_not_active",
                headers={"X-Cathedral-Rejection-Reason": "challenge_not_active"},
            )
        if err == "already_solved":
            _record_submit_event(
                "rejected",
                "already_solved",
                challenge_id=challenge_id,
                status_code=409,
                log=True,
            )
            raise HTTPException(
                409,
                "already_solved",
                headers={"X-Cathedral-Rejection-Reason": "already_solved"},
            )
        _record_submit_event("accepted", "ranked", challenge_id=challenge_id, status_code=200)
        return {
            "status": "ranked", "id": sub_id, "eval_run_id": row_uuid,
            "challenge_id": challenge_id, "weighted_score": ws,
            "solve_rank": rank, "attestation_status": "pending",
        }

    # ---- V2 off-chain solution manifest intake (Phase 1/2+) ---------------
    def _require_v2_submit_token_mint_allowed(hotkey: str) -> None:
        if v2_submit_token_allowlist and str(hotkey).strip() not in v2_submit_token_allowlist:
            raise HTTPException(403, "v2_submit_token_hotkey_not_allowlisted")

    def _authorize_v2_cnf_access(
        request: Request,
        *,
        challenge_id: str,
        tier: int | None,
        seq: int | None,
        hotkey: str,
        signature: str,
        submitted_at: str | None,
        require_submit_token: bool,
    ) -> dict[str, Any]:
        """The single auth/ownership/grace contract for both V2 CNF paths."""
        if not solution_manifest_enabled:
            raise HTTPException(404, "solution_manifest_v2_not_enabled")
        from . import per_miner as pm
        if not v2_pipeline.v2_perminer_enabled():
            raise HTTPException(404, "v2_per_miner_not_enabled")
        _require_v2_perminer_ready(pm)
        if submitted_at is None:
            raise HTTPException(401, "missing X-Cathedral-Submitted-At")
        _verify_hotkey_claim(
            hotkey,
            signature,
            submitted_at,
            challenge_id="",
            dimacs_solution_sha256="",
        )
        mark_verified_hotkey(request, hotkey)
        parsed = pm.parse_challenge_id(challenge_id)
        current_epoch = pm.current_epoch()
        epoch = int(parsed["epoch"]) if parsed else current_epoch
        if epoch not in (current_epoch, current_epoch - 1):
            raise HTTPException(410, "per_miner_challenge_expired")
        assignment_identity = _assignment_identity_for_hotkey(hotkey)
        tier_seq = pm.resolve_tier_seq_for(
            assignment_identity,
            epoch,
            challenge_id,
            tier=tier,
            seq=seq,
        )
        if tier_seq is None:
            raise HTTPException(404, "challenge_id_not_in_miner_set")
        tier_i, seq_i = tier_seq
        if require_submit_token:
            if not v2_submit_bitset_enabled:
                raise HTTPException(404, "v2_submit_bitset_not_enabled")
            _require_v2_submit_token_mint_allowed(hotkey)
            if not v2_submit_token_secret:
                raise HTTPException(503, "v2_submit_token_secret_missing")
        return {
            "pm": pm,
            "challenge_id": challenge_id,
            "hotkey": hotkey,
            "assignment_identity": assignment_identity,
            "epoch": epoch,
            "tier": int(tier_i),
            "seq": int(seq_i),
        }

    def _mint_v2_cnf_submit_token(
        context: dict[str, Any],
        *,
        n_vars: int,
        cnf_sha256: str,
    ) -> tuple[str, str]:
        expires_at = _now_iso_ms_plus(v2_submit_token_ttl_secs)
        token = v2_bitset_submit.mint_submit_token(
            secret=v2_submit_token_secret,
            miner_hotkey=str(context["hotkey"]),
            challenge_id=str(context["challenge_id"]),
            epoch=int(context["epoch"]),
            tier=int(context["tier"]),
            seq=int(context["seq"]),
            nvars=int(n_vars),
            cnf_sha256=str(cnf_sha256),
            expires_at=expires_at,
        )
        return token, expires_at

    def _artifact_for_v2_cnf_context(
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        artifact = v2_cnf_artifacts.get_artifact(
            v2_store, str(context["challenge_id"]))
        if artifact is None:
            return None
        if any(
            int(artifact[field]) != int(context[field])
            for field in ("epoch", "tier", "seq")
        ):
            return None
        return artifact

    @app.get("/v2/synthetic-boolean/per-miner/challenges")
    async def v2_per_miner_challenges(
        request: Request,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str | None = Header(None),
    ):
        def _run():
            if not solution_manifest_enabled:
                raise HTTPException(404, "solution_manifest_v2_not_enabled")
            from . import per_miner as pm
            from . import real_corpus
            with v2_pipeline.v2_pm_env():
                if not v2_pipeline.v2_perminer_enabled():
                    raise HTTPException(404, "v2_per_miner_not_enabled")
                _require_v2_perminer_ready(pm)
                if x_cathedral_submitted_at is None:
                    raise HTTPException(401, "missing X-Cathedral-Submitted-At")
                _verify_hotkey_claim(
                    x_cathedral_hotkey, x_cathedral_signature, x_cathedral_submitted_at,
                    challenge_id="", dimacs_solution_sha256="",
                )
                mark_verified_hotkey(request, x_cathedral_hotkey)
                epoch = pm.current_epoch()
                # V1 parity: instances derive from the scoring identity (coldkey
                # collapse aware). Signing/receipts stay on the raw hotkey.
                v2_assignment_identity = _assignment_identity_for_hotkey(
                    x_cathedral_hotkey)
                effective_limit = pm.assignment_page_limit(limit)
                items = pm.miner_instance_set(
                    v2_assignment_identity, epoch, offset=offset, limit=effective_limit)
                if v2_submit_bitset_enabled and v2_lazy_issuance:
                    _require_v2_submit_token_mint_allowed(x_cathedral_hotkey)
                    if not v2_submit_token_secret:
                        raise HTTPException(503, "v2_submit_token_secret_missing")
                    for item in items:
                        item["assignment_encoding"] = "bitset/v1"
                        item["token_source"] = "cnf_fetch"
                elif v2_submit_bitset_enabled:
                    _require_v2_submit_token_mint_allowed(x_cathedral_hotkey)
                    if not v2_submit_token_secret:
                        raise HTTPException(503, "v2_submit_token_secret_missing")
                    expires_at = _now_iso_ms_plus(v2_submit_token_ttl_secs)
                    from ..dimacs import parse_cnf
                    for item in items:
                        tier_i = int(item["tier"])
                        seq_i = int(item["seq"])
                        cid = str(item["challenge_id"])
                        # Read-through the persistent CNF store first so warm rows
                        # avoid generation across process restarts. On a miss, use
                        # pm.item_meta() so process-local warm pages skip generation,
                        # parse_cnf and sha work. Both paths bind the submit token to
                        # the exact CNF bytes and submit/verify sha-gate again later.
                        cnf_text = v2_cnf_store.get(v2_store, cid)
                        if cnf_text is None:
                            meta_cid, cnf_sha, actual_nvars, is_real, cnf_text = pm.item_meta(
                                v2_assignment_identity, epoch, tier_i, seq_i)
                            if meta_cid != cid:
                                raise HTTPException(500, "v2_challenge_generation_mismatch")
                            try:
                                v2_cnf_store.put(v2_store, cid, cnf_text)
                            except Exception:
                                pass
                        else:
                            actual_nvars, _clauses = parse_cnf(cnf_text)
                            cnf_sha = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
                            is_real = pm.uses_real_instance(
                                v2_assignment_identity, epoch, tier_i, seq_i)
                        item["n_vars"] = actual_nvars
                        item["kind"] = (
                            real_corpus.kind_for(epoch, tier_i, seq_i, salt=v2_assignment_identity)
                            if is_real else "random_3sat_perminer"
                        )
                        item["cnf_sha256"] = cnf_sha
                        item["assignment_encoding"] = "bitset/v1"
                        item["submit_token"] = v2_bitset_submit.mint_submit_token(
                            secret=v2_submit_token_secret,
                            miner_hotkey=x_cathedral_hotkey,
                            challenge_id=cid,
                            epoch=epoch,
                            tier=tier_i,
                            seq=seq_i,
                            nvars=actual_nvars,
                            cnf_sha256=cnf_sha,
                            expires_at=expires_at,
                        )
                        item["submit_token_expires_at"] = expires_at
                return {
                    "family_id": _FAMILY,
                    "kind": "per_miner_v2",
                    "issuance": "lazy" if (v2_submit_bitset_enabled and v2_lazy_issuance) else "eager",
                    "epoch": epoch,
                    "miner_hotkey": x_cathedral_hotkey,
                    "assignment_identity": v2_assignment_identity,
                    "offset": offset,
                    "requested_limit": limit,
                    "limit": effective_limit,
                    "max_limit": pm.assignment_page_limit_max(),
                    "next_offset": offset + effective_limit,
                    "count": len(items),
                    "items": items,
                    "submit_path": "/v2/agents/submit-bitset" if v2_submit_bitset_enabled else "/v2/agents/submit-manifest",
                    "submit_bitset_path": "/v2/agents/submit-bitset",
                    "manifest_submit_path": "/v2/agents/submit-manifest",
                    "blob_upload_path": "/v2/blobs/solutions",
                    "cnf_path": "/v2/synthetic-boolean/per-miner/cnf",
                    "cnf_params": ["challenge_id", "tier", "seq"],
                    "cnf_access_path": (
                        "/v2/synthetic-boolean/per-miner/cnf-access"
                        if v2_cnf_artifacts_enabled else None
                    ),
                    "cnf_access_params": ["challenge_id", "tier", "seq"],
                }

        import asyncio
        payload = await asyncio.get_running_loop().run_in_executor(v2_read_executor, _run)
        return JSONResponse(
            payload,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v2/synthetic-boolean/per-miner/cnf-access")
    async def v2_per_miner_cnf_access(
        request: Request,
        challenge_id: str = Query(...),
        tier: int | None = Query(None, ge=1),
        seq: int | None = Query(None, ge=0),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str | None = Header(None),
    ):
        def _run():
            if not v2_cnf_artifacts_enabled:
                raise HTTPException(404, "v2_cnf_artifacts_not_enabled")
            with v2_pipeline.v2_pm_env():
                context = _authorize_v2_cnf_access(
                    request,
                    challenge_id=challenge_id,
                    tier=tier,
                    seq=seq,
                    hotkey=x_cathedral_hotkey,
                    signature=x_cathedral_signature,
                    submitted_at=x_cathedral_submitted_at,
                    require_submit_token=True,
                )
                epoch = int(context["epoch"])
                if not v2_cnf_artifacts.epoch_is_ready(v2_store, epoch):
                    raise HTTPException(503, "v2_cnf_artifacts_not_ready")
                artifact = _artifact_for_v2_cnf_context(context)
                if artifact is None:
                    # A ready epoch may never silently degrade to generation on
                    # the metadata path: that would put unique bytes back on the
                    # origin and make a partial publication look complete.
                    raise HTTPException(503, "v2_cnf_artifact_missing")
                token, expires_at = _mint_v2_cnf_submit_token(
                    context,
                    n_vars=int(artifact["n_vars"]),
                    cnf_sha256=str(artifact["cnf_sha256"]),
                )
                return {
                    "schema": v2_cnf_artifacts.ACCESS_SCHEMA,
                    "challenge_id": challenge_id,
                    "epoch": epoch,
                    "tier": int(context["tier"]),
                    "seq": int(context["seq"]),
                    "n_vars": int(artifact["n_vars"]),
                    "artifact_version": v2_cnf_artifacts.ARTIFACT_VERSION,
                    "artifact_url": str(artifact["artifact_url"]),
                    "artifact_key": str(artifact["artifact_key"]),
                    "cnf_sha256": str(artifact["cnf_sha256"]),
                    "cnf_bytes": int(artifact["cnf_bytes"]),
                    "content_type": v2_cnf_artifacts.ARTIFACT_CONTENT_TYPE,
                    "compression": "identity",
                    "artifact_cache_control": v2_cnf_artifacts.ARTIFACT_CACHE_CONTROL,
                    "assignment_encoding": "bitset/v1",
                    "submit_path": "/v2/agents/submit-bitset",
                    "submit_token": token,
                    "submit_token_expires_at": expires_at,
                }

        import asyncio
        payload = await asyncio.get_running_loop().run_in_executor(v2_read_executor, _run)
        return JSONResponse(
            payload,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Access-Control-Allow-Origin": "*",
            },
        )

    @app.get("/v2/synthetic-boolean/per-miner/cnf")
    async def v2_per_miner_cnf(
        request: Request,
        challenge_id: str = Query(...),
        tier: int | None = Query(None, ge=1),
        seq: int | None = Query(None, ge=0),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str | None = Header(None),
    ):
        def _run():
            with v2_pipeline.v2_pm_env():
                context = _authorize_v2_cnf_access(
                    request,
                    challenge_id=challenge_id,
                    tier=tier,
                    seq=seq,
                    hotkey=x_cathedral_hotkey,
                    signature=x_cathedral_signature,
                    submitted_at=x_cathedral_submitted_at,
                    require_submit_token=False,
                )
                pm = context["pm"]
                epoch = int(context["epoch"])
                tier_i = int(context["tier"])
                seq_i = int(context["seq"])
                assignment_identity = str(context["assignment_identity"])
                artifact = _artifact_for_v2_cnf_context(context)
                reused_published_bytes = False
                if artifact is not None:
                    cnf_text = v2_cnf_store.get(
                        v2_store,
                        challenge_id,
                        expected_sha256=str(artifact["cnf_sha256"]),
                    )
                    reused_published_bytes = cnf_text is not None
                else:
                    cnf_text = v2_cnf_store.get(v2_store, challenge_id)
                if cnf_text is None:
                    gen_cid, cnf_text, _ = pm.generate_instance(
                        assignment_identity, epoch, tier_i, seq_i)
                    if gen_cid != challenge_id:
                        raise HTTPException(404, "challenge_id_not_in_miner_set")
                    try:
                        v2_cnf_store.put(v2_store, challenge_id, cnf_text)
                    except Exception:
                        pass
                cnf_bytes = cnf_text.encode("utf-8")
                cnf_sha = hashlib.sha256(cnf_bytes).hexdigest()
                headers = {
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*",
                    "X-Cathedral-V2": "true",
                    "X-Perminer-Challenge-Id": challenge_id,
                    "X-Perminer-Tier": str(tier_i),
                    "X-Perminer-Seq": str(seq_i),
                    "X-Perminer-Epoch": str(epoch),
                    "X-Cathedral-CNF-Sha256": cnf_sha,
                    "X-Cathedral-CNF-Bytes": str(len(cnf_bytes)),
                }
                if reused_published_bytes and artifact is not None:
                    headers.update({
                        "X-Cathedral-CNF-Artifact-Reused": "true",
                        "X-Cathedral-CNF-Artifact-Key": str(artifact["artifact_key"]),
                    })
                if v2_submit_bitset_enabled:
                    _require_v2_submit_token_mint_allowed(x_cathedral_hotkey)
                    if not v2_submit_token_secret:
                        raise HTTPException(503, "v2_submit_token_secret_missing")
                    from ..dimacs import parse_cnf
                    actual_nvars, _clauses = parse_cnf(cnf_text)
                    token, expires_at = _mint_v2_cnf_submit_token(
                        context,
                        n_vars=actual_nvars,
                        cnf_sha256=cnf_sha,
                    )
                    headers.update({
                        "X-Cathedral-Submit-Path": "/v2/agents/submit-bitset",
                        "X-Cathedral-Submit-Token": token,
                        "X-Cathedral-Submit-Token-Expires-At": expires_at,
                        "X-Cathedral-Assignment-Encoding": "bitset/v1",
                    })
                return PlainTextResponse(
                    cnf_text,
                    media_type="text/plain; charset=utf-8",
                    headers=headers,
                )

        import asyncio
        return await asyncio.get_running_loop().run_in_executor(v2_read_executor, _run)

    def _v2_shadow_row_dict(row: Any) -> dict[str, Any]:
        try:
            keys = row.keys()
            return {k: row[k] for k in keys}
        except Exception:
            return dict(row)

    def _v2_shadow_v1_receipt(row: dict[str, Any], *, inserted: bool | None = None) -> dict[str, Any]:
        payload = {
            "schema": "cathedral.v2.shadow_v1_submit_receipt.v1",
            "shadow": True,
            "status": str(row.get("status") or "received"),
            "open": True,
            "terminal": False,
            "receipt_id": str(row["id"]),
            "receipt_url": f"/v2/shadow/v1/agents/submit/receipts/{row['id']}",
            "miner_hotkey": str(row["miner_hotkey"]),
            "challenge_id": str(row["challenge_id"]),
            "card_id": str(row["card_id"]),
            "solution_sha256": str(row["solution_sha256"]),
            "solution_bytes": int(row.get("solution_bytes") or 0),
            "solution_cid": str(row["solution_cid"]),
            "received_at": str(row["received_at_iso"]),
            "source": str(row.get("source") or "mirror"),
        }
        if row.get("submitted_at"):
            payload["submitted_at"] = str(row["submitted_at"])
        if inserted is not None:
            payload["idempotent_replay"] = not inserted
        return payload

    def _v2_shadow_v1_admit(
        *,
        miner_hotkey: str,
        challenge_id: str,
        card_id: str,
        solution_sha256: str,
        solution_bytes: int,
        solution_cid: str,
        request_sha256: str,
        content_type: str,
        submitted_at: str,
        received_at_iso: str,
        signature: str,
        source: str,
        form_json: str,
        headers_json: str,
    ) -> tuple[dict[str, Any], bool]:
        idem_body = json.dumps(
            {
                "miner_hotkey": miner_hotkey,
                "challenge_id": challenge_id,
                "solution_sha256": solution_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        idem = hashlib.sha256(b"cathedral:v2:shadow-v1-submit:\0" + idem_body).hexdigest()
        rid = "shv1_" + new_uuid().replace("-", "")

        def _tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO v2_shadow_v1_submits("
                "id, idempotency_key, miner_hotkey, challenge_id, card_id, "
                "solution_sha256, solution_bytes, solution_cid, request_sha256, "
                "content_type, status, submitted_at, received_at_iso, signature, "
                "source, form_json, headers_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    idem,
                    miner_hotkey,
                    challenge_id,
                    card_id,
                    solution_sha256,
                    int(solution_bytes),
                    solution_cid,
                    request_sha256,
                    content_type,
                    submitted_at,
                    received_at_iso,
                    signature,
                    source,
                    form_json,
                    headers_json,
                ),
            )
            row = conn.execute(
                "SELECT * FROM v2_shadow_v1_submits WHERE idempotency_key=? LIMIT 1",
                (idem,),
            ).fetchone()
            try:
                keys = row.keys()
                out = {k: row[k] for k in keys}
            except Exception:
                out = dict(row)
            return out, bool(out.get("id") == rid)

        return v2_store.write(_tx)

    def _v2_shadow_v1_meta_admit(body: dict[str, Any], *, header_hotkey: str, source_header: str) -> dict[str, Any]:
        rid = "shv1m_" + new_uuid().replace("-", "")

        def _str_field(name: str, default: str = "") -> str:
            value = body.get(name, default)
            if value is None:
                return default
            return str(value)

        def _int_field(name: str, default: int = 0) -> int:
            value = body.get(name, default)
            try:
                return max(0, int(value or 0))
            except Exception:
                return default

        miner_hotkey = (_str_field("miner_hotkey") or header_hotkey or "").strip()[:256]
        challenge_id = _str_field("challenge_id").strip()[:256]
        card_id = _str_field("card_id").strip()[:128]
        submitted_at = _str_field("submitted_at").strip()[:64]
        edge_received_at_iso = _str_field("edge_received_at_iso").strip()[:64]
        request_id = _str_field("request_id").strip()[:128]
        source = (_str_field("source") or source_header or "mirror-meta").strip()[:64]
        content_type = _str_field("content_type").strip()[:256]
        parse_error = _str_field("parse_error").strip()[:256]
        signature_present = 1 if bool(body.get("signature_present")) else 0
        received_at_iso = _now_iso_ms()

        def _tx(conn):
            conn.execute(
                "INSERT INTO v2_shadow_v1_submit_meta("
                "id, request_id, miner_hotkey, challenge_id, card_id, submitted_at, "
                "edge_received_at_iso, received_at_iso, source, original_content_length, "
                "original_body_bytes, dimacs_solution_bytes, field_count, signature_present, "
                "content_type, parse_error"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    request_id,
                    miner_hotkey,
                    challenge_id,
                    card_id,
                    submitted_at,
                    edge_received_at_iso,
                    received_at_iso,
                    source,
                    _int_field("original_content_length"),
                    _int_field("original_body_bytes"),
                    _int_field("dimacs_solution_bytes"),
                    _int_field("field_count"),
                    signature_present,
                    content_type,
                    parse_error,
                ),
            )
            row = conn.execute(
                "SELECT * FROM v2_shadow_v1_submit_meta WHERE id=? LIMIT 1",
                (rid,),
            ).fetchone()
            try:
                keys = row.keys()
                return {k: row[k] for k in keys}
            except Exception:
                return dict(row)

        return v2_store.write(_tx)

    @app.post("/v2/shadow/v1/agents/submit/meta")
    async def shadow_v1_submit_meta_v2(
        request: Request,
        x_cathedral_hotkey: str = Header(default=""),
        x_cathedral_shadow_source: str = Header(default=""),
    ):
        """Metadata-only live V1 submit mirror target for throughput probes.

        This intentionally does not store the DIMACS solution body and does not
        verify/sign/score anything. V1 remains authoritative. The row is an
        isolated V2 shadow measurement of live submit metadata write pressure.
        """
        if not (solution_manifest_enabled and v2_shadow_v1_enabled):
            raise HTTPException(404, "v2_shadow_v1_not_enabled")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("not_object")
        except Exception:
            raise HTTPException(400, "invalid_shadow_meta_json")
        row = _v2_shadow_v1_meta_admit(
            body,
            header_hotkey=x_cathedral_hotkey,
            source_header=x_cathedral_shadow_source,
        )
        return JSONResponse(
            {
                "schema": "cathedral.v2.shadow_v1_submit_meta_receipt.v1",
                "shadow": True,
                "metadata_only": True,
                "solution_body_stored": False,
                "status": "received",
                "receipt_id": str(row["id"]),
                "miner_hotkey": str(row.get("miner_hotkey") or ""),
                "challenge_id": str(row.get("challenge_id") or ""),
                "card_id": str(row.get("card_id") or ""),
                "dimacs_solution_bytes": int(row.get("dimacs_solution_bytes") or 0),
                "received_at": str(row.get("received_at_iso") or ""),
                "edge_received_at": str(row.get("edge_received_at_iso") or ""),
                "source": str(row.get("source") or "mirror-meta"),
            },
            status_code=202,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v2/shadow/v1/agents/submit/meta/metrics")
    def shadow_v1_submit_meta_metrics_v2():
        if not (solution_manifest_enabled and v2_shadow_v1_enabled):
            raise HTTPException(404, "v2_shadow_v1_not_enabled")
        totals = v2_store.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(original_body_bytes), 0) AS body_bytes, "
            "COALESCE(SUM(dimacs_solution_bytes), 0) AS solution_bytes, "
            "MIN(received_at_iso) AS first_received_at, MAX(received_at_iso) AS last_received_at "
            "FROM v2_shadow_v1_submit_meta"
        )[0]
        by_source = v2_store.query(
            "SELECT source, COUNT(*) AS n, COALESCE(SUM(original_body_bytes), 0) AS body_bytes, "
            "COALESCE(SUM(dimacs_solution_bytes), 0) AS solution_bytes "
            "FROM v2_shadow_v1_submit_meta GROUP BY source ORDER BY n DESC LIMIT 20"
        )
        recent = v2_store.query(
            "SELECT id, miner_hotkey, challenge_id, card_id, original_body_bytes, "
            "dimacs_solution_bytes, received_at_iso, edge_received_at_iso, source, parse_error "
            "FROM v2_shadow_v1_submit_meta ORDER BY received_at_iso DESC LIMIT 25"
        )
        now = datetime.now(timezone.utc)
        windows = {}
        for label, secs in (("1m", 60), ("5m", 300), ("1h", 3600)):
            since_dt = now - timedelta(seconds=secs)
            since = since_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{since_dt.microsecond // 1000:03d}Z"
            row = v2_store.query(
                "SELECT COUNT(*) AS n, COALESCE(SUM(original_body_bytes), 0) AS body_bytes, "
                "COALESCE(SUM(dimacs_solution_bytes), 0) AS solution_bytes "
                "FROM v2_shadow_v1_submit_meta WHERE received_at_iso > ?",
                (since,),
            )[0]
            windows[label] = {
                "count": int(row["n"] or 0),
                "body_bytes": int(row["body_bytes"] or 0),
                "solution_bytes": int(row["solution_bytes"] or 0),
            }
        return JSONResponse(
            {
                "schema": "cathedral.v2.shadow_v1_submit_meta_metrics.v1",
                "shadow": True,
                "metadata_only": True,
                "solution_body_stored": False,
                "total": {
                    "count": int(totals["n"] or 0),
                    "body_bytes": int(totals["body_bytes"] or 0),
                    "solution_bytes": int(totals["solution_bytes"] or 0),
                },
                "first_received_at": totals["first_received_at"],
                "last_received_at": totals["last_received_at"],
                "windows": windows,
                "by_source": [
                    {
                        "source": str(r["source"]),
                        "count": int(r["n"] or 0),
                        "body_bytes": int(r["body_bytes"] or 0),
                        "solution_bytes": int(r["solution_bytes"] or 0),
                    }
                    for r in by_source
                ],
                "recent": [_v2_shadow_row_dict(r) for r in recent],
            },
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.post("/v2/shadow/v1/agents/submit")
    async def shadow_v1_submit_v2(
        request: Request,
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(default="", alias="X-Cathedral-Submitted-At"),
    ):
        """Storage-only mirror target for live V1 submit traffic.

        This accepts the existing V1 submit form and V1 hotkey signature, stores
        the solution body as a beta blob plus an idempotent receipt row in the
        isolated V2 store, and returns quickly. It never writes V1 scoring,
        rewards, payout, or validator-weight ledgers.
        """
        if not (solution_manifest_enabled and v2_shadow_v1_enabled):
            raise HTTPException(404, "v2_shadow_v1_not_enabled")
        raw_body = await request.body()
        request_sha = hashlib.sha256(raw_body).hexdigest()
        try:
            form = await request.form()
        except Exception:
            raise HTTPException(400, "invalid_v1_submit_form")

        def _field(name: str, default: str = "") -> str:
            value = form.get(name, default)
            if value is None:
                return default
            if hasattr(value, "filename"):
                raise HTTPException(400, f"invalid_{name}")
            return str(value)

        card_id = _field("card_id")
        challenge_id = _field("challenge_id")
        dimacs_solution = _field("dimacs_solution")
        submitted_at = _field("submitted_at", x_cathedral_submitted_at or _now_iso_ms())
        display_name = _field("display_name", "")
        if card_id != _FAMILY:
            raise HTTPException(400, f"only card_id={_FAMILY} accepted")
        if not challenge_id or not dimacs_solution:
            raise HTTPException(400, "missing_challenge_id_or_dimacs_solution")

        solution_bytes = dimacs_solution.encode("utf-8")
        if v2_shadow_v1_max_solution_bytes > 0 and len(solution_bytes) > v2_shadow_v1_max_solution_bytes:
            raise HTTPException(413, "solution_too_large")
        sol_sha = hashlib.sha256(solution_bytes).hexdigest()
        submitted_at = _verify_hotkey_claim(
            x_cathedral_hotkey,
            x_cathedral_signature,
            submitted_at,
            challenge_id=challenge_id,
            dimacs_solution_sha256=sol_sha,
            alt_submitted_at=x_cathedral_submitted_at or None,
            allow_fallback_shapes=False,
        )
        mark_verified_hotkey(request, x_cathedral_hotkey)

        put = v2_blob_store.put(solution_bytes, kind="v1_submit_solution")
        received_at_iso = _now_iso_ms()
        source = (request.headers.get("x-cathedral-shadow-source") or "mirror").strip()[:64]
        content_type = (request.headers.get("content-type") or "").strip()[:256]
        form_json = json.dumps(
            {
                "card_id": card_id,
                "challenge_id": challenge_id,
                "display_name": display_name,
                "submitted_at": submitted_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        headers_json = json.dumps(
            {
                "content_type": content_type,
                "content_length": request.headers.get("content-length", ""),
                "shadow_source": source,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        row, inserted = _v2_shadow_v1_admit(
            miner_hotkey=x_cathedral_hotkey,
            challenge_id=challenge_id,
            card_id=card_id,
            solution_sha256=put.sha256,
            solution_bytes=put.size,
            solution_cid=put.cid,
            request_sha256=request_sha,
            content_type=content_type,
            submitted_at=submitted_at,
            received_at_iso=received_at_iso,
            signature=x_cathedral_signature,
            source=source,
            form_json=form_json,
            headers_json=headers_json,
        )
        return JSONResponse(
            _v2_shadow_v1_receipt(row, inserted=inserted),
            status_code=202 if inserted else 200,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v2/shadow/v1/agents/submit/receipts/{receipt_id}")
    def shadow_v1_submit_receipt_v2(receipt_id: str):
        if not (solution_manifest_enabled and v2_shadow_v1_enabled):
            raise HTTPException(404, "v2_shadow_v1_not_enabled")
        rows = v2_store.query(
            "SELECT * FROM v2_shadow_v1_submits WHERE id=? LIMIT 1",
            (receipt_id,),
        )
        if not rows:
            raise HTTPException(404, "receipt_not_found")
        return JSONResponse(
            _v2_shadow_v1_receipt(_v2_shadow_row_dict(rows[0])),
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v2/shadow/v1/agents/submit/metrics")
    def shadow_v1_submit_metrics_v2():
        if not (solution_manifest_enabled and v2_shadow_v1_enabled):
            raise HTTPException(404, "v2_shadow_v1_not_enabled")
        by_status = v2_store.query(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(solution_bytes), 0) AS bytes "
            "FROM v2_shadow_v1_submits GROUP BY status ORDER BY status"
        )
        totals = v2_store.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(solution_bytes), 0) AS bytes, "
            "MIN(received_at_iso) AS first_received_at, MAX(received_at_iso) AS last_received_at "
            "FROM v2_shadow_v1_submits"
        )[0]
        recent = v2_store.query(
            "SELECT id, miner_hotkey, challenge_id, solution_bytes, received_at_iso, source "
            "FROM v2_shadow_v1_submits ORDER BY received_at_iso DESC LIMIT 25"
        )
        now = datetime.now(timezone.utc)
        windows = {}
        for label, secs in (("1m", 60), ("5m", 300), ("1h", 3600)):
            since = (now - timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%S.") + f"{(now - timedelta(seconds=secs)).microsecond // 1000:03d}Z"
            row = v2_store.query(
                "SELECT COUNT(*) AS n, COALESCE(SUM(solution_bytes), 0) AS bytes "
                "FROM v2_shadow_v1_submits WHERE received_at_iso > ?",
                (since,),
            )[0]
            windows[label] = {"count": int(row["n"] or 0), "bytes": int(row["bytes"] or 0)}
        return JSONResponse(
            {
                "schema": "cathedral.v2.shadow_v1_submit_metrics.v1",
                "shadow": True,
                "total": {"count": int(totals["n"] or 0), "bytes": int(totals["bytes"] or 0)},
                "first_received_at": totals["first_received_at"],
                "last_received_at": totals["last_received_at"],
                "windows": windows,
                "by_status": [
                    {"status": str(r["status"]), "count": int(r["n"] or 0), "bytes": int(r["bytes"] or 0)}
                    for r in by_status
                ],
                "recent": [_v2_shadow_row_dict(r) for r in recent],
            },
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.post("/v2/blobs/solutions")
    async def upload_solution_blob_v2(
        request: Request,
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(...),
        x_cathedral_blob_sha256: str = Header(...),
    ):
        """Optional local/beta blob upload for V2.

        Production miners can publish directly to Hippius/IPFS/R2 and skip this.
        This route exists so the full blob -> manifest -> verify path can be
        tested on an isolated V2 stack without touching the V1 submit body path.
        """
        if not (solution_manifest_enabled and solution_blob_upload_enabled):
            raise HTTPException(404, "solution_blob_upload_v2_not_enabled")
        body = await request.body()
        if solution_blob_upload_max_bytes > 0 and len(body) > solution_blob_upload_max_bytes:
            raise HTTPException(413, "solution_blob_too_large")
        actual_sha = hashlib.sha256(body).hexdigest()
        if actual_sha != x_cathedral_blob_sha256.strip().lower():
            raise HTTPException(400, "blob_sha256_mismatch")
        ts = _parse_iso(x_cathedral_submitted_at)
        if ts is None or abs(time.time() - ts) > _SKEW_SECS:
            raise HTTPException(400, "submitted_at outside acceptable clock-skew window")
        msg = solution_manifest.canonical_blob_upload_bytes(
            miner_hotkey=x_cathedral_hotkey,
            submitted_at=x_cathedral_submitted_at,
            blob_sha256=actual_sha,
            blob_bytes=len(body),
            kind="solution",
        )
        if not verifier.verify(x_cathedral_hotkey, msg, x_cathedral_signature):
            raise HTTPException(401, "invalid hotkey signature")
        mark_verified_hotkey(request, x_cathedral_hotkey)
        put = v2_blob_store.put(body, kind="solution")
        return JSONResponse(
            {
                "schema": "cathedral.solution_blob.v1",
                "cid": put.cid,
                "sha256": put.sha256,
                "bytes": put.size,
            },
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    def _v2_submit_existing_receipt(submit: dict[str, Any]) -> dict[str, Any] | None:
        idem = v2_bitset_submit.idempotency_key(
            miner_hotkey=submit["miner_hotkey"],
            challenge_id=submit["challenge_id"],
        )
        rows = v2_store.query(
            "SELECT * FROM v2_submit_events WHERE idempotency_key=? LIMIT 1",
            (idem,),
        )
        if not rows:
            return None
        row = rows[0]
        try:
            return {k: row[k] for k in row.keys()}
        except Exception:
            return dict(row)

    def _is_db_unavailable_error(exc: BaseException) -> bool:
        """True when exc (or its cause/context chain) is a DB availability
        failure: psycopg2 OperationalError (connect refused/timeout under
        connection pressure), psycopg2 pool PoolError (pool exhausted), or
        sqlite3 OperationalError ("database is locked"). Matching by class
        name/module keeps psycopg2 an optional import on sqlite deployments."""
        seen: set[int] = set()
        cur: BaseException | None = exc
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            name = type(cur).__name__
            module = getattr(type(cur), "__module__", "") or ""
            if name in {"OperationalError", "PoolError"} and (
                    module.startswith("psycopg2") or module == "sqlite3"):
                return True
            cur = cur.__cause__ or cur.__context__
        return False

    def _v2_db_unavailable_response() -> JSONResponse:
        """Controlled shed for transient DB connection pressure on the V2
        submit/receipt hot path (open-v2 incident 2026-07-08): a distinct 503 +
        Retry-After instead of an unhandled psycopg2.OperationalError 500."""
        reason = "v2_db_unavailable_retry"
        return JSONResponse(
            {
                "schema": "cathedral.v2.db_unavailable.v1",
                "detail": reason,
                "reason": reason,
                "message": "V2 origin database is briefly saturated. Retry shortly.",
                "retry_after_seconds": v2_db_unavailable_retry_after_secs,
            },
            status_code=503,
            headers={
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
                "Retry-After": str(v2_db_unavailable_retry_after_secs),
                # Literal on purpose: the miner error contract drift-check
                # (test_miner_error_contract.py) greps static header values.
                "X-Cathedral-Rejection-Reason": "v2_db_unavailable_retry",
            },
        )

    def _v2_submit_backpressure_snapshot() -> dict[str, Any] | None:
        if not v2_submit_backpressure_enabled:
            return None
        pending = _v2_verify_pending_metrics_uncached()
        pending_count = int(pending.get("pending_count") or 0)
        oldest_age = pending.get("oldest_pending_age_secs")
        reasons: list[str] = []
        if (
            v2_submit_backpressure_max_pending > 0
            and pending_count >= v2_submit_backpressure_max_pending
        ):
            reasons.append("pending_count")
        if (
            v2_submit_backpressure_max_oldest_age_secs > 0
            and oldest_age is not None
            and float(oldest_age) >= v2_submit_backpressure_max_oldest_age_secs
        ):
            reasons.append("oldest_pending_age")
        if not reasons:
            return None
        return {
            "schema": "cathedral.v2.submit_backpressure.v1",
            "detail": "v2_submit_backpressure",
            "reason": "v2_submit_backpressure",
            "reasons": reasons,
            "pending_count": pending_count,
            "oldest_pending_age_secs": oldest_age,
            "max_pending": v2_submit_backpressure_max_pending,
            "max_oldest_pending_age_secs": v2_submit_backpressure_max_oldest_age_secs,
            "retry_after_seconds": v2_submit_backpressure_retry_after_secs,
        }

    @app.post("/v2/agents/submit-bitset")
    async def submit_bitset_v2(
        request: Request,
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(...),
    ):
        """Tiny PM-native V2 submit path.

        This path is beta/shadow-only. It admits only cheap-valid per-miner SAT
        assignments: token-bound challenge, hotkey signature, exact bitset shape,
        and SAT witness verification all pass before a durable event is written.
        """
        if not (solution_manifest_enabled and v2_submit_bitset_enabled):
            raise HTTPException(404, "v2_submit_bitset_not_enabled")
        if not v2_submit_token_secret:
            raise HTTPException(503, "v2_submit_token_secret_missing")
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > v2_submit_bitset_max_body_bytes:
                    raise HTTPException(413, "submit_bitset_body_too_large")
            except ValueError:
                raise HTTPException(400, "invalid_content_length")
        raw_body = await request.body()
        if len(raw_body) > v2_submit_bitset_max_body_bytes:
            raise HTTPException(413, "submit_bitset_body_too_large")
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            raise HTTPException(400, "invalid_json_submit_bitset")
        try:
            submit = v2_bitset_submit.normalize_submit_body(
                body,
                miner_hotkey=x_cathedral_hotkey,
                submitted_at=x_cathedral_submitted_at,
                card_id=_FAMILY,
            )
        except v2_bitset_submit.BitsetSubmitError as exc:
            raise HTTPException(400, exc.reason)

        ts = _parse_iso(x_cathedral_submitted_at)
        if ts is None or abs(time.time() - ts) > _SKEW_SECS:
            raise HTTPException(400, "submitted_at outside acceptable clock-skew window")
        msg = v2_bitset_submit.canonical_submit_bytes(submit)
        if not verifier.verify(x_cathedral_hotkey, msg, x_cathedral_signature):
            raise HTTPException(401, "invalid hotkey signature")
        mark_verified_hotkey(request, x_cathedral_hotkey)

        try:
            token_payload = v2_bitset_submit.verify_submit_token(
                submit["submit_token"],
                secret=v2_submit_token_secret,
                miner_hotkey=x_cathedral_hotkey,
                challenge_id=submit["challenge_id"],
            )
        except v2_bitset_submit.BitsetSubmitError as exc:
            raise HTTPException(400, exc.reason)

        from . import per_miner as pm
        from . import real_corpus

        # THIN SUBMIT (async scoring). The submit_token was already HMAC-verified
        # above, cryptographically binding miner_hotkey + challenge_id + epoch/tier/
        # seq + nvars + cnf_sha256. Ownership and challenge identity are proven
        # without regenerating the CNF. Decode the assignment shape cheaply, then
        # admit the row as received. The verify worker does witness-check + scoring
        # async and pushes the miner's flat results file.
        nvars = int(token_payload["nvars"])
        try:
            assignment_raw, _assignment = v2_bitset_submit.decode_assignment_b64(
                submit["assignment_b64"], nvars=nvars)
        except v2_bitset_submit.BitsetSubmitError as exc:
            raise HTTPException(400, exc.reason)

        try:
            existing = _v2_submit_existing_receipt(submit)
        except Exception as exc:
            if _is_db_unavailable_error(exc):
                return _v2_db_unavailable_response()
            raise
        if existing is not None:
            payload = v2_bitset_submit.receipt_payload(existing, inserted=False)
            payload["results_path"] = f"/v2/results/{x_cathedral_hotkey}.json"
            return JSONResponse(
                payload,
                status_code=200,
                headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
            )

        try:
            backpressure = _v2_submit_backpressure_snapshot()
        except Exception as exc:
            if _is_db_unavailable_error(exc):
                return _v2_db_unavailable_response()
            raise
        if backpressure is not None:
            return JSONResponse(
                backpressure,
                status_code=503,
                headers={
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*",
                    "Retry-After": str(v2_submit_backpressure_retry_after_secs),
                    "X-Cathedral-Rejection-Reason": "v2_submit_backpressure",
                },
            )

        def _admit_received():
            with v2_pipeline.v2_pm_env():
                if not v2_pipeline.v2_perminer_enabled():
                    raise HTTPException(404, "v2_per_miner_not_enabled")
                _require_v2_perminer_ready(pm)
                tier_i = int(token_payload["tier"])
                seq_i = int(token_payload["seq"])
                epoch_i = int(token_payload["epoch"])
                current_epoch = pm.current_epoch()
                if epoch_i not in (current_epoch, current_epoch - 1):
                    # V1 parity: stale-epoch tokens are refused at admit, so the
                    # verify worker and payout bridge only ever see in-window work.
                    raise HTTPException(410, "per_miner_challenge_expired")
                v2_assignment_identity = _assignment_identity_for_hotkey(
                    x_cathedral_hotkey)
                resolved = pm.resolve_tier_seq_for(
                    v2_assignment_identity, epoch_i, submit["challenge_id"],
                    tier=tier_i, seq=seq_i)
                if resolved is None:
                    raise HTTPException(400, "challenge_id_not_in_miner_set")
                challenge_kind = (
                    real_corpus.kind_for(epoch_i, tier_i, seq_i, salt=v2_assignment_identity)
                    if pm.uses_real_instance(v2_assignment_identity, epoch_i, tier_i, seq_i)
                    else "random_3sat_perminer"
                )
            return v2_bitset_submit.admit_received_event(
                v2_store,
                submit=submit,
                token_payload=token_payload,
                signature=x_cathedral_signature,
                assignment_raw=assignment_raw,
                received_at_iso=_now_iso_ms(),
                challenge_kind=challenge_kind,
            )

        try:
            row, inserted = await asyncio.get_running_loop().run_in_executor(
                v2_submit_executor, _admit_received)
        except Exception as exc:
            if _is_db_unavailable_error(exc):
                return _v2_db_unavailable_response()
            raise
        payload = v2_bitset_submit.receipt_payload(row, inserted=inserted)
        payload["results_path"] = f"/v2/results/{x_cathedral_hotkey}.json"
        return JSONResponse(
            payload,
            status_code=202 if inserted else 200,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v2/agents/submit-bitset/receipts/{receipt_id}")
    def submit_bitset_receipt_v2(receipt_id: str):
        if not (solution_manifest_enabled and v2_submit_bitset_enabled):
            raise HTTPException(404, "v2_submit_bitset_not_enabled")
        try:
            row = v2_bitset_submit.get_receipt(v2_store, receipt_id)
        except Exception as exc:
            if _is_db_unavailable_error(exc):
                return _v2_db_unavailable_response()
            raise
        if row is None:
            raise HTTPException(404, "receipt_not_found")
        return JSONResponse(
            v2_bitset_submit.receipt_payload(row),
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.post("/v2/agents/submit-manifest")
    async def submit_solution_manifest_v2(
        request: Request,
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(...),
    ):
        """Cheap, durable V2 admission for blob-backed solution submissions.

        This endpoint does not verify the solution or affect payout yet. It only
        authenticates a small manifest and writes an idempotent receipt row so we
        can test the new "miner uploads blob, Cathedral stores manifest" path
        beside the current synchronous submit.
        """
        if not solution_manifest_enabled:
            raise HTTPException(404, "solution_manifest_v2_not_enabled")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid_json_manifest")
        try:
            manifest = solution_manifest.normalize_manifest(
                body,
                miner_hotkey=x_cathedral_hotkey,
                submitted_at=x_cathedral_submitted_at,
                card_id=_FAMILY,
                max_solution_bytes=solution_manifest_max_bytes,
            )
        except solution_manifest.ManifestError as exc:
            raise HTTPException(400, exc.reason)

        ts = _parse_iso(x_cathedral_submitted_at)
        if ts is None or abs(time.time() - ts) > _SKEW_SECS:
            raise HTTPException(400, "submitted_at outside acceptable clock-skew window")
        msg = solution_manifest.canonical_manifest_bytes(manifest)
        if not verifier.verify(x_cathedral_hotkey, msg, x_cathedral_signature):
            raise HTTPException(401, "invalid hotkey signature")

        mark_verified_hotkey(request, x_cathedral_hotkey)
        # Capture a durable inline copy of the (small) solution blob at admit
        # time: the local blob dir is ephemeral container disk, so a redeploy
        # between admit and async verify loses the bytes (blob_fetch_failed on
        # the whole backlog). Best-effort — on any failure we admit exactly as
        # before; verify sha-checks whichever copy it uses.
        inline_solution: bytes | None = None
        try:
            if int(manifest.get("solution_bytes") or 0) <= solution_inline_max_bytes:
                inline_solution = v2_blob_store.fetch(
                    str(manifest["solution_cid"]), max_bytes=solution_inline_max_bytes)
        except Exception:
            inline_solution = None
        row, inserted = solution_manifest.admit_manifest(
            v2_store, manifest, signature=x_cathedral_signature,
            inline_solution=inline_solution)
        payload = solution_manifest.receipt_payload(row, inserted=inserted)
        return JSONResponse(
            payload,
            status_code=202 if inserted else 200,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v2/agents/submit-manifest/receipts/{receipt_id}")
    def solution_manifest_receipt_v2(receipt_id: str):
        if not solution_manifest_enabled:
            raise HTTPException(404, "solution_manifest_v2_not_enabled")
        row = solution_manifest.get_manifest_receipt(v2_store, receipt_id)
        if row is None:
            raise HTTPException(404, "receipt_not_found")
        return JSONResponse(
            solution_manifest.receipt_payload(row),
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    # TTL cache for the DB-heavy metrics blocks. /v2/verify/metrics is publicly
    # polled (the miner announcement points at it), so per-request COUNT(*) and
    # GROUP BY sweeps over the (large, growing) event tables turn N pollers
    # into N concurrent table scans and starve the verify worker's DB access.
    # One caller recomputes when stale; everyone else gets the cached copy.
    _v2_metrics_cache: dict[str, tuple[float, Any]] = {}
    _v2_metrics_cache_lock = threading.Lock()

    def _v2_metrics_cached(key: str, ttl_secs: float, compute):
        now_ts = time.time()
        with _v2_metrics_cache_lock:
            entry = _v2_metrics_cache.get(key)
            if entry and (now_ts - entry[0]) < ttl_secs:
                return entry[1]
        value = compute()
        with _v2_metrics_cache_lock:
            _v2_metrics_cache[key] = (time.time(), value)
        return value

    def _v2_verify_pending_metrics_uncached() -> dict[str, Any]:
        now_ts = time.time()
        pending_count = 0
        oldest_iso: str | None = None
        tables = (
            ("solution_manifests", "manifest"),
            ("v2_submit_events", "bitset"),
        )
        by_source: dict[str, dict[str, Any]] = {}
        for table, source in tables:
            try:
                rows = v2_store.query(
                    f"SELECT COUNT(*) AS n, MIN(received_at_iso) AS oldest "
                    f"FROM {table} WHERE status IN (?, ?)",
                    (v2_pipeline.STATUS_RECEIVED, v2_pipeline.STATUS_RETRY),
                )
                n = int(rows[0]["n"] or 0) if rows else 0
                oldest = str(rows[0]["oldest"] or "") if rows else ""
            except Exception:
                n = 0
                oldest = ""
            pending_count += n
            if oldest and (oldest_iso is None or oldest < oldest_iso):
                oldest_iso = oldest
            by_source[source] = {"pending_count": n, "oldest_pending_at": oldest or None}
        oldest_age = None
        if oldest_iso:
            ts = v2_bitset_submit.parse_iso(oldest_iso)
            if ts is not None:
                oldest_age = max(0.0, now_ts - ts)
        return {
            "pending_count": pending_count,
            "oldest_pending_at": oldest_iso,
            "oldest_pending_age_secs": round(oldest_age, 3) if oldest_age is not None else None,
            "by_source": by_source,
        }

    def _v2_verify_pending_metrics() -> dict[str, Any]:
        return _v2_metrics_cached(
            "pending", 20.0, _v2_verify_pending_metrics_uncached)

    def _v2_verify_kind_metrics_uncached() -> dict[str, Any]:
        """Attributed real-vs-planted solve counts from verified v2_submit_events.

        challenge_kind is metadata only (see kind_for docstring) — this never
        touches scoring/eligibility. NULL challenge_kind (pre-existing rows
        from before the column existed) is bucketed as "unknown" rather than
        raising.
        """
        try:
            rows = v2_store.query(
                "SELECT challenge_kind, miner_hotkey, COUNT(*) AS n "
                "FROM v2_submit_events WHERE status = ? "
                "GROUP BY challenge_kind, miner_hotkey",
                (v2_pipeline.STATUS_VERIFIED,),
            )
        except Exception:
            rows = []
        totals = {"real": 0, "planted": 0, "unknown": 0}
        kinds: dict[str, int] = {}
        per_hotkey: dict[str, dict[str, int]] = {}
        for row in rows:
            kind = row["challenge_kind"]
            hotkey = str(row["miner_hotkey"] or "")
            n = int(row["n"] or 0)
            kind_label = str(kind) if kind else "unknown"
            kinds[kind_label] = kinds.get(kind_label, 0) + n
            if kind_label in ("coloring", "latin"):
                bucket = "real"
            elif kind_label == "random_3sat_perminer":
                bucket = "planted"
            else:
                bucket = "unknown"
            totals[bucket] += n
            if bucket in ("real", "planted") and hotkey:
                entry = per_hotkey.setdefault(hotkey, {"real": 0, "planted": 0})
                entry[bucket] += n
        top_hotkeys = sorted(
            per_hotkey.items(),
            key=lambda kv: kv[1]["real"] + kv[1]["planted"],
            reverse=True,
        )[:50]
        return {
            "totals": totals,
            "kinds": kinds,
            "by_hotkey": dict(top_hotkeys),
        }

    def _v2_verify_kind_metrics() -> dict[str, Any]:
        return _v2_metrics_cached(
            "by_kind", 60.0, _v2_verify_kind_metrics_uncached)

    @app.get("/v2/verify/metrics")
    def v2_verify_metrics():
        if not solution_manifest_enabled:
            raise HTTPException(404, "solution_manifest_v2_not_enabled")
        now = time.time()
        metrics = dict(app.state.v2_verify_metrics)
        recent = [e for e in (metrics.get("recent_events") or []) if now - float(e.get("ts") or 0.0) <= 60.0]
        errors = [e for e in (metrics.get("tick_errors") or []) if now - float(e.get("ts") or 0.0) <= 60.0]
        verified_last_60s = sum(int(e.get("verified") or 0) for e in recent)
        rejected_last_60s = sum(int(e.get("rejected") or 0) for e in recent)
        total_last_60s = sum(int(e.get("total") or 0) for e in recent)
        pending = _v2_verify_pending_metrics()
        payload = {
            "schema": "cathedral.v2.verify_metrics.v1",
            "enabled": bool(v2_worker_enabled),
            "service_role": service_role,
            "worker_id": metrics.get("worker_id"),
            "lock_held_by_self": bool(metrics.get("lock_held_by_self")),
            "last_lock_acquired_at": metrics.get("last_lock_acquired_at"),
            "last_lock_contended_at": metrics.get("last_lock_contended_at"),
            "last_batch_at": metrics.get("last_batch_at"),
            "last_batch_ms": metrics.get("last_batch_ms"),
            "last_batch_count": int(metrics.get("last_batch_count") or 0),
            "verified_last_60s": verified_last_60s,
            "rejected_last_60s": rejected_last_60s,
            "processed_last_60s": total_last_60s,
            "verify_rate_per_sec": round(total_last_60s / 60.0, 6),
            "tick_errors_last_60s": len(errors),
            "worker_restarts": int(metrics.get("worker_restarts") or 0),
            "last_worker_error": metrics.get("last_worker_error"),
            "lock_steals": int(metrics.get("lock_steals") or 0),
            **pending,
            "by_kind": _v2_verify_kind_metrics(),
            **v2_pipeline.cnf_store_metrics(),
        }
        return JSONResponse(
            payload,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.post("/v2/admin/verify/tick")
    def v2_verify_tick_admin(authorization: str | None = Header(None)):
        if not solution_manifest_enabled:
            raise HTTPException(404, "solution_manifest_v2_not_enabled")
        _require_v2_admin(authorization)
        results = v2_pipeline.process_batch(
            v2_store,
            v2_blob_store,
            worker_id=f"v2:manual:{new_uuid()[:8]}",
            batch_size=v2_worker_batch_size,
            lock_secs=v2_worker_lock_secs,
            max_blob_bytes=v2_worker_max_blob_bytes,
        )
        return JSONResponse(
            {"schema": "cathedral.v2.verify_tick.v1", "count": len(results), "results": results},
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v2/validator/weights/next")
    def validator_weights_next_v2():
        if not solution_manifest_enabled:
            raise HTTPException(404, "solution_manifest_v2_not_enabled")
        # TTL-cached: this endpoint is publicly polled (the miner announcement
        # calls it the fast-path scoreboard) and a full vector rebuild is a
        # 20-40s multi-query sweep of the 24h window. Uncached, concurrent
        # pollers exhausted the PG pool (PoolError -> 500s) and starved the
        # reward poster. One caller rebuilds every 20s; everyone else gets the
        # cached signed vector (valid_for_secs is 1800s, so a 20s-stale copy
        # is always still comfortably valid for consumers).
        vector = _v2_metrics_cached(
            "weights_next_v2", 20.0,
            lambda: v2_pipeline.build_shadow_weight_vector(
                v2_store,
                signing_key_hex=key_hex,
                window_hours=_env_float("CATHEDRAL_V2_WEIGHTS_WINDOW_HOURS", 24.0),
                valid_for_secs=_env_float("CATHEDRAL_V2_WEIGHTS_VALID_FOR_SECS", 1800.0),
            ))
        return JSONResponse(
            vector,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v2/audit/epochs/{epoch}")
    def audit_epoch_v2(epoch: int):
        if not solution_manifest_enabled:
            raise HTTPException(404, "solution_manifest_v2_not_enabled")
        bundle = v2_pipeline.audit_bundle(v2_store, epoch=epoch, signing_key_hex=key_hex)
        return JSONResponse(
            bundle,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    # ---- Public receipts feed (scrubbed, signed, per-epoch) ---------------
    # Public and read-only by design: it only ever surfaces already-VERIFIED
    # rows, scrubbed to a fixed field list (see v2_receipts._public_receipt).
    # Epochs are append-only once the epoch has fully passed, so a 5-minute
    # cache is safe; /latest still needs to notice a brand-new epoch quickly,
    # so its own MAX(epoch) lookup is cached much shorter (60s).
    def _receipts_bundle_for_epoch(epoch: int) -> dict[str, Any]:
        return _v2_metrics_cached(
            f"receipts:{epoch}", 300.0,
            lambda: v2_receipts.build_receipts_bundle(
                v2_store,
                epoch=epoch,
                signing_key_hex=key_hex,
                coldkey_resolver=v2_receipts_coldkey_resolver,
            ))

    @app.get("/v2/receipts/epochs/{epoch}")
    def receipts_epoch_v2(epoch: int):
        if not solution_manifest_enabled:
            raise HTTPException(404, "solution_manifest_v2_not_enabled")
        bundle = _receipts_bundle_for_epoch(epoch)
        return JSONResponse(
            bundle,
            headers={
                "Cache-Control": "public, max-age=60",
                "Access-Control-Allow-Origin": "*",
            },
        )

    @app.get("/v2/receipts/latest")
    def receipts_latest_v2():
        if not solution_manifest_enabled:
            raise HTTPException(404, "solution_manifest_v2_not_enabled")
        epoch = _v2_metrics_cached(
            "receipts:latest_epoch", 60.0,
            lambda: v2_receipts.latest_verified_epoch(v2_store))
        bundle = _receipts_bundle_for_epoch(int(epoch) if epoch is not None else 0)
        return JSONResponse(
            bundle,
            headers={
                "Cache-Control": "public, max-age=60",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # ---- Durable submit receipts (Phase 4) --------------------------------
    @app.get("/v1/agents/receipts/{receipt_id}")
    def agents_receipt(receipt_id: str):
        """Look up a durable submit receipt by id. Returns the same receipt shape
        the 202 admission returned, with `status` advancing pending -> ranked/
        rejected as the async worker verifies. 404 if unknown."""
        receipt = submit_admission.get_receipt(store, receipt_id)
        if receipt is not None:
            return JSONResponse(receipt, headers={"Cache-Control": "no-store"})
        rows = store.query(
            "SELECT id, miner_hotkey, sat_challenge_id, status, rejection_reason "
            "FROM agent_submissions WHERE id=?",
            (receipt_id,),
        )
        if not rows:
            raise HTTPException(404, "receipt_not_found")
        row = rows[0]
        status = str(row["status"])
        payload = {
            "schema": "cathedral.submit_receipt.v1",
            "status": status,
            "open": False,
            "terminal": True,
            "receipt_id": str(row["id"]),
            "challenge_id": str(row["sat_challenge_id"]),
            "miner_hotkey": str(row["miner_hotkey"]),
            "receipt_url": f"/v1/agents/receipts/{row['id']}",
        }
        if row["rejection_reason"] is not None:
            payload["rejection_reason"] = row["rejection_reason"]
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    # ---- Async verification finalize (Phase 5) ----------------------------
    # Mirror of the legacy inline public-lane _accept: one atomic txn that burns
    # the signature, claims the distinct-solver slot, writes the submission row and
    # the signed feed rows. Reused by the worker so the async path has IDENTICAL
    # scoring/payout semantics to the synchronous path — only the timing differs.
    def _accept_public_async(receipt_id, attempt_row, check, now_iso):
        challenge_id = str(attempt_row["challenge_id"])
        miner_hotkey = str(attempt_row["miner_hotkey"])
        signature = str(attempt_row["signature"])
        submitted_at = attempt_row["submitted_at"]
        received_at_iso = str(attempt_row["received_at_iso"] or now_iso)
        chal_rows = store.query(
            "SELECT * FROM lane_challenges WHERE challenge_id=?", (challenge_id,))
        if not chal_rows:
            return ("challenge_not_active", None, None, None)
        chal = chal_rows[0]
        if chal["status"] not in ("active", "locked"):
            return ("challenge_not_active", None, None, None)
        row_uuid = new_uuid()
        answer_hash = sha256_hex(",".join(str(x) for x in check.assignment))
        sol_sha = str(attempt_row["dimacs_solution_sha256"])
        verifier_details_hash = sha256_hex(f"{challenge_id}:{sol_sha}")
        lock_wins = scoring.submit_mode() == "lock_wins"

        def _accept(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO submit_signatures(signature, seen_at) VALUES (?, ?)",
                (signature, now_iso))
            if not cur.rowcount:
                return ("replayed_signature", None, None, None)
            if lock_wins:
                locked = conn.execute(
                    "UPDATE lane_challenges SET status='locked' "
                    "WHERE challenge_id=? AND status='active'", (challenge_id,))
                if locked.rowcount != 1:
                    return ("challenge_already_locked", None, None, None)
                rank = (
                    scoring.claim_solve(
                        conn, challenge_id, miner_hotkey, received_at_iso)
                    or 1
                )
            else:
                active = conn.execute(
                    "UPDATE lane_challenges SET updated_at_iso=? "
                    "WHERE challenge_id=? AND status='active'", (now_iso, challenge_id))
                if active.rowcount != 1:
                    return ("challenge_not_active", None, None, None)
                rank = scoring.claim_solve(
                    conn, challenge_id, miner_hotkey, received_at_iso)
                if rank is None:
                    return ("already_solved", None, None, None)
            score_multiplier = float(chal["score_multiplier"])
            ws = (
                scoring.weighted_score_for(store, miner_hotkey)
                * score_multiplier
                * _public_row_score_multiplier()
            )
            conn.execute(
                "INSERT INTO agent_submissions(id, miner_hotkey, sat_challenge_id, "
                "status, rejection_reason, current_score, seq_no, submitted_at, signature) "
                "VALUES (?, ?, ?, 'ranked', NULL, ?, ?, ?, ?)",
                (receipt_id, miner_hotkey, challenge_id, ws, rank,
                 submitted_at, signature))
            emitted = rows.build_solve_rows(
                row_uuid=row_uuid, miner_hotkey=miner_hotkey,
                agent_id=new_uuid(), challenge_id=challenge_id, tier=chal["tier"],
                weighted_score=ws, answer_hash=answer_hash,
                verifier_details_hash=verifier_details_hash, ran_at=received_at_iso,
                epoch_salt=epoch_salt, solve_rank=rank, solved=True,
                private_key_hex=key_hex,
            )
            for r in emitted:
                conn.execute(
                    "INSERT OR IGNORE INTO eval_runs "
                    "(id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (r["id"], r["ran_at"], int(r["eval_output_schema_version"]),
                     r["miner_hotkey"], r["task_type"], json.dumps(r)))
            # Advance the receipt to its terminal ranked result in the SAME txn so a
            # crash between feed rows and receipt update cannot exist.
            conn.execute(
                "UPDATE per_miner_attempts SET status='ranked', rejection_reason=NULL, "
                "verified_at_iso=?, recorded_at_iso=?, solve_rank=?, weighted_score=?, "
                "eval_run_id=?, solution_body=NULL, locked_by=NULL, locked_until_iso=NULL "
                "WHERE id=?",
                (now_iso, now_iso, rank, ws, row_uuid, receipt_id))
            return (None, rank, ws, row_uuid)

        err, rank, ws, eval_run_id = store.write(_accept)
        if err is None and lock_wins:
            board_cache_mod.invalidate_all()
        return (err, rank, ws, eval_run_id)

    def _async_verify_load_cnf(challenge_id):
        rows_ = store.query(
            "SELECT cnf_text FROM lane_challenges WHERE challenge_id=?", (challenge_id,))
        return rows_[0]["cnf_text"] if rows_ else None

    # ---- TRACK 1: pm-* worker resolve+verify and atomic accept ------------
    # The HEAVY pm-* work the inline handler used to do on the request path: from
    # the durable attempt row, re-derive the miner's OWN CNF off assignment_identity,
    # run the DIMACS satisfaction check, then the anti-copy ownership/witness check.
    # Returns (ok, reason, check) where `check` carries the verified assignment and
    # the resolved (tier, seq) used to build the witness — same logic as inline.
    def _pm_resolve_and_verify(attempt_row):
        from . import per_miner as pm
        challenge_id = str(attempt_row["challenge_id"])
        identity = attempt_row["assignment_identity"] or str(attempt_row["miner_hotkey"])
        epoch = int(attempt_row["epoch"] or 0)
        body = attempt_row["solution_body"]
        tier_seq = pm.resolve_tier_seq_for(identity, epoch, challenge_id)
        if tier_seq is None:
            return (False, "assignment_required_fetch_challenges_first", None)
        cnf = pm.get_miner_cnf(identity, epoch, tier_seq[0], tier_seq[1])
        if cnf is None:
            return (False, "challenge_id_not_in_miner_set", None)
        _cid, cnf_text = cnf
        check = verify_dimacs_solution(cnf_text, body)
        if not check.ok:
            return (False, check.rejection_reason, None)
        ok, reason = pm.verify_miner_submission_for(
            identity, epoch, tier_seq[0], tier_seq[1], challenge_id, check.assignment)
        if not ok:
            return (False, reason, None)
        # Stash tier/seq on the check so the accept path needs no second lookup.
        return (True, None, (check, tier_seq[0], tier_seq[1], identity, epoch))

    # Mirror of the inline `_pm_accept`: one atomic txn that burns the signature,
    # claims the distinct per-miner solve (NO double payout), writes the submission,
    # witness, and signed feed rows, and advances the receipt to its terminal ranked
    # result. Reused by the worker so the async pm path has IDENTICAL payout/scoring
    # semantics to the synchronous one — only the timing differs.
    def _accept_pm_async(receipt_id, attempt_row, resolved, now_iso):
        from . import per_miner as pm
        check, tier, seq, _identity, epoch = resolved
        challenge_id = str(attempt_row["challenge_id"])
        miner_hotkey = str(attempt_row["miner_hotkey"])
        signature = str(attempt_row["signature"])
        submitted_at = attempt_row["submitted_at"]
        received_at_iso = str(attempt_row["received_at_iso"] or now_iso)
        sol_sha = str(attempt_row["dimacs_solution_sha256"])
        dimacs_solution = attempt_row["solution_body"]
        pm_weight = pm.weight_for(tier)
        row_uuid = new_uuid()
        answer_hash = sha256_hex(",".join(str(x) for x in check.assignment))
        verifier_details_hash = sha256_hex(f"{challenge_id}:{sol_sha}")

        def _accept(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO submit_signatures(signature, seen_at) VALUES (?, ?)",
                (signature, now_iso))
            if not cur.rowcount:
                return "replayed_signature"
            solved = conn.execute(
                "INSERT OR IGNORE INTO per_miner_solves"
                "(challenge_id, miner_hotkey, epoch, tier, seq, difficulty_weight, "
                "verified, solved_at_iso) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (challenge_id, miner_hotkey, epoch, tier, seq, pm_weight, 1,
                 received_at_iso))
            if not solved.rowcount:
                return "already_solved"
            conn.execute(
                "INSERT INTO agent_submissions(id, miner_hotkey, sat_challenge_id, "
                "status, rejection_reason, current_score, seq_no, submitted_at, signature) "
                "VALUES (?, ?, ?, 'ranked', NULL, ?, 1, ?, ?)",
                (receipt_id, miner_hotkey, challenge_id, pm_weight, submitted_at,
                 signature))
            conn.execute(
                "INSERT INTO per_miner_witnesses(challenge_id, miner_hotkey, epoch, "
                "tier, seq, dimacs_solution_sha256, answer_hash, dimacs_solution, "
                "recorded_at_iso) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (challenge_id, miner_hotkey, epoch, tier, seq, sol_sha,
                 answer_hash, dimacs_solution, now_iso))
            emitted = rows.build_solve_rows(
                row_uuid=row_uuid, miner_hotkey=miner_hotkey,
                agent_id=new_uuid(), challenge_id=challenge_id, tier=tier,
                weighted_score=pm_weight, answer_hash=answer_hash,
                verifier_details_hash=verifier_details_hash, ran_at=received_at_iso,
                epoch_salt=epoch_salt, solve_rank=1, solved=True,
                private_key_hex=key_hex,
            )
            for r in emitted:
                conn.execute(
                    "INSERT OR IGNORE INTO eval_runs "
                    "(id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (r["id"], r["ran_at"], int(r["eval_output_schema_version"]),
                     r["miner_hotkey"], r["task_type"], json.dumps(r)))
            # Advance the receipt to its terminal ranked result in the SAME txn so a
            # crash between feed rows and receipt update cannot exist.
            conn.execute(
                "UPDATE per_miner_attempts SET status='ranked', rejection_reason=NULL, "
                "verified_at_iso=?, recorded_at_iso=?, solve_rank=1, weighted_score=?, "
                "eval_run_id=?, solution_body=NULL, locked_by=NULL, locked_until_iso=NULL "
                "WHERE id=?",
                (now_iso, now_iso, pm_weight, row_uuid, receipt_id))
            return None

        return store.write(_accept)

    def _pm_log_divergence(*, challenge_id, receipt_id, inline_status,
                           async_status, async_reason):
        print("[verify] pm_shadow_divergence " + json.dumps({
            "challenge_id": challenge_id, "receipt_id": receipt_id,
            "inline": inline_status, "async": async_status,
            "async_reason": async_reason,
        }, sort_keys=True))
        _record_submit_event(
            "shadow_divergence", str(async_reason or async_status),
            challenge_id=challenge_id)

    def _async_verify_tick(*, worker_id, batch_size=8, lock_secs=120):
        """Claim and verify up to `batch_size` pending attempts. Returns the count
        processed. Safe to call from a loop or a test. The claim is kind-agnostic and
        ordered by received_at (fairness); each row is dispatched to the finalizer for
        its challenge_kind — public, per_miner (authoritative), or per_miner_shadow."""
        now_iso = _now_iso_ms()
        deadline = _now_iso_ms_plus(lock_secs)
        claimed = submit_admission.claim_pending(
            store, worker_id=worker_id, now_iso=now_iso,
            lock_deadline_iso=deadline, batch_size=batch_size)
        for attempt in claimed:
            kind = attempt["challenge_kind"]

            def rec(outcome, reason, challenge_id=None):
                return _record_submit_event(
                    outcome, reason, challenge_id=challenge_id
                )

            if kind == submit_admission.KIND_PER_MINER_SHADOW:
                submit_admission.finalize_pm_shadow(
                    store, attempt, now_iso=_now_iso_ms(),
                    resolve_and_verify=_pm_resolve_and_verify,
                    log_divergence=_pm_log_divergence,
                )
            elif kind == submit_admission.KIND_PER_MINER:
                submit_admission.finalize_pm_attempt(
                    store, attempt, now_iso=_now_iso_ms(),
                    resolve_and_verify=_pm_resolve_and_verify,
                    accept_pm=_accept_pm_async,
                    record_event=rec,
                )
            else:
                submit_admission.finalize_attempt(
                    store, attempt, now_iso=_now_iso_ms(),
                    load_cnf=_async_verify_load_cnf,
                    verify_dimacs=verify_dimacs_solution,
                    accept_public=_accept_public_async,
                    record_event=rec,
                )
        return len(claimed)

    app.state.async_verify_tick = _async_verify_tick

    # ---- M3: Lane S registry ----------------------------------------------
    @app.post("/v1/arena/solvers")
    def arena_register_solver(
        source_url: str = Form(...),
        container_digest: str = Form(...),
        source_sha256: str = Form(...),
        submitted_at: str = Form(None),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
    ):
        submitted_at = submitted_at or _now_iso_ms()
        # sign over the solver source hash via the 6-field claim shape (reuse).
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, submitted_at,
            challenge_id="arena", dimacs_solution_sha256=source_sha256,
            allow_fallback_shapes=False,
        )
        spec = SolverSpec(source_url, container_digest, source_sha256,
                          owner_hotkey=x_cathedral_hotkey)
        accepted, reason = arena_registry.register(spec)

        def _store(conn):
            conn.execute(
                "INSERT OR IGNORE INTO arena_solvers(source_sha256, source_url, "
                "container_digest, owner_hotkey, registered_round, status, created_at_iso) "
                "VALUES (?, ?, ?, ?, 0, 'pending', ?)",
                (source_sha256, source_url, container_digest, x_cathedral_hotkey,
                 _now_iso_ms()))
        store.write(_store)
        return {"accepted": accepted, "reason": reason, "commitment_id": spec.commitment_id}

    @app.get("/v1/arena/status")
    def arena_status():
        pending = store.query(
            "SELECT source_sha256, owner_hotkey FROM arena_solvers WHERE status='pending'")
        champ = store.query(
            "SELECT source_sha256, owner_hotkey FROM arena_solvers WHERE status='champion' LIMIT 1")
        return {
            "champion": (dict(champ[0]) if champ else None),
            "pending_challengers": [dict(r) for r in pending],
            "count_pending": len(pending),
        }

    # ---- M3: Lane I intake ------------------------------------------------
    @app.post("/v1/arena/instances")
    def arena_submit_instance(
        cnf_text: str = Form(...),
        round_no: int = Form(...),
        submitted_at: str = Form(None),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
    ):
        submitted_at = submitted_at or _now_iso_ms()
        cnf_sha = sha256_hex(cnf_text)
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, submitted_at,
            challenge_id="arena-instance", dimacs_solution_sha256=cnf_sha,
            allow_fallback_shapes=False,
        )
        instance_id = new_uuid()
        quarantine_until = round_no + _QUARANTINE_ROUNDS

        def _store(conn):
            conn.execute(
                "INSERT INTO arena_instances(instance_id, owner_hotkey, cnf_sha256, "
                "cnf_text, submitted_round, quarantine_until_round, min_batch_score, "
                "status, created_at_iso) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (instance_id, x_cathedral_hotkey, cnf_sha, cnf_text, round_no,
                 quarantine_until, _MIN_BATCH_SCORE, _now_iso_ms()))
        store.write(_store)
        return {
            "instance_id": instance_id, "submitted_round": round_no,
            "quarantine_until_round": quarantine_until,
            "min_batch_score": _MIN_BATCH_SCORE,
        }

    return app


def _empty_bundle_hash() -> str:
    """blake3 of empty bytes — the bundle_hash miners sign when no card bundle is
    uploaded (the SAT path). Matches the monolith's blake3(b'') convention."""
    try:
        import blake3
        return blake3.blake3(b"").hexdigest()
    except Exception:
        # fallback if blake3 unavailable — sha256 of empty (dev/stub only).
        return hashlib.sha256(b"").hexdigest()


# --------------------------------------------------------------------------
# CNF-store registry — maps a Store id to its CNFStore so the module-level mint
# helper (seed_challenge) can upload immutable bodies to the bucket backend
# without an app reference. Keyed by id(store) because one process may build
# several apps (the gates do). No-op for the default `db` backend.
# --------------------------------------------------------------------------
_CNF_STORES: dict[int, "CNFStore"] = {}


def _register_cnf_store(store: Store, cnf_store: "CNFStore") -> None:
    _CNF_STORES[id(store)] = cnf_store


def _cnf_put_on_mint(store: Store, challenge_id: str, cnf_text: str) -> None:
    cs = _CNF_STORES.get(id(store))
    if cs is None or cs.backend != "bucket" or not cnf_text:
        return
    try:
        cs.put(challenge_id, cnf_text, sha256=sha256_hex(cnf_text))
    except Exception as e:  # never let an object-store blip fail a mint
        print(f"[cnf] bucket put failed for {challenge_id}: {e!r}")


# --------------------------------------------------------------------------
# Seeding helpers (used by the e2e script + tests).
# --------------------------------------------------------------------------
def seed_challenge(store: Store, *, challenge_id: str, tier: int, cnf_text: str,
                   status: str = "active", difficulty_label: str | None = None,
                   score_multiplier: float = 1.0,
                   designated_solver_digest: str | None = None) -> None:
    from ..dimacs import parse_cnf
    n_vars, clauses = parse_cnf(cnf_text)
    cnf_bytes = len(cnf_text.encode("utf-8"))

    def _do(conn):
        conn.execute(
            "INSERT OR REPLACE INTO lane_challenges(challenge_id, family_id, tier, "
            "cnf_text, cnf_sha256, cnf_bytes, num_vars, num_clauses, status, "
            "score_multiplier, difficulty_label, designated_solver_digest, created_at_iso) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (challenge_id, _FAMILY, tier, cnf_text, sha256_hex(cnf_text), cnf_bytes,
             n_vars, len(clauses), status, score_multiplier, difficulty_label,
             designated_solver_digest, _now_iso_ms()))
    store.write(_do)
    # Broadcast tier: the active set changed (mint/retire) — drop the cached
    # board so the next poll rebuilds. If a bucket CNF backend is configured,
    # upload the immutable body once on mint (no-op for the db backend).
    board_cache_mod.invalidate_all()
    _cnf_put_on_mint(store, challenge_id, cnf_text)


def seed_audit_challenge(
    store: Store,
    *,
    challenge_id: str,
    tier: int,
    cnf_text: str,
    manifest: dict[str, Any] | None = None,
    decode_map: dict[str, Any] | None = None,
    source_path: str | None = None,
    status: str = "active",
    score_multiplier: float = 0.0,
) -> None:
    """Seed a structured audit-family CNF in shadow-safe mode by default.

    score_multiplier=0.0 means miners can solve it and we can harvest witnesses,
    but the challenge contributes zero to proportional weights until an operator
    explicitly raises the multiplier.
    """
    label = "audit_shadow" if score_multiplier <= 0.0 else "audit"
    seed_challenge(
        store,
        challenge_id=challenge_id,
        tier=tier,
        cnf_text=cnf_text,
        status=status,
        difficulty_label=label,
        score_multiplier=score_multiplier,
    )
    cnf_sha = sha256_hex(cnf_text)
    manifest_json = json.dumps(manifest or {}, sort_keys=True, separators=(",", ":"))
    decode_json = json.dumps(decode_map or {}, sort_keys=True, separators=(",", ":"))
    created_at = _now_iso_ms()

    def _do(conn):
        conn.execute(
            "INSERT OR REPLACE INTO audit_challenge_manifests"
            "(challenge_id, cnf_sha256, manifest_json, decode_map_json, "
            "source_path, created_at_iso) VALUES (?, ?, ?, ?, ?, ?)",
            (challenge_id, cnf_sha, manifest_json, decode_json, source_path, created_at))
    store.write(_do)
