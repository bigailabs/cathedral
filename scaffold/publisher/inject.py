"""Continuous, ADDITIVE challenge-injection lane (env-gated, default OFF).

Purpose
-------
Run a SECOND stream of challenges alongside the native refill loop so we can
measure, on the LIVE board, how unpredictable / harder instances affect solve
time and scores — WITHOUT risking board availability. The native refill loop is
untouched: if this lane is off or stalls, the board still fills normally. Best of
both worlds — native keeps the board full, this lane mixes in test puzzles.

How it stays additive + isolated
---------------------------------
* Distinct family_id (default 'gentest'). Native refill counts/retires only its
  own family (``synthetic_boolean_v1``), so injected challenges never eat native
  slots and native never retires them. This lane manages its own family.
* ``cnf_source='local'`` — injected challenges are served, HMAC-fetch-gated,
  witness-verified on submit, and scored EXACTLY like native ones: one signed
  solve per (challenge, hotkey), same dedup, same proportional scoring.
  NOTE: a solved injected challenge DOES pay real tier weight — there is no
  zero-value mode — so keep the per-tier target small.
* Identifiable: the challenge_id embeds the family label, e.g.
  ``sat-t2-random-3sat-gentest-<seed-hex>``. The tier still parses
  (``tier_from_challenge_id`` reads ``t{N}`` right after ``sat-t``), and every
  solve row carries the challenge_id, so measurement is a substring filter on
  ``-<family>-`` (see measure_inject.py).

What makes an injected puzzle different from a native one
---------------------------------------------------------
* SEED: native derives its seed from ``sha256(utc_hour:tier:seq)`` — recomputable
  offline, so the planted answer is predictable. This lane seeds from
  ``secrets.randbits(63)`` (OS entropy) — unpredictable, like the standalone
  generator. This is the headline variable under test.
* METHOD / SHAPE: configurable per tier (default = the native method/shape for an
  apples-to-apples *seed-only* comparison; override to test harder instances).

Default OFF (``CATHEDRAL_INJECT_ENABLED`` unset/false). When off this module does
nothing and the publisher is byte-identical to before.

Env knobs
---------
* ``CATHEDRAL_INJECT_ENABLED``         — master switch (default off)
* ``CATHEDRAL_INJECT_FAMILY``          — isolation family_id (default ``gentest``)
* ``CATHEDRAL_INJECT_TIERS``           — comma list of tiers (default ``1,2``)
* ``CATHEDRAL_INJECT_TARGET_T{N}``     — active injected challenges to hold (default 5)
* ``CATHEDRAL_INJECT_METHOD_T{N}``     — planting method (default = native method_for tier)
* ``CATHEDRAL_INJECT_NVARS_T{N}`` /
  ``CATHEDRAL_INJECT_NCLAUSES_T{N}``   — instance shape (default = native shape_for tier)
* ``CATHEDRAL_INJECT_INTERVAL_SECONDS``— loop period (default 60)

Retirement reuses the native age / distinct-solver thresholds
(``CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_*``) but scoped to the injected family.
"""
from __future__ import annotations

import asyncio
import os
import secrets

import hashlib
import re

from . import refill
from .store import Store

# --- config defaults --------------------------------------------------------
_DEFAULT_FAMILY = "gentest"
_DEFAULT_TARGET = 5             # small: bounds extra income + CPU
_DEFAULT_TIERS = (1, 2)
_DEFAULT_INTERVAL_SECONDS = 60

# The native lane's family — injected challenges must NEVER use it, or this lane's
# counting/retirement would collide with native refill on real emissions.
_NATIVE_FAMILY = refill._FAMILY
# Family ids become part of public challenge_ids; keep them to a safe slug.
_FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")


def inject_enabled() -> bool:
    return os.environ.get("CATHEDRAL_INJECT_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"}


def inject_family() -> str:
    return os.environ.get("CATHEDRAL_INJECT_FAMILY", "").strip() or _DEFAULT_FAMILY


def family_is_safe(family: str) -> tuple[bool, str]:
    """A family is usable only if it (a) is NOT the native family — else inject
    counting/retirement would collide with native refill on real emissions — and
    (b) is a safe slug, since it lands in public challenge_ids. Returns
    (ok, reason)."""
    if family == _NATIVE_FAMILY:
        return False, (f"refuses native family '{_NATIVE_FAMILY}' — would collide "
                       f"with native refill counting/retirement")
    if not _FAMILY_RE.match(family):
        return False, f"family '{family}' must match {_FAMILY_RE.pattern}"
    return True, ""


