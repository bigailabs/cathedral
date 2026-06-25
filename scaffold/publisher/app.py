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

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from ..contract import GenerateCtx
from ..lanes.solver_arena import SolverRegistry, SolverSpec
from . import board_cache as board_cache_mod
from . import keys, rows, scoring, top_cache as top_cache_mod, weights as weights_mod
from .auth import canonical_claim_bytes, default_verifier, sha256_hex
from .board_cache import BoardCache, board_cache_headers
from .cnf_store import CNFStore
from .sat_solution import verify_dimacs_solution
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


def _cnf_token_secret() -> bytes:
    raw = (
        os.environ.get(_CNF_TOKEN_SECRET_ENV, "").lstrip("\ufeff").strip()
        or os.environ.get("CATHEDRAL_PUBLISHER_SEED_SECRET", "").lstrip("\ufeff").strip()
    )
    if raw:
        return hashlib.sha256(raw.encode("utf-8")).digest()
    # Local/dev fallback. Production split roles must set a stable secret.
    print(f"[cnf] WARNING: {_CNF_TOKEN_SECRET_ENV} is unset; "
          "active-cnf tokens are process-local and unsafe for split replicas")
    return secrets.token_bytes(32)


def _public_cnf_url(path: str) -> str:
    base = os.environ.get(_CNF_PUBLIC_BASE_URL_ENV, "").lstrip("\ufeff").strip().rstrip("/")
    return f"{base}{path}" if base else path


class _SoftTtlCache:
    """Serve stale cached visibility payloads while one background refresh runs."""

    def __init__(self, name: str, ttl_secs: float) -> None:
        self.name = name
        self.ttl_secs = max(0.0, ttl_secs)
        self._lock = threading.Lock()
        self._entries: dict[Any, dict[str, Any]] = {}

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
                age = now - float(entry["built_at"])
                if self.ttl_secs <= 0 or age <= self.ttl_secs:
                    return entry["value"], "hit"
                if not entry.get("refreshing"):
                    entry["refreshing"] = True
                    threading.Thread(
                        target=self._refresh,
                        args=(key, builder),
                        name=f"{self.name}-refresh",
                        daemon=True,
                    ).start()
                return entry["value"], "stale"

            if cold_async:
                value = cold_value() if callable(cold_value) else cold_value
                self._entries[key] = {
                    "value": value,
                    "built_at": 0.0,
                    "refreshing": True,
                }
                threading.Thread(
                    target=self._refresh,
                    args=(key, builder),
                    name=f"{self.name}-cold-refresh",
                    daemon=True,
                ).start()
                return value, "warming"

        value = builder()
        with self._lock:
            self._entries[key] = {
                "value": value,
                "built_at": time.monotonic(),
                "refreshing": False,
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
                }
        except Exception as exc:
            print(f"[visibility_cache] refresh_failed name={self.name} key={key!r} error={exc!r}")
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None:
                    entry["refreshing"] = False


def _now_iso_ms() -> str:
    dt = datetime.now(timezone.utc)
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
        print(f"[runtime] invalid CATHEDRAL_SERVICE_ROLE={raw!r}; using all")
        return "all"
    return raw


def _role_runs_worker(role: str) -> bool:
    return role in {"all", "worker"}


def _role_runs_read_background(role: str) -> bool:
    return role in {"all", "read"}


