"""Signed final-scores vector — the orchestrator's ONE number per miner.

This is the v4 scoring interface. The orchestrator composes whatever scoring
it wants (multiple challenge types, recency, arena payouts) into a single
per-hotkey weight, signs the vector, and serves it at
``GET /v1/validator/weights/next``. A validator's whole job is: verify the
signature, sanity-check, apply burn from the same signed payload, set weights.
No row pulling, no local averaging, no 7-day window — every scoring decision
lives HERE and can change without a validator release.

Wire shape is byte-compatible with the vector deployed validators already
verify (cathedral.policy.signing.SignedWeightVector): canonical bytes = drop
``signature``, sort keys, no whitespace, UTF-8; Ed25519 over that. Env knob
names match the live publisher so config carries over on the domain swap.

Score composition (the recency gate lives here, not in validator code):
  * window: only solves in the trailing CATHEDRAL_WEIGHTS_WINDOW_HOURS count
    (default 24h). A miner who stops solving drops out of the vector when the
    window passes — this replaces the validator-side 7-day mean whose frozen
    tail let idle miners coast for a week.
  * mode `flat_recent` (default): every hotkey with >=1 accepted solve in the
    window gets equal weight — byte-faithful to today's economics (flat 1.0
    rows) minus the stale tail.
  * mode `proportional`: weight = distinct challenges solved in the window,
    relative to the busiest solver. The dial to turn when we want volume to
    pay — flipped by env, no validator involvement.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import Store
# Shared verify surface lives in the dependency-light module so a validator
# install doesn't drag in FastAPI/store; re-exported here for the orchestrator's
# callers and the gates (one import surface).
from ..wire_vector import (  # noqa: F401
    MAX_VECTOR_ENTRIES,
    VectorError,
    canonical_bytes,
    invariant_check,
    verify_signature,
)

# Env knobs — SAME names as the live publisher (config carries over).
SIGNING_KEY_ENV = "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY"   # falls back to app key
KEY_ID_ENV = "CATHEDRAL_WEIGHT_POLICY_KEY_ID"
NETWORK_ENV = "CATHEDRAL_WEIGHT_POLICY_NETWORK"
NETUID_ENV = "CATHEDRAL_WEIGHT_POLICY_NETUID"
BURN_UID_ENV = "CATHEDRAL_WEIGHT_POLICY_BURN_UID"
BURN_PERCENTAGE_ENV = "CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE"
VALID_FOR_ENV = "CATHEDRAL_WEIGHT_POLICY_VALID_FOR_SECS"
# v4-only composition knobs.
WINDOW_HOURS_ENV = "CATHEDRAL_WEIGHTS_WINDOW_HOURS"
MODE_ENV = "CATHEDRAL_WEIGHTS_MODE"                       # flat_recent | proportional

_CACHE_TTL_SECS = 60.0
_vector_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def window_hours() -> float:
    return _env_float(WINDOW_HOURS_ENV, 24.0)


def mode() -> str:
    m = os.environ.get(MODE_ENV, "flat_recent").strip().lower()
    return m if m in ("flat_recent", "proportional") else "flat_recent"


def burn_percentage() -> float:
    return min(100.0, max(0.0, _env_float(BURN_PERCENTAGE_ENV, 85.0)))


def burn_uid() -> int | None:
    raw = os.environ.get(BURN_UID_ENV, "204").strip()
    return int(raw) if raw else None


def _ms_iso(dt: datetime) -> str:
    """ISO-8601 UTC, ms precision, trailing Z — the live vector convention."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"


# -- score composition --------------------------------------------------------

