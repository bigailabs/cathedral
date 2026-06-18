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
import threading
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
# Difficulty-weighted scoring: tier2 multiplier (default 3.0).
# Set to "1.0" to make both tiers score equally (byte-identical to pre-AJM).
TIER2_MULT_ENV = "CATHEDRAL_WEIGHTS_TIER2_MULT"

_CACHE_TTL_SECS = 60.0
_vector_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# Serializes the cache-miss build so concurrent misses can't each call
# next_policy_version() and emit two different vectors with the same
# policy_version (the orchestrator is single-instance — a process lock suffices).
_build_lock = threading.Lock()
# Background refresh state.  A single daemon thread rebuilds the vector every
# _CACHE_TTL_SECS; all request handlers read from _vector_cache without ever
# blocking on the DB query.  _bg_started tracks whether the thread is running
# so we only ever spawn one.
_bg_started = False
_bg_lock = threading.Lock()


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

def tier_from_challenge_id(cid: str) -> int:
    """Parse sat-t{N}-... → tier N (int). Defaults to 1 on any parse failure."""
    try:
        # expected prefix: sat-t{N}-
        if cid.startswith("sat-t"):
            rest = cid[len("sat-t"):]
            return int(rest.split("-", 1)[0])
    except (ValueError, IndexError):
        pass
    return 1


def tier2_multiplier() -> float:
    """Weight multiplier applied to tier2 challenges relative to tier1.
    Default 3.0 — a tier2 solve counts 3× a tier1 solve in proportional mode.
    Set CATHEDRAL_WEIGHTS_TIER2_MULT=1.0 to disable (byte-identical to pre-AJM scoring)."""
    return _env_float(TIER2_MULT_ENV, 3.0)


def coldkey_collapse_enabled() -> bool:
    """Opt-in Sybil hardening. OFF by default so this is byte-identical to today
    until an operator flips it AND a hotkey->coldkey map is supplied."""
    return os.environ.get("CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE", "").strip().lower() in {
        "1", "true", "yes", "on"}


def _perminer_compose_scores(store: Store) -> dict[str, float] | None:
    """Per-miner scoring path. Returns scores when CATHEDRAL_PERMINER_ENABLED is on
    AND not in shadow-only mode. Returns None when flag is off (caller falls
    through to existing scoring — byte-identical to pre-flag behaviour).

    Shadow mode: flag is on + CATHEDRAL_PERMINER_SHADOW=1 → compute the vector,
    log it, but return None so the LIVE vector stays the current scoring. This
    lets us run shadow comparisons without touching the live board.
    """
    from . import per_miner as pm
    if not pm.perminer_enabled():
        return None  # flag off: zero change
    epoch = pm.current_epoch()
    scores = pm.compute_perminer_scores(store, epoch)
    if pm.perminer_shadow():
        # Shadow: log the vector for comparison but don't serve it.
        print(f"[per_miner] shadow_vector epoch={epoch} scores={scores}")
        return None  # fall through to live scoring
    return scores if scores else None


def _load_coldkey_map(store: Store) -> dict[str, str] | None:
    """hotkey->coldkey, refreshed out-of-band into the ``coldkey_map`` table by
    a small metagraph poller (the thin publisher has no chain access of its own).
    Returns None when the table is missing/empty so scoring stays per-hotkey
    (fail-open: a missing or partial map can never zero an honest miner)."""
    try:
        rows = store.query("SELECT hotkey, coldkey FROM coldkey_map")
    except Exception:
        return None
    m = {str(r["hotkey"]): str(r["coldkey"]) for r in rows}
    return m or None


