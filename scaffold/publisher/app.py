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
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Form, Header, HTTPException, Query, Request, Response
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
_SKEW_SECS = 300
# Public, non-scored readiness probe: a tiny satisfiable toy CNF miners fetch to
# self-test their solve pipeline before mining. Byte-identical to the monolith's
# tier-1 toy instance so any client that pinned its sha still passes.
_READINESS_CNF = "p cnf 3 3\n1 -2 3 0\n-1 2 3 0\n1 2 -3 0\n"
_READINESS_SHA = hashlib.sha256(_READINESS_CNF.encode("utf-8")).hexdigest()
_QUARANTINE_ROUNDS = 3      # Lane I (V4-DESIGN.md)
_MIN_BATCH_SCORE = 0.5      # Lane I (V4-DESIGN.md)
_CNF_TOKEN_TTL = 120        # active-cnf fetch token lifetime (seconds)


def _now_iso_ms() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


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
    verifier = default_verifier()
    epoch_salt = f"epoch_{datetime.now(timezone.utc):%Y%m%d}:{_FAMILY}"
    arena_registry = SolverRegistry()
    # secret for HMAC CNF fetch tokens — fresh per process (tokens are short-lived)
    token_secret = secrets.token_bytes(32)
    min_interval = (
        submit_min_interval_secs
        if submit_min_interval_secs is not None
        else int(os.environ.get("CATHEDRAL_SUBMIT_MIN_INTERVAL_SECS", "0"))
    )
    # in-process per-(hotkey, challenge) last-submit clock for rate limiting
    last_submit: dict[tuple[str, str], float] = {}

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

    def _build_board() -> dict[str, Any]:
        items = [_challenge_public(r) for r in _active_challenges()]
        return {"family_id": _FAMILY, "count": len(items), "items": items}

    board_cache = BoardCache(_build_board)
    board_cache_mod.register(board_cache)

    top_cache = top_cache_mod.TopCache()
    top_cache.start(store)
    top_cache_mod.register(top_cache)

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

    app.add_middleware(_StripLegacyPrefixMiddleware)

    # Per-key sliding-window rate limiter — anti-flood backpressure for miner
    # endpoints.  Validators (/health, /v1/validator/weights/next) are exempt.
    # Default 120 req/min/key; set CATHEDRAL_RATELIMIT_RPM=0 to disable.
    # Also pure ASGI (no BaseHTTPMiddleware) for the same buffering reason.
    from .ratelimit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)

    app.state.store = store
    app.state.cnf_store = cnf_store
    app.state.board_cache = board_cache
    app.state.top_cache = top_cache
    app.state.public_key_hex = pub_hex
    app.state.signing_key_hex = key_hex
    app.state.refill_task = None
    app.state.seed_task = None
    app.state.arena_eval_task = None
    app.state.arena_payout_task = None
    # Lane S champion machine + registry persist across eval ticks on app.state
    # (the validator constructs ONE lane and reuses it so the champion survives).
    from ..lanes.solver_arena import SolverArenaLane
    app.state.arena_lane = SolverArenaLane(registry=arena_registry)

    # ---- G2: challenge refill loop (env-gated) ----------------------------
    @app.on_event("startup")
    async def _start_refill():
        from . import refill
        if refill.refill_enabled():
            import asyncio
            loop_log = lambda evt, **kw: print(f"[refill] {evt} {kw}")  # noqa: E731
            app.state.refill_task = asyncio.create_task(
                refill.refill_loop(store, log=loop_log))

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
        import asyncio
        salt = f"epoch_{datetime.now(timezone.utc):%Y%m%d}:{_FAMILY}"

        def _prod_adapter_for(spec):
            # No real container runner is wired into the thin publisher yet; a
            # solver stays pending until one is. Flagged for Fred — turning the
            # loop on without a runner is a no-op, never a crash or false payout.
            return None

        app.state.arena_eval_task = asyncio.create_task(
            arena_eval.arena_eval_loop(
                store, app.state.arena_lane,
                adapter_for=_prod_adapter_for, private_key_hex=key_hex,
                epoch_salt=salt, log=lambda evt, **kw: print(f"[arena] {evt} {kw}")))

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
        import asyncio
        salt = f"epoch_{datetime.now(timezone.utc):%Y%m%d}:{_FAMILY}"

        app.state.arena_payout_task = asyncio.create_task(
            arena_payout.arena_payout_loop(
                store, app.state.arena_lane,
                round_source=lambda: int(os.environ.get("CATHEDRAL_ARENA_ROUND", "0")),
                champion_provider=lambda: None,   # no real runner wired yet
                closers_provider=lambda: [],
                private_key_hex=key_hex, epoch_salt=salt,
                log=lambda evt, **kw: print(f"[lane-i] {evt} {kw}")))

    # ---- G1b: self-seed from the live feed (env-gated) --------------------
    # Runs the backfill INSIDE the app process — survives as long as the
    # container does, checkpoints the watermark per page, and resumes after any
    # redeploy. No external ssh/cron. Gated by CATHEDRAL_SEED_ON_BOOT so it
    # only runs on the staging service during cutover prep.
    @app.on_event("startup")
    async def _start_seed():
        if os.environ.get("CATHEDRAL_SEED_ON_BOOT", "").lower() not in ("1", "true", "yes"):
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

        app.state.seed_task = asyncio.create_task(_seed_runner())

    @app.on_event("shutdown")
    async def _stop_refill():
        for attr in ("refill_task", "seed_task", "arena_eval_task", "arena_payout_task"):
            task = getattr(app.state, attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
        if hasattr(app.state, "top_cache"):
            app.state.top_cache.stop()

    # ---- helpers ----------------------------------------------------------
    def _challenge_public(r: Any) -> dict[str, Any]:
        cid = r["challenge_id"]
        return {
            "family_id": r["family_id"],
            "challenge_id": cid,
            "status": r["status"],
            "tier": r["tier"],
            "difficulty_label": r["difficulty_label"],
            "score_multiplier": r["score_multiplier"],
            "kind": "random_3sat",
            "storage": "sqlite_text",
            "cnf_sha256": r["cnf_sha256"],
            "cnf_bytes": r["cnf_bytes"],
            "num_vars": r["num_vars"],
            "num_clauses": r["num_clauses"],
            "announced_time_limit_secs": 604800,
            "solve_on_submit_enabled": True,
            "win_rule": "First submitted valid SAT receipt wins.",
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
    ) -> str:
        """Verify an sr25519 claim or raise HTTPException; return the timestamp
        that actually verified.

        Backend-compat: miners may have signed the timestamp they put in the
        `submitted_at` form field OR the `X-Cathedral-Submitted-At` header, and
        either the 6-field solve shape or the legacy 4-field shape (depending on
        the client/era). The monolith tolerated this spread; we try the same
        candidate (timestamp x shape) combinations and accept the first that
        verifies, so no miner needs to re-sign.
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
                # solution-bound 6-field (skill.md submit recipe)
                dict(challenge_id=challenge_id, dimacs_solution_sha256=dimacs_solution_sha256),
                # empty-string 6-field — the shape clients reuse from the
                # active-cnf/registration signing path (challenge_id/solution "")
                dict(challenge_id="", dimacs_solution_sha256=""),
                # legacy 4-field (no challenge/solution keys)
                dict(challenge_id=None, dimacs_solution_sha256=None),
            ]
            for shape in shapes:
                msg = canonical_claim_bytes(
                    bundle_hash=_empty_bundle_hash(), card_id=_FAMILY,
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

    # ---- M1: feed ---------------------------------------------------------
    @app.get("/v1/leaderboard/recent")
    def leaderboard_recent(
        since: str | None = Query(None),
        since_ran_at: str | None = Query(None),
        since_id: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ):
        # legacy ?since= compat: a single watermark seeds the ran_at cursor.
        cur_ran_at = since_ran_at or since
        cur_id = since_id
        items = store.recent_rows(cur_ran_at, cur_id, limit)
        if items:
            last = items[-1]
            nxt_ran_at, nxt_id = last["ran_at"], last["id"]
        else:
            nxt_ran_at, nxt_id = cur_ran_at, cur_id
        return JSONResponse(
            {
                "items": items,
                "next_since": nxt_ran_at,
                "next_since_ran_at": nxt_ran_at,
                "next_since_id": nxt_id,
                "merkle_epoch_latest": None,
            },
            headers={"Cache-Control": "public, max-age=5", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v1/leaderboard/top")
    def leaderboard_top(window: str = Query("24h")):
        """Fast pre-aggregated miner ranking. Cached ~45s in-process.
        Returns top 100 miners ranked by total weighted_score over the window.
        window=24h only for now (others fall back to 24h).
        """
        rows_, built_at, window_h = top_cache.get()
        return JSONResponse(
            {
                "miners": rows_,
                "window_hours": window_h,
                "built_at": built_at,
                "count": len(rows_),
                "cache_ttl_secs": 45,
            },
            headers={"Cache-Control": "public, max-age=30", "Access-Control-Allow-Origin": "*"},
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

    @app.get("/health")
    def health():
        return {"status": "ok", "db": "ok", "hippius": "ok", "polaris": "ok",
                "signing_key": "loaded", "sr25519_backend": getattr(verifier, "backend", "bittensor")}

    # ---- M2: Lane A read --------------------------------------------------
    @app.get("/v1/synthetic-boolean/active-challenges")
    def active_challenges_list(request: Request):
        # Broadcast tier: serve the cached board snapshot (rebuilt only on
        # mint/retire + TTL), with ETag/Cache-Control so an edge/CDN caches it
        # and a conditional GET short-circuits to 304 — reads don't touch the DB.
        payload, etag = board_cache.get()
        headers = board_cache_headers(etag)
        inm = request.headers.get("if-none-match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            return Response(status_code=304, headers=headers)
        return JSONResponse(payload, headers=headers)

    @app.get("/v1/synthetic-boolean/current-challenge")
    def current_challenge(tier: int | None = Query(None), difficulty: str | None = Query(None)):
        if tier is not None and tier < 0:
            raise HTTPException(400, "tier must be >= 0")
        actives = _active_challenges(tier)
        if difficulty is not None:
            labeled = [r for r in actives if r["difficulty_label"] == difficulty]
            if labeled:
                actives = labeled
        if not actives:
            raise HTTPException(404, "no_active_challenge")
        return _challenge_public(actives[0])

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
            "cnf_url": f"/v1/challenges/{cid}/cnf?t={token}",
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

    # ---- M2b: Per-miner challenge endpoints (CATHEDRAL_PERMINER_ENABLED) ----
    # These endpoints are completely new — no existing miner client calls them.
    # Flag-off: both routes return 404 immediately, zero change to existing paths.
    # Flag-on: miners can opt in by calling these instead of active-challenges.
    #
    # Authentication: same X-Cathedral-Hotkey + X-Cathedral-Signature + Submitted-At
    # pattern as active-cnf (the 6-field claim shape, challenge_id="", solution="").
    # The hotkey IS the identity of the challenge set — no hotkey, no set.

    @app.get("/v1/synthetic-boolean/per-miner/challenges")
    def per_miner_challenges(
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
        if x_cathedral_submitted_at is None:
            raise HTTPException(401, "missing X-Cathedral-Submitted-At")
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, x_cathedral_submitted_at,
            challenge_id="", dimacs_solution_sha256="",
        )
        epoch = pm.current_epoch()
        items = pm.miner_instance_set(x_cathedral_hotkey, epoch)
        return {
            "family_id": _FAMILY,
            "kind": "per_miner",
            "epoch": epoch,
            "miner_hotkey": x_cathedral_hotkey,
            "count": len(items),
            "items": items,
            "submit_path": "/api/cathedral/v1/agents/submit",
        }

    @app.get("/v1/synthetic-boolean/per-miner/cnf")
    def per_miner_cnf(
        challenge_id: str = Query(...),
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
        if x_cathedral_submitted_at is None:
            raise HTTPException(401, "missing X-Cathedral-Submitted-At")
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, x_cathedral_submitted_at,
            challenge_id="", dimacs_solution_sha256="",
        )
        epoch = pm.current_epoch()
        # Scan the miner's allotment to find the matching challenge.
        for tier in pm.TIERS:
            for seq in range(pm.allotment_for(tier)):
                cid = pm.instance_id(x_cathedral_hotkey, epoch, tier, seq)
                if cid == challenge_id:
                    _, cnf_text, _ = pm.generate_instance(x_cathedral_hotkey, epoch, tier, seq)
                    return PlainTextResponse(cnf_text, media_type="text/plain; charset=utf-8",
                                            headers={"X-Perminer-Challenge-Id": cid,
                                                     "X-Perminer-Tier": str(tier),
                                                     "X-Perminer-Epoch": str(epoch)})
        raise HTTPException(404, "challenge_not_in_miner_set")

    # ---- M2: Lane A submit (solve-on-submit) ------------------------------
    @app.post("/v1/agents/submit")
    async def agents_submit(
        request: Request,
        card_id: str = Form(...),
        display_name: str = Form(""),
        submitted_at: str = Form(None),
        challenge_id: str = Form(None),
        dimacs_solution: str = Form(None),
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(default="", alias="X-Cathedral-Submitted-At"),
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
        if min_interval > 0:
            prev = last_submit.get(rl_key)
            if prev is not None and (now - prev) < min_interval:
                raise HTTPException(429, "rate_limited")

        sol_sha = sha256_hex(dimacs_solution)
        submitted_at = _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, submitted_at,
            challenge_id=challenge_id, dimacs_solution_sha256=sol_sha,
            alt_submitted_at=x_cathedral_submitted_at or None,
        )

        # ---- Per-miner submit path (CATHEDRAL_PERMINER_ENABLED) ----
        # Detected by challenge_id prefix "pm-". Flag-off: this block is never
        # entered even if a miner sends a pm- id (it won't exist in lane_challenges
        # and will 409 — a safe hard stop, not silent misbehaviour).
        from . import per_miner as pm
        if challenge_id.startswith("pm-") and pm.perminer_enabled():
            last_submit[rl_key] = now
            if len(last_submit) > 50_000:
                horizon = now - max(min_interval, 3600.0)
                for k in [k for k, t in last_submit.items() if t < horizon]:
                    last_submit.pop(k, None)

            epoch = pm.current_epoch()
            # Parse assignment from dimacs_solution (space-separated signed ints).
            try:
                assignment = [int(x) for x in dimacs_solution.split() if x and x != "0"]
            except ValueError:
                raise HTTPException(400, "malformed_dimacs_solution")

            ok, reason = pm.verify_miner_submission(
                x_cathedral_hotkey, epoch, challenge_id, assignment)
            sub_id = new_uuid()

            if not ok:
                def _pm_rej(conn):
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO submit_signatures(signature, seen_at) VALUES (?, ?)",
                        (x_cathedral_signature, _now_iso_ms()))
                    if not cur.rowcount:
                        return False
                    conn.execute(
                        "INSERT INTO agent_submissions(id, miner_hotkey, sat_challenge_id, "
                        "status, rejection_reason, current_score, seq_no, submitted_at, signature) "
                        "VALUES (?, ?, ?, 'rejected', ?, 0.0, 1, ?, ?)",
                        (sub_id, x_cathedral_hotkey, challenge_id, reason,
                         submitted_at, x_cathedral_signature))
                    return True
                if not store.write(_pm_rej):
                    raise HTTPException(409, "replayed_signature")
                raise HTTPException(400, {"detail": reason, "challenge_id": challenge_id})

            # Accepted: find tier/seq to record the solve.
            tier_seq = pm.recover_tier_seq_for(x_cathedral_hotkey, epoch, challenge_id)
            tier = tier_seq[0] if tier_seq else 1
            seq = tier_seq[1] if tier_seq else 0
            pm_weight = pm.weight_for(tier)
            now_iso = _now_iso_ms()
            row_uuid = new_uuid()
            answer_hash = sha256_hex(",".join(str(x) for x in assignment))
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
                raise HTTPException(409, "replayed_signature")
            if err == "already_solved":
                raise HTTPException(409, "already_solved")
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
            raise HTTPException(409, "challenge_not_active")
        chal = rows_[0]
        if chal["status"] != "active":
            raise HTTPException(409, "challenge_already_locked")

        last_submit[rl_key] = now  # consume the slot only past the gates
        # bound the rate-limit map (long-lived single process): prune stale slots.
        if len(last_submit) > 50_000:
            horizon = now - max(min_interval, 3600.0)
            for k in [k for k, t in last_submit.items() if t < horizon]:
                last_submit.pop(k, None)

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
                raise HTTPException(409, "replayed_signature")
            raise HTTPException(400, {"detail": check.rejection_reason, "challenge_id": challenge_id})

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
            ws = scoring.weighted_score_for(store, x_cathedral_hotkey)
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
            raise HTTPException(409, "replayed_signature")
        if err == "challenge_already_locked":
            raise HTTPException(409, "challenge_already_locked")
        if err == "already_solved":
            raise HTTPException(409, "already_solved")
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