def build_app(
    *,
    database_path: str = ":memory:",
    signing_key_hex: str | None = None,
    submit_min_interval_secs: int | None = None,
) -> FastAPI:
    key_hex = signing_key_hex or keys.load_signing_key()
    pub_hex = rows.public_key_hex(key_hex)
    jwks_doc = rows.jwks_from_key(key_hex)
    store = Store(database_path)
    service_role = _service_role_from_env()
    verifier = default_verifier()
    epoch_salt = f"epoch_{datetime.now(timezone.utc):%Y%m%d}:{_FAMILY}"
    arena_registry = SolverRegistry()
    # Shared HMAC secret lets active-cnf and CNF fetch land on different replicas.
    token_secret = _cnf_token_secret()
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
    if submit_hard_cap > 0 and submit_max_concurrency > 0:
        submit_max_concurrency = min(submit_max_concurrency, submit_hard_cap)
    submit_gate = (
        threading.BoundedSemaphore(submit_max_concurrency)
        if submit_max_concurrency > 0 else None
    )
    submit_log_events = os.environ.get("CATHEDRAL_SUBMIT_LOG_EVENTS", "").strip().lower() in {
        "1", "true", "yes", "on"}
    submit_metrics_lock = threading.Lock()
    submit_metrics: dict[str, Any] = {
        "started_at_iso": _now_iso_ms(),
        "max_concurrency": submit_max_concurrency,
        "configured_max_concurrency": configured_submit_max_concurrency,
        "hard_cap": submit_hard_cap,
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

    def _submit_metrics_snapshot() -> dict[str, Any]:
        with submit_metrics_lock:
            return {
                "started_at_iso": submit_metrics["started_at_iso"],
                "max_concurrency": submit_metrics["max_concurrency"],
                "configured_max_concurrency": submit_metrics["configured_max_concurrency"],
                "hard_cap": submit_metrics["hard_cap"],
                "min_interval_secs": submit_metrics["min_interval_secs"],
                "total": submit_metrics["total"],
                "by_outcome": dict(submit_metrics["by_outcome"]),
                "by_reason": dict(submit_metrics["by_reason"]),
                "by_kind": dict(submit_metrics["by_kind"]),
                "recent": list(submit_metrics["recent"]),
            }

    def _submit_slot():
        if submit_gate is None:
            yield
            return
        if not submit_gate.acquire(blocking=False):
            _record_submit_event(
                "rate_limited",
                "submit_busy_retry",
                status_code=429,
                log=True,
            )
            raise HTTPException(
                429,
                "submit_busy_retry",
                headers={
                    "Retry-After": "1",
                    "X-Cathedral-Rejection-Reason": "submit_busy_retry",
                },
            )
        try:
            yield
        finally:
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

    def _serve_board_snapshot(request: Request):
        payload, etag = board_cache.get()
        headers = board_cache_headers(etag)
        headers["X-Cathedral-Board-Rebuilds"] = str(board_cache.rebuild_count)
        inm = request.headers.get("if-none-match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            return Response(status_code=304, headers=headers)
        return JSONResponse(payload, headers=headers)

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

    class _HotPathBackpressureMiddleware:
        """Reject excess heavy requests before body parsing/threadpool work."""

        _SUBMIT_PATHS = {
            "/v1/agents/submit",
            f"{_LEGACY_PREFIX}/v1/agents/submit",
        }
        _PM_READ_PATHS = {
            "/v1/synthetic-boolean/per-miner/challenges",
            "/v1/synthetic-boolean/per-miner/cnf",
            f"{_LEGACY_PREFIX}/v1/synthetic-boolean/per-miner/challenges",
            f"{_LEGACY_PREFIX}/v1/synthetic-boolean/per-miner/cnf",
        }

        def __init__(self, asgi_app):
            self._app = asgi_app
            self._submit_gate = (
                threading.BoundedSemaphore(submit_max_concurrency)
                if submit_max_concurrency > 0 else None
            )
            pm_read_cap = _env_int("CATHEDRAL_PM_READ_HARD_CAP", submit_max_concurrency)
            self._pm_read_gate = (
                threading.BoundedSemaphore(pm_read_cap)
                if pm_read_cap > 0 else None
            )

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                await self._app(scope, receive, send)
                return
            method = scope.get("method")
            path = scope.get("path", "")
            gate = None
            reason = ""
            if method == "POST" and path in self._SUBMIT_PATHS:
                gate = self._submit_gate
                reason = "submit_busy_retry"
            elif method == "GET" and path in self._PM_READ_PATHS:
                gate = self._pm_read_gate
                reason = "per_miner_busy_retry"
            if gate is None:
                await self._app(scope, receive, send)
                return

            if not gate.acquire(blocking=False):
                _record_submit_event(
                    "rate_limited",
                    reason,
                    status_code=429,
                    log=True,
                )
                body = reason.encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                        (b"retry-after", b"1"),
                        (b"x-cathedral-rejection-reason", body),
                    ],
                })
                await send({
                    "type": "http.response.body",
                    "body": body,
                    "more_body": False,
                })
                return

            try:
                await self._app(scope, receive, send)
            finally:
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
            "/v1/synthetic-boolean/active-challenges",
            "/v1/synthetic-boolean/challenge-broadcast",
            "/v1/synthetic-boolean/current-challenge",
            "/v1/synthetic-boolean/per-miner/status",
            "/v1/synthetic-boolean/per-miner/summary",
            "/v1/validator/weights/next",
            "/v1/leaderboard/recent",
            "/v1/leaderboard/top",
            "/v1/leaderboard/explain",
        }
        _READ_GET_PREFIXES = {
            "/v1/audit-scanner/",
        }
        _SUBMIT_GET_PATHS = {
            "/v1/synthetic-boolean/active-cnf",
            "/v1/synthetic-boolean/per-miner/challenges",
            "/v1/synthetic-boolean/per-miner/cnf",
        }
        _SUBMIT_GET_PREFIXES = {
            "/v1/challenges/",
        }
        _SUBMIT_POST_PATHS = {
            "/v1/agents/submit",
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

    app.add_middleware(_StripLegacyPrefixMiddleware)
    app.add_middleware(_SlowRequestLogMiddleware)

    # Per-key sliding-window rate limiter — anti-flood backpressure for miner
    # endpoints.  Validators (/health, /v1/validator/weights/next) are exempt.
    # Default 120 req/min/key; set CATHEDRAL_RATELIMIT_RPM=0 to disable.
    # Also pure ASGI (no BaseHTTPMiddleware) for the same buffering reason.
    from .ratelimit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)
    # Add this last so it is the outermost ASGI layer: overloaded submit/PM-read
    # requests should get a cheap 429 before rate-limit state or route handling.
    app.add_middleware(_HotPathBackpressureMiddleware)
    # Role guard is now the true outer edge: role-mismatched traffic fails before
    # rate-limit state, request body parsing, or expensive route work.
    app.add_middleware(_ServiceRoleGuardMiddleware)

    app.state.store = store
    app.state.cnf_store = cnf_store
    app.state.board_cache = board_cache
    app.state.top_cache = top_cache
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
            "retention_task",
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
            vec = weights_mod.current_vector(store, signing_key_hex=weight_key)
        except Exception as exc:
            print(f"[leaderboard] weight context unavailable: {exc!r}")
            return {
                "generated_at": None,
                "policy_reason": None,
                "ranked": [],
                "by_hotkey": {},
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
        items = store.recent_rows(cur_ran_at, cur_id, limit)
        if items:
            last = items[-1]
            nxt_ran_at, nxt_id = last["ran_at"], last["id"]
        else:
            nxt_ran_at, nxt_id = cur_ran_at, cur_id
        weight_ctx = _current_weight_context()
        hotkeys = sorted({str(item.get("miner_hotkey") or "") for item in items if item.get("miner_hotkey")})
        return {
            "items": items,
            "view": "recent_signed_receipts",
            "rank_kind": "none",
            "explanation": (
                "Recent is an audit stream, not the earning leaderboard. "
                "Use current_weights or /v1/leaderboard/top?view=weights for current payment rank."
            ),
            "earning_weight_source": "v1/validator/weights/next",
            "earning_weights_generated_at": weight_ctx["generated_at"],
            "current_weights": _weight_annotations(weight_ctx, hotkeys),
            "current_weights_status": "available" if weight_ctx["generated_at"] else "unavailable",
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

    # ---- M1: feed ---------------------------------------------------------
    @app.get("/v1/leaderboard/recent")
    async def leaderboard_recent(
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
            payload, cache_status = recent_cache.get(
                int(limit),
                lambda: _recent_payload(None, None, limit),
                cold_async=visibility_cold_async,
                cold_value=lambda: _recent_warming_payload(limit),
            )
        else:
            payload = _recent_payload(cur_ran_at, cur_id, limit)
        return JSONResponse(
            payload,
            headers={
                "Cache-Control": "public, max-age=5",
                "Access-Control-Allow-Origin": "*",
                "X-Cathedral-Cache": cache_status,
            },
        )

    @app.get("/v1/leaderboard/top")
    async def leaderboard_top(
        window: str = Query("24h"),
        view: str = Query("weights"),
    ):
        """Fast pre-aggregated miner ranking. Cached ~45s in-process.
        Defaults to current earning weights. Use view=receipts for the old
        top 100 miners ranked by total weighted_score over the window.
        window=24h only for now (others fall back to 24h).
        """
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
        return JSONResponse(
            {
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
            },
            headers={"Cache-Control": "public, max-age=30", "Access-Control-Allow-Origin": "*"},
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
        weight_key = os.environ.get(weights_mod.SIGNING_KEY_ENV, "").strip() or key_hex
        try:
            vec = weights_mod.current_vector(store, signing_key_hex=weight_key)
        except Exception:
            import traceback
            print("[weights] vector build failed:\n" + traceback.format_exc())
            raise HTTPException(status_code=503, detail="no vector available")
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

    @app.get("/health/ready")
    async def health_ready():
        try:
            store.query("SELECT 1 AS ok")
            return _health_base("ready", "ok")
        except Exception as exc:
            payload = _health_base("ready", "error")
            payload["error"] = type(exc).__name__
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

    def _perminer_legacy_id_scan_enabled() -> bool:
        return os.environ.get(
            "CATHEDRAL_PERMINER_LEGACY_ID_SCAN", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}

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

    def _lookup_perminer_assignment(hotkey: str, epoch: int, challenge_id: str) -> tuple[int, int] | None:
        found = store.query(
            "SELECT tier, seq FROM per_miner_assignments "
            "WHERE challenge_id=? AND miner_hotkey=? AND epoch=?",
            (challenge_id, hotkey, epoch))
        if found:
            return int(found[0]["tier"]), int(found[0]["seq"])
        return None

    def _resolve_perminer_tier_seq(
        pm,
        hotkey: str,
        epoch: int,
        challenge_id: str,
        tier: int | None,
        seq: int | None,
    ) -> tuple[int, int] | None:
        if tier is not None and seq is not None:
            if tier not in pm.TIERS:
                return None
            if seq < 0 or seq >= pm.allotment_for(tier):
                return None
            cid = pm.instance_id(hotkey, epoch, tier, seq)
            return (tier, seq) if cid == challenge_id else None
        found = _lookup_perminer_assignment(hotkey, epoch, challenge_id)
        if found is not None or not _perminer_legacy_id_scan_enabled():
            return found
        return pm.recover_tier_seq_for(hotkey, epoch, challenge_id)

    def _require_perminer_ready(pm) -> None:
        try:
            pm.require_seed_secret()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

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
            require_mapped=weights_mod.perminer_require_coldkey(),
        )
        if identity is None:
            raise HTTPException(403, "coldkey_mapping_required")
        return identity

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
        clauses = ["status != 'ranked'"]
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
        totals = store.query(
            "SELECT COUNT(DISTINCT challenge_id) AS unique_solves, "
            "COUNT(*) AS verified_solves, SUM(difficulty_weight) AS units, "
            "MAX(solved_at_iso) AS last_solved_at "
            "FROM per_miner_solves WHERE " + where,
            tuple(params),
        )
        tiers = store.query(
            "SELECT tier, COUNT(DISTINCT challenge_id) AS solves, "
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
        clauses = ["miner_hotkey=?"]
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
        require_mapped = weights_mod.perminer_require_coldkey()
        identity = weights_mod.scoring_identity_for_hotkey(
            store,
            miner_hotkey,
            require_mapped=require_mapped,
        )
        eligible = identity is not None
        return _perminer_contribution_for(
            miner_hotkey,
            assignment_identity=identity or miner_hotkey,
            eligible=eligible,
            ineligibility_reason=None if eligible else "coldkey_mapping_required",
            expose_assignment_identity=False,
            include_assignment_supply=False,
            include_attempts=False,
        )

    def _perminer_summary(limit: int) -> dict[str, Any]:
        from . import per_miner as pm

        enabled = pm.perminer_enabled()
        epoch = pm.current_epoch() if enabled else None
        since_24h = _since_24h_iso()
        rows_ = store.query(
            "SELECT miner_hotkey, COUNT(DISTINCT challenge_id) AS unique_solves, "
            "COUNT(*) AS verified_solves, SUM(difficulty_weight) AS units, "
            "MAX(solved_at_iso) AS last_solved_at "
            "FROM per_miner_solves WHERE solved_at_iso > ? AND verified=1 "
            "GROUP BY miner_hotkey ORDER BY units DESC, unique_solves DESC, miner_hotkey "
            "LIMIT ?",
            (since_24h, limit),
        )
        active_miners = store.query(
            "SELECT COUNT(DISTINCT miner_hotkey) AS n FROM per_miner_solves "
            "WHERE solved_at_iso > ? AND verified=1",
            (since_24h,),
        )
        assigned_miners = []
        assignment_accounting = (
            "listing" if _perminer_record_listing_assignments()
            else "cnf_fetch"
        )
        if epoch is not None and _perminer_record_listing_assignments():
            assigned_miners = store.query(
                "SELECT COUNT(DISTINCT miner_hotkey) AS n, COUNT(*) AS assignments "
                "FROM per_miner_assignments WHERE epoch=?",
                (epoch,),
            )
        return {
            "kind": "per_miner_summary",
            "enabled": enabled,
            "shadow": pm.perminer_shadow() if enabled else False,
            "current_epoch": epoch,
            "last_24h_since": since_24h,
            "scoring": {
                "mode": weights_mod.perminer_scoring_mode(),
                "bonus_multiplier": weights_mod.perminer_bonus_multiplier(),
                "history_floor": weights_mod.perminer_history_floor(),
                "coldkey_required": weights_mod.perminer_require_coldkey(),
            },
            "assignment_accounting": assignment_accounting,
            "current_epoch_assignment_miners": int(assigned_miners[0]["n"] or 0) if assigned_miners else 0,
            "current_epoch_assigned_challenges": int(assigned_miners[0]["assignments"] or 0) if assigned_miners else 0,
            "active_miners_24h": int(active_miners[0]["n"] or 0) if active_miners else 0,
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
        return JSONResponse(
            payload,
            headers={
                "Cache-Control": "public, max-age=10",
                "Access-Control-Allow-Origin": "*",
                "X-Cathedral-Cache": cache_status,
            },
        )

    @app.get("/v1/admin/synthetic-boolean/submit-metrics")
    def submit_metrics_admin(authorization: str | None = Header(None)):
        """Operator-only submit pressure and rejection telemetry."""
        _require_publisher_admin(authorization)
        return JSONResponse(
            _submit_metrics_snapshot(),
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v1/synthetic-boolean/per-miner/challenges")
    def per_miner_challenges(
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
        _slot: None = Depends(_submit_slot),
    ):
        if card_id != _FAMILY:
            raise HTTPException(400, f"only card_id={_FAMILY} accepted (see skill.md)")
        if not dimacs_solution or not challenge_id:
            raise HTTPException(400, "this publisher requires solve-on-submit "
                                     "(challenge_id + dimacs_solution); see skill.md")
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
            tier_seq = _lookup_perminer_assignment(assignment_identity, epoch, challenge_id)
            if tier_seq is None:
                check = None
                ok, reason = False, "assignment_required_fetch_challenges_first"
            else:
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
            ws = scoring.weighted_score_for(store, x_cathedral_hotkey) * score_multiplier
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