def inject_tiers() -> list[int]:
    raw = os.environ.get("CATHEDRAL_INJECT_TIERS", "").strip()
    if not raw:
        return list(_DEFAULT_TIERS)
    out = [int(tok) for tok in (t.strip() for t in raw.split(",")) if tok.isdigit()]
    return out or list(_DEFAULT_TIERS)


def inject_target(tier: int) -> int:
    return refill._env_int(f"CATHEDRAL_INJECT_TARGET_T{tier}", _DEFAULT_TARGET)


def inject_method(tier: int) -> str:
    """Planting method for an injected tier. Defaults to the native method for the
    tier (apples-to-apples; the only variable is the seed). Override per tier with
    ``CATHEDRAL_INJECT_METHOD_T{N}`` (e.g. force ``ajm`` on tier1)."""
    override = os.environ.get(f"CATHEDRAL_INJECT_METHOD_T{tier}", "").strip().lower()
    return override or refill.method_for(tier)


def inject_shape(tier: int) -> tuple[int, int]:
    """(n_vars, n_clauses) for an injected tier. Defaults to the native shape;
    override with ``CATHEDRAL_INJECT_NVARS_T{N}`` / ``_NCLAUSES_T{N}`` to make
    injected instances harder/easier than native for the experiment."""
    base_n, base_m = refill.shape_for(tier)
    n = refill._env_int(f"CATHEDRAL_INJECT_NVARS_T{tier}", base_n)
    m = refill._env_int(f"CATHEDRAL_INJECT_NCLAUSES_T{tier}", base_m)
    return n, m


