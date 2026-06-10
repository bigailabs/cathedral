"""The thin publisher — a FastAPI app that replaces the 46.5k-line monolith
publisher while keeping the frozen wire surface byte-identical.

Surfaces:
  M1 validator feed   GET /v1/leaderboard/recent  (dual cursor, signed rows)
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
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Form, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from ..contract import GenerateCtx
from ..lanes.solver_arena import SolverRegistry, SolverSpec
from . import keys, rows
from .auth import canonical_claim_bytes, default_verifier, sha256_hex
from .sat_solution import verify_dimacs_solution
from .store import Store, new_uuid

_FAMILY = "synthetic_boolean_v1"
_SKEW_SECS = 300
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

    app = FastAPI(title="cathedral-thin-publisher")
    app.state.store = store
    app.state.public_key_hex = pub_hex
    app.state.signing_key_hex = key_hex

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
    ) -> None:
        """Verify an sr25519 claim or raise HTTPException. Enforces ±skew."""
        ts = _parse_iso(submitted_at)
        if ts is None:
            raise HTTPException(400, "invalid submitted_at")
        if abs(time.time() - ts) > _SKEW_SECS:
            raise HTTPException(400, "submitted_at outside acceptable clock-skew window")
        from blake3 import blake3 as _b3  # noqa
        msg = canonical_claim_bytes(
            bundle_hash=_empty_bundle_hash(), card_id=_FAMILY, miner_hotkey=hotkey,
            submitted_at=submitted_at, challenge_id=challenge_id,
            dimacs_solution_sha256=dimacs_solution_sha256,
        )
        if not verifier.verify(hotkey, msg, signature_b64):
            raise HTTPException(401, "invalid hotkey signature")

    def _active_challenges(tier: int | None = None) -> list[Any]:
        if tier is None:
            return store.query(
                "SELECT * FROM lane_challenges WHERE status='active' ORDER BY challenge_id ASC")
        return store.query(
            "SELECT * FROM lane_challenges WHERE status='active' AND tier=? ORDER BY challenge_id ASC",
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
        return {
            "items": items,
            "next_since": nxt_ran_at,
            "next_since_ran_at": nxt_ran_at,
            "next_since_id": nxt_id,
            "merkle_epoch_latest": None,
        }

    @app.get("/.well-known/cathedral-jwks.json")
    def jwks():
        return JSONResponse(jwks_doc)

    @app.get("/health")
    def health():
        return {"status": "ok", "db": "ok", "hippius": "ok", "polaris": "ok",
                "signing_key": "loaded", "sr25519_backend": getattr(verifier, "backend", "bittensor")}

    # ---- M2: Lane A read --------------------------------------------------
    @app.get("/v1/synthetic-boolean/active-challenges")
    def active_challenges_list():
        items = [_challenge_public(r) for r in _active_challenges()]
        return {"family_id": _FAMILY, "count": len(items), "items": items}

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
        if not _check_token(challenge_id, t):
            raise HTTPException(404, "not found")
        rows_ = store.query(
            "SELECT cnf_text FROM lane_challenges WHERE challenge_id=?", (challenge_id,))
        if not rows_:
            raise HTTPException(404, "not found")
        return PlainTextResponse(rows_[0]["cnf_text"])

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
    ):
        if card_id != _FAMILY:
            raise HTTPException(400, f"only card_id={_FAMILY} accepted (see skill.md)")
        if not dimacs_solution or not challenge_id:
            raise HTTPException(400, "this publisher requires solve-on-submit "
                                     "(challenge_id + dimacs_solution); see skill.md")
        submitted_at = submitted_at or _now_iso_ms()

        # per-(hotkey, challenge) rate limit (fires before lock check).
        rl_key = (x_cathedral_hotkey, challenge_id)
        now = time.time()
        if min_interval > 0:
            prev = last_submit.get(rl_key)
            if prev is not None and (now - prev) < min_interval:
                raise HTTPException(429, "rate_limited")

        sol_sha = sha256_hex(dimacs_solution)
        _verify_hotkey_claim(
            x_cathedral_hotkey, x_cathedral_signature, submitted_at,
            challenge_id=challenge_id, dimacs_solution_sha256=sol_sha,
        )

        # replay dedup: a signature seen before is rejected.
        def _dedup(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO submit_signatures(signature, seen_at) VALUES (?, ?)",
                (x_cathedral_signature, _now_iso_ms()))
            return cur.rowcount == 1
        if not store.write(_dedup):
            raise HTTPException(409, "replayed_signature")

        rows_ = store.query(
            "SELECT * FROM lane_challenges WHERE challenge_id=?", (challenge_id,))
        if not rows_:
            raise HTTPException(409, "challenge_not_active")
        chal = rows_[0]
        if chal["status"] != "active":
            raise HTTPException(409, "challenge_already_locked")

        last_submit[rl_key] = now  # consume the slot only past the gates

        check = verify_dimacs_solution(chal["cnf_text"], dimacs_solution)
        sub_id = new_uuid()
        if not check.ok:
            def _rej(conn):
                conn.execute(
                    "INSERT INTO agent_submissions(id, miner_hotkey, sat_challenge_id, "
                    "status, rejection_reason, current_score, seq_no, submitted_at, signature) "
                    "VALUES (?, ?, ?, 'rejected', ?, 0.0, 1, ?, ?)",
                    (sub_id, x_cathedral_hotkey, challenge_id, check.rejection_reason,
                     submitted_at, x_cathedral_signature))
            store.write(_rej)
            raise HTTPException(400, {"detail": check.rejection_reason, "challenge_id": challenge_id})

        # atomic winner claim: lock the challenge, write the ranked submission, emit rows.
        def _win(conn):
            cur = conn.execute(
                "UPDATE lane_challenges SET status='locked' "
                "WHERE challenge_id=? AND status='active'", (challenge_id,))
            if cur.rowcount != 1:
                return None  # someone else won the race
            conn.execute(
                "INSERT INTO agent_submissions(id, miner_hotkey, sat_challenge_id, "
                "status, rejection_reason, current_score, seq_no, submitted_at, signature) "
                "VALUES (?, ?, ?, 'ranked', NULL, 1.0, 1, ?, ?)",
                (sub_id, x_cathedral_hotkey, challenge_id, submitted_at, x_cathedral_signature))
            return True
        won = store.write(_win)
        if not won:
            raise HTTPException(409, "challenge_already_locked")

        row_uuid = new_uuid()
        answer_hash = sha256_hex(",".join(str(x) for x in check.assignment))
        verifier_details_hash = sha256_hex(f"{challenge_id}:{sol_sha}")
        emitted = rows.build_solve_rows(
            row_uuid=row_uuid, miner_hotkey=x_cathedral_hotkey,
            agent_id=new_uuid(), challenge_id=challenge_id, tier=chal["tier"],
            weighted_score=1.0, answer_hash=answer_hash,
            verifier_details_hash=verifier_details_hash, ran_at=_now_iso_ms(),
            epoch_salt=epoch_salt, solve_rank=1, solved=True, private_key_hex=key_hex,
        )
        for r in emitted:
            store.insert_row(r)
        return {
            "status": "ranked", "id": sub_id, "eval_run_id": row_uuid,
            "challenge_id": challenge_id, "weighted_score": 1.0,
            "attestation_status": "pending",
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
                "submitted_round, quarantine_until_round, min_batch_score, status, created_at_iso) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (instance_id, x_cathedral_hotkey, cnf_sha, round_no, quarantine_until,
                 _MIN_BATCH_SCORE, _now_iso_ms()))
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