def compose_scores(
    store: Store, *, now: datetime | None = None,
    coldkey_of: dict[str, str] | None = None,
) -> dict[str, float]:
    """One final number per hotkey, from solves inside the trailing window.

    This is where multi-challenge scoring composes: community solves today;
    arena/champion payouts and future challenge types add their term here and
    the validator interface never changes.

    IDENTITY-AWARE SCORING (the Sybil fix). When coldkey collapse is enabled AND
    a hotkey->coldkey map is supplied, a distinct challenge is credited ONCE PER
    COLDKEY -- the union of solves across all of that coldkey's hotkeys -- and
    the coldkey's score is then split across its solving hotkeys. So:
      * mirroring one solve onto k hotkeys adds NOTHING (same challenge_id, one
        entry in the coldkey's set) -> cloning earns zero extra;
      * solving MORE distinct challenges earns more, even across many hotkeys ->
        honest volume is fully rewarded, not punished.
    With no map (default) identity == hotkey, so this is byte-identical to the
    prior per-hotkey proportional scoring.

    flat_recent reads the signed feed (eval_runs) -- seeded history keeps the
    vector populated from the first second after a cutover. proportional needs
    the per-challenge claim ledger; it falls back to flat until that ledger has
    in-window data.
    """
    # Per-miner path (flag-gated). When the flag is off this is a no-op and
    # the rest of the function runs unchanged — byte-identical to pre-flag.
    pm_scores = _perminer_compose_scores(store)
    if pm_scores is not None:
        return pm_scores

    now = now or datetime.now(timezone.utc)
    since = _ms_iso(now - timedelta(hours=window_hours()))
    use_ck = coldkey_collapse_enabled() and bool(coldkey_of)

    def ident(hk: str) -> str:
        return coldkey_of.get(hk, hk) if use_ck else hk

    if mode() == "proportional":
        rows = store.query(
            "SELECT DISTINCT miner_hotkey, challenge_id FROM lane_challenge_solves "
            "WHERE solved_at_iso > ?", (since,))
        # identity -> weighted score (sum of per-challenge tier weights, deduped)
        scores_w: dict[str, float] = {}
        # identity -> set of distinct challenge_ids (for dedup)
        seen: dict[str, set] = {}
        hks: dict[str, set] = {}     # identity -> set of its solving hotkeys
        t2_mult = tier2_multiplier()
        for r in rows:
            hk = str(r["miner_hotkey"]); idk = ident(hk)
            cid = str(r["challenge_id"])
            if cid not in seen.get(idk, set()):
                seen.setdefault(idk, set()).add(cid)
                tier = tier_from_challenge_id(cid)
                weight = t2_mult if tier == 2 else 1.0
                scores_w[idk] = scores_w.get(idk, 0.0) + weight
            hks.setdefault(idk, set()).add(hk)
        if scores_w:
            top = max(scores_w.values())
            base: dict[str, float] = {}
            for idk, w in scores_w.items():
                per = round((w / top) / len(hks[idk]), 6)
                for hk in hks[idk]:
                    base[hk] = per
            return base
        # no in-window claim rows -> fall through to flat

    feed = store.query(
        "SELECT DISTINCT miner_hotkey FROM eval_runs WHERE ran_at > ?", (since,))
    hotkeys = {str(r["miner_hotkey"]) for r in feed}
    if not use_ck:
        return {hk: 1.0 for hk in hotkeys}
    # flat, identity-deduped: each coldkey's hotkeys share a single 1.0
    groups: dict[str, list[str]] = {}
    for hk in hotkeys:
        groups.setdefault(ident(hk), []).append(hk)
    out: dict[str, float] = {}
    for members in groups.values():
        per = round(1.0 / len(members), 6)
        for hk in members:
            out[hk] = per
    return out


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
    coldkey_of = _load_coldkey_map(store) if coldkey_collapse_enabled() else None
    scores = compose_scores(store, now=now, coldkey_of=coldkey_of)
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


def _bg_refresh_loop(store: Store, signing_key_hex: str) -> None:
    """Background daemon thread: rebuild the vector every _CACHE_TTL_SECS.

    Never raises — a transient DB error is logged and retried next cycle.
    Runs forever; the process exiting is the only exit condition (daemon=True).
    """
    while True:
        try:
            vec = build_signed_vector(store, signing_key_hex=signing_key_hex)
            with _build_lock:
                _vector_cache["v"] = (time.time(), vec)
        except Exception as exc:
            print(f"[weights] bg_refresh error (will retry): {exc!r}")
        time.sleep(_CACHE_TTL_SECS)


def _ensure_bg_started(store: Store, signing_key_hex: str) -> None:
    """Lazily start the background refresh thread (idempotent)."""
    global _bg_started
    if _bg_started:
        return
    with _bg_lock:
        if _bg_started:
            return
        t = threading.Thread(
            target=_bg_refresh_loop, args=(store, signing_key_hex),
            name="weights-bg-refresh", daemon=True,
        )
        t.start()
        _bg_started = True


def current_vector(store: Store, *, signing_key_hex: str) -> dict[str, Any]:
    """Serve the latest signed vector from the in-memory cache.

    The background refresh thread (started on first call) rebuilds the vector
    every _CACHE_TTL_SECS without ever blocking the request path.  Only the
    very first call (empty cache) waits for a build — after that every request
    returns in microseconds.

    IMPORTANT: the synchronous first-build path does NOT hold _build_lock
    during the DB query.  Holding the lock during a slow (5-30s) DB build
    would block every concurrent request handler that tries to read the cache,
    causing a cascading stall.  Instead we build outside the lock and acquire
    only briefly to write the result.  If two threads both hit an empty cache
    simultaneously, both build (at most twice at startup), and the first writer
    wins; the second's result is discarded.  This wastes one extra build at
    most once at startup and is far better than starving all callers.
    """
    # Ensure the background thread is running so the cache stays fresh.
    _ensure_bg_started(store, signing_key_hex)

    with _build_lock:
        hit = _vector_cache.get("v")
    if hit is not None:
        return hit[1]

    # First call with an empty cache: build synchronously WITHOUT holding the
    # lock (a slow build inside the lock starves every concurrent request).
    vec = build_signed_vector(store, signing_key_hex=signing_key_hex)
    with _build_lock:
        # Another thread may have written the cache while we built.
        # Prefer the existing entry (avoids a duplicate policy_version bump),
        # but if it is still empty write ours.
        existing = _vector_cache.get("v")
        if existing is None:
            _vector_cache["v"] = (time.time(), vec)
        else:
            vec = existing[1]
    return vec


def _reset_vector_cache() -> None:
    """Test hook."""
    global _bg_started
    _vector_cache.clear()
    _bg_started = False