def inject_interval_seconds() -> int:
    return refill._env_int("CATHEDRAL_INJECT_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS)


# --- id + counting ----------------------------------------------------------
def _inject_seed() -> int:
    """Unpredictable 63-bit seed (OS entropy) — the whole point of the lane.
    Contrast refill.mint_seed, which is sha256(utc_hour:tier:seq) and therefore
    recomputable offline."""
    return secrets.randbits(63)


def inject_cid(tier: int, family: str, seed: int) -> str:
    """Opaque, unique challenge id that does NOT reveal the seed.

    The suffix is a one-way hash of (tier, family, seed), so a participant
    CANNOT invert the public id back to the seed and reconstruct the planted
    assignment — they must actually solve. Keeps the ``sat-t{N}-`` prefix so
    tier_from_challenge_id parses the tier, and the ``{family}`` label so solves
    stay filterable for measurement.

    SECURITY: an earlier version used ``{seed:016x}`` directly, which let anyone
    read the seed off the public board, regenerate ``random.Random(seed)``, and
    recover the planted model with no solving. The seed is never published and
    never stored — the served CNF body is the only artifact, and it cannot be
    reproduced from any public field. See inject_verify.py §SEED-SECRECY."""
    suffix = hashlib.sha256(f"{tier}:{family}:{seed}".encode()).hexdigest()[:16]
    return f"sat-t{tier}-random-3sat-{family}-{suffix}"


def active_inject_count(store: Store, tier: int, family: str) -> int:
    rows = store.query(
        "SELECT COUNT(*) AS n FROM lane_challenges "
        "WHERE family_id=? AND tier=? AND status='active' AND cnf_source='local'",
        (family, tier))
    return rows[0]["n"]


def retire_inject_ready(store: Store, tier: int, family: str) -> int:
    """Retire injected challenges that are old enough or saturated. Mirrors
    refill.retire_ready but scoped to the injected family — it never touches the
    native family. Same age / distinct-solver thresholds as native."""
    now = refill._now_iso()
    age_cutoff = refill._iso_before(refill.retire_after_seconds())
    retired = 0

    def _age(conn):
        cur = conn.execute(
            "UPDATE lane_challenges SET status='retired', cnf_text='', updated_at_iso=? "
            "WHERE family_id=? AND tier=? AND status='active' AND cnf_source='local' "
            "AND created_at_iso <= ?",
            (now, family, tier, age_cutoff))
        return int(cur.rowcount or 0)
    retired += store.write(_age)

    threshold = refill.retire_after_distinct_solvers()

    def _solved(conn):
        cur = conn.execute(
            "UPDATE lane_challenges SET status='retired', cnf_text='', updated_at_iso=? "
            "WHERE family_id=? AND tier=? AND status='active' AND cnf_source='local' "
            "AND challenge_id IN ("
            "  SELECT challenge_id FROM lane_challenge_solves "
            "  GROUP BY challenge_id HAVING COUNT(DISTINCT miner_hotkey) >= ?)",
            (now, family, tier, threshold))
        return int(cur.rowcount or 0)
    retired += store.write(_solved)

    if retired:
        from . import board_cache as _bc
        _bc.invalidate_all()
    return retired


def _commit_injected(store: Store, cid: str, tier: int, family: str, cnf_text: str) -> None:
    """Write one injected challenge via the shared seed_challenge path, tagged
    with the injected family. Stamps updated_at_iso=created_at_iso to match the
    native mint shape."""
    from .app import seed_challenge
    seed_challenge(store, challenge_id=cid, tier=tier, cnf_text=cnf_text,
                   status="active", family_id=family,
                   difficulty_label=f"inject:{family}")

    def _stamp(conn, cid=cid):
        conn.execute(
            "UPDATE lane_challenges SET updated_at_iso=created_at_iso WHERE challenge_id=?",
            (cid,))
    store.write(_stamp)


# --- the loop ---------------------------------------------------------------
async def inject_tier_async(store: Store, tier: int, family: str,
                            log=lambda *a, **k: None) -> dict:
    """One inject pass for a tier: retire ready, then top up to target with
    OS-entropy-seeded mints. Gen runs off the event loop (refill._gen_cnf forks a
    nice(19) child that releases the GIL via Pipe.poll), one mint at a time."""
    retired = await asyncio.to_thread(retire_inject_ready, store, tier, family)
    target = inject_target(tier)
    n_vars, n_clauses = inject_shape(tier)
    method = inject_method(tier)

    minted = 0
    guard = 0
    while (await asyncio.to_thread(active_inject_count, store, tier, family) < target
           and guard < target * 4 + 8):
        guard += 1
        seed = _inject_seed()
        cid = inject_cid(tier, family, seed)
        # collision check off the event loop (postgres getconn() blocks)
        exists = await asyncio.to_thread(
            store.query, "SELECT status FROM lane_challenges WHERE challenge_id=?", (cid,))
        if exists:
            continue  # astronomically unlikely id collision — skip
        cnf_text = await asyncio.to_thread(refill._gen_cnf, seed, n_vars, n_clauses, method)
        await asyncio.to_thread(_commit_injected, store, cid, tier, family, cnf_text)
        minted += 1
        # NOTE: never log the seed — it is the secret that keeps the planted
        # answer unrecoverable. The opaque cid is enough to identify the mint.
        log("inject_mint", tier=tier, cid=cid[:40], method=method,
            shape=(n_vars, n_clauses))
        await asyncio.sleep(0)  # yield between mints

    active = await asyncio.to_thread(active_inject_count, store, tier, family)
    return {"tier": tier, "family": family, "retired": retired, "minted": minted,
            "active": active, "target": target, "method": method,
            "shape": (n_vars, n_clauses)}


async def inject_once_async(store: Store, *, log=lambda *a, **k: None) -> list[dict]:
    """One full inject+retire pass across configured tiers. No-op (returns []) if
    the configured family is unsafe — fail closed rather than touch native."""
    family = inject_family()
    ok, why = family_is_safe(family)
    if not ok:
        log("inject_disabled", reason=why)
        return []
    return [await inject_tier_async(store, tier, family, log) for tier in inject_tiers()]


async def inject_loop(store: Store, *, interval_seconds: int | None = None,
                      log=lambda *a, **k: None, stop_event: asyncio.Event | None = None) -> None:
    """Asyncio task: periodic additive inject+retire. Mirrors refill.refill_loop.
    Never blocks the event loop (gen forks; DB calls run via to_thread)."""
    interval = interval_seconds or inject_interval_seconds()
    family = inject_family()
    log("inject_loop_start", interval=interval, family=family, tiers=inject_tiers(),
        targets={t: inject_target(t) for t in inject_tiers()})
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                summary = await inject_once_async(store, log=log)
                log("inject_pass", summary=summary)
            except Exception as e:  # never let a transient error kill the loop
                log("inject_error", error=str(e))
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(interval),
                    timeout=interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        log("inject_loop_cancelled")
        raise