def compose_scores(store: Store, *, now: datetime | None = None) -> dict[str, float]:
    """One final number per hotkey, from solves inside the trailing window.

    This function is where multi-challenge scoring composes: community solves
    today; arena/champion payouts and future challenge types add their term
    here and the validator interface never changes.

    flat_recent reads the signed feed (eval_runs) rather than the local claim
    ledger: the feed carries seeded history from the previous orchestrator, so
    the vector is fully populated from the first second after a cutover —
    never a burn-only gap while the fresh store waits for new submits. (It
    also pays arena record-falls automatically, since those emit feed rows.)
    proportional needs per-challenge dedup, which only the claim ledger has;
    it falls back to flat until that ledger has in-window data.
    """
    now = now or datetime.now(timezone.utc)
    since = _ms_iso(now - timedelta(hours=window_hours()))
    feed = store.query(
        "SELECT DISTINCT miner_hotkey FROM eval_runs WHERE ran_at > ?", (since,))
    hotkeys = {str(r["miner_hotkey"]) for r in feed}
    if mode() == "proportional":
        rows = store.query(
            "SELECT miner_hotkey, COUNT(DISTINCT challenge_id) AS n "
            "FROM lane_challenge_solves WHERE solved_at_iso > ? GROUP BY miner_hotkey",
            (since,))
        counts = {str(r["miner_hotkey"]): int(r["n"]) for r in rows}
        if counts:
            top = max(counts.values())
            return {hk: round(n / top, 6) for hk, n in counts.items()}
    return {hk: 1.0 for hk in hotkeys}


# -- monotonic policy_version (validator rollback fence) -----------------------

def next_policy_version(store: Store) -> int:
    """Monotonic AND continuous with the live orchestrator: the deployed
    validators' rollback fences hold the live emitter's epoch-ms versions
    (~1.78e12), so a counter restarting at 1 would be rejected as a rollback
    by every fence. Epoch-ms keeps any successor emitter automatically ahead;
    max(stored+1, now_ms) keeps it strictly monotonic even within one ms."""
    now_ms = int(time.time() * 1000)

    def _bump(conn):
        row = conn.execute(
            "SELECT last_policy_version FROM weight_policy_state WHERE id = 1"
        ).fetchone()
        nxt = max((int(row[0]) if row else 0) + 1, now_ms)
        conn.execute(
            "INSERT OR REPLACE INTO weight_policy_state(id, last_policy_version, updated_at_iso) "
            "VALUES (1, ?, ?)", (nxt, _ms_iso(datetime.now(timezone.utc))))
        return nxt
    return store.write(_bump)


# -- sign -----------------------------------------------------------------------

def build_signed_vector(store: Store, *, signing_key_hex: str,
                        now: datetime | None = None) -> dict[str, Any]:
    """Compose scores, assemble the wire payload, sign. Returns the dict
    served verbatim by /v1/validator/weights/next."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    now = now or datetime.now(timezone.utc)
    scores = compose_scores(store, now=now)
    valid_for = _env_float(VALID_FOR_ENV, 1800.0)
    policy_inputs = {
        "mode": mode(), "window_hours": window_hours(),
        "burn": burn_percentage(), "burn_uid": burn_uid(),
        "hotkeys": sorted(scores), "scores": [scores[k] for k in sorted(scores)],
    }
    payload: dict[str, Any] = {
        "vector_id": str(uuid.uuid4()),
        "policy_version": next_policy_version(store),
        "network": os.environ.get(NETWORK_ENV, "finney"),
        "netuid": int(os.environ.get(NETUID_ENV, "39")),
        "generated_at": _ms_iso(now),
        "expires_at": _ms_iso(now + timedelta(seconds=valid_for)),
        "burn_snapshot": {
            "burn_uid": burn_uid(),
            "forced_burn_percentage": burn_percentage(),
        },
        "policy_hash": "sha256:" + hashlib.sha256(
            json.dumps(policy_inputs, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "key_id": os.environ.get(KEY_ID_ENV, "cathedral-weight-policy"),
        "policy_reason": f"v4_{mode()}_{window_hours():g}h_window",
        "policy_metadata": {"miner_count": len(scores), "composer": "scaffold.weights"},
        "weights": [
            {"miner_hotkey": hk, "weight": scores[hk]} for hk in sorted(scores)
        ],
    }
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_key_hex.strip()))
    payload["signature"] = base64.b64encode(sk.sign(canonical_bytes(payload))).decode()
    return payload


def current_vector(store: Store, *, signing_key_hex: str) -> dict[str, Any]:
    """Cached build — at most one compose+sign per _CACHE_TTL_SECS so the
    endpoint never adds load to the write path."""
    now = time.time()
    hit = _vector_cache.get("v")
    if hit is not None and (now - hit[0]) < _CACHE_TTL_SECS:
        return hit[1]
    vec = build_signed_vector(store, signing_key_hex=signing_key_hex)
    _vector_cache["v"] = (now, vec)
    return vec


def _reset_vector_cache() -> None:
    """Test hook."""
    _vector_cache.clear()
