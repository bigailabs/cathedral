"""G2 — Challenge refill + retirement loop for the thin publisher.

Keeps N active challenges per tier on the board, minting fresh planted-3SAT
instances and retiring stale/saturated ones — the live v6 open-window lifecycle
(see ~/code/cathedral/src/cathedral/publisher/sat_fill.py):

  * Refill: for each configured tier, while active count < target, mint a new
    challenge from scaffold.dimacs.gen_planted_3sat (kind=random_3sat). Targets
    default to the live board (25× tier1, 25× tier2).

  * Shape: tier1/tier2 use n=6000, m=25560 — IDENTICAL to live. gen_planted_3sat
    mints that size in ~2.0s (< the 5s budget; measured 2026-06-09), so there is
    NO divergence from live shape. (If a future host is slower, set
    CATHEDRAL_REFILL_NVARS/_NCLAUSES per tier to fall back to a smaller size; any
    such fallback is an explicit, logged divergence.)

  * Determinism: each minted challenge id + CNF is a pure function of
    (seed_input, tier, sequence). seed_input is a block-ish value (defaults to a
    UTC date bucket; a chain block hash can be injected). Re-running with the
    same inputs reproduces the same instances — no unseeded randomness.

  * Retirement (live v6 thresholds, COMPAT.md / sat_fill.py):
      - age-based:   active longer than RETIRE_AFTER_SECONDS (default 3600 = 1h)
      - solved-based: >= RETIRE_AFTER_DISTINCT_SOLVERS distinct solvers
        (default 64), counted from lane_challenge_solves.
    Locally-minted (cnf_source='local') challenges are the ones managed here;
    externally-mirrored seed challenges are left to the live publisher.

Runs as an asyncio task inside the publisher, gated by CATHEDRAL_REFILL_ENABLED.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import multiprocessing
import os
from datetime import datetime, timedelta, timezone

from ..dimacs import gen_planted_3sat
from .app import seed_challenge
from .store import Store

# ---------------------------------------------------------------------------
# GIL-free CNF generation via a subprocess pool
# ---------------------------------------------------------------------------
# gen_planted_3sat is pure-Python CPU-bound (~2s for tier1 6000/25560 biased).
# asyncio.to_thread puts the call in a worker thread, but the GIL is still
# held by that thread during computation. Python's GIL reacquisition bias
# means a CPU-hungry thread can re-grab the GIL immediately on release,
# starving the event loop (main thread) even though the nominal switch
# interval is 5ms. This freezes HTTP request processing for the duration
# of each gen call.
#
# Fix: run gen_planted_3sat in a subprocess (ProcessPoolExecutor with the
# 'spawn' context). A spawned child starts a fresh Python interpreter —
# no shared GIL, no inherited DB connections, no asyncio state. The parent
# thread blocks on Future.result() while the child does the CPU work; the
# parent thread itself holds no GIL during that wait (blocking IO/wait).
#
# 'spawn' is used over 'fork' to avoid:
#   - inheriting open psycopg2 connections (fork-unsafe, causes crashes)
#   - inheriting uvicorn signal handlers and asyncio state in the child
#
# Pool is created once (lazily) at first call and never recreated per call.
# max_workers=1: refill_loop runs one pass at a time; >1 workers would
# waste memory on a single Railway instance. If pool init fails, we fall
# back to the direct in-thread call — the GIL is held but minting still
# works (safe degradation).

_POOL: concurrent.futures.ProcessPoolExecutor | None = None
_POOL_BROKEN: bool = False
# Resolved once at import time: the app root directory that must be on
# sys.path in the spawned worker so it can import scaffold.dimacs.
# refill.py lives at <app_root>/scaffold/publisher/refill.py, so the
# app root is 3 directories up from this file.
_APP_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
# Per-call timeout for future.result(). Cap at 60s so a hung worker
# doesn't block the refill thread slot indefinitely.
_GEN_TIMEOUT: float = 60.0


def _worker_init(app_root: str) -> None:
    """Subprocess initializer: ensure app_root is on sys.path so the
    worker can import scaffold.dimacs. Called once per worker process.
    Runs in the child — safe to modify sys.path here."""
    import sys
    if app_root not in sys.path:
        sys.path.insert(0, app_root)


def _get_pool() -> concurrent.futures.ProcessPoolExecutor | None:
    """Return the module-level process pool, initialising it lazily with
    the 'spawn' context. Returns None on init failure (triggers fallback).

    'spawn' is used over 'fork' to avoid inheriting psycopg2 connections
    (fork-unsafe, causes child crashes) and uvicorn signal handlers.
    An initializer adds the app root to sys.path so the worker can import
    scaffold.dimacs without a full pip install in the child.
    """
    global _POOL, _POOL_BROKEN
    if _POOL_BROKEN:
        return None
    if _POOL is not None:
        return _POOL
    try:
        ctx = multiprocessing.get_context("spawn")
        _POOL = concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            mp_context=ctx,
            initializer=_worker_init,
            initargs=(_APP_ROOT,),
        )
        # Warm the pool: submit a trivial task so the worker process is
        # started now (and _worker_init has run) rather than on the first
        # real mint. Timeout avoids blocking startup forever if spawn fails.
        _POOL.submit(int, 0).result(timeout=30)
    except Exception:  # pragma: no cover — spawn unsupported or times out
        _POOL_BROKEN = True
        _POOL = None
        return None
    return _POOL


def _gen_cnf_in_process(seed: int, n_vars: int, n_clauses: int,
                         method: str) -> tuple[str, list[int]]:
    """Generate CNF in a subprocess, releasing the GIL in the calling thread.

    Called from a worker thread (refill_tier runs via asyncio.to_thread),
    so blocking on Future.result() is safe. The CPU work runs in a separate
    process — no GIL contention with the event loop or other threads.

    Falls back to direct in-thread call if the pool is unavailable or times
    out, so minting never breaks even if subprocess spawning fails.
    """
    pool = _get_pool()
    if pool is None:
        return gen_planted_3sat(seed, n_vars, n_clauses, method=method)
    try:
        future = pool.submit(gen_planted_3sat, seed, n_vars, n_clauses, method)
        return future.result(timeout=_GEN_TIMEOUT)
    except Exception:  # pragma: no cover — worker crash, timeout, pickle error
        return gen_planted_3sat(seed, n_vars, n_clauses, method=method)

_FAMILY = "synthetic_boolean_v1"

# Live shape (board.json / live active-challenges): tier1 unchanged.
# tier2 ships at a smaller AJM size; env-overridable for emergency revert.
_TIER_SHAPE: dict[int, tuple[int, int]] = {
    1: (6000, 25560),
    2: (400, 1704),   # AJM at m/n=4.26; calibrated 2026-06-17 (~1s minisat22)
}
# Planting method per tier: tier1 stays biased (unchanged); tier2 uses AJM.
_TIER_METHOD: dict[int, str] = {
    1: "biased",
    2: "ajm",
}
_DEFAULT_TARGETS: dict[int, int] = {1: 25, 2: 25}
_DEFAULT_RETIRE_AFTER_SECONDS = 60 * 60       # live default (sat_fill.py)
_DEFAULT_RETIRE_AFTER_DISTINCT_SOLVERS = 64   # live default (sat_fill.py)
_DEFAULT_INTERVAL_SECONDS = 60


def refill_enabled() -> bool:
    return os.environ.get("CATHEDRAL_REFILL_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def retire_after_seconds() -> int:
    return _env_int("CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_SECONDS",
                    _DEFAULT_RETIRE_AFTER_SECONDS)


def retire_after_distinct_solvers() -> int:
    return _env_int("CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_DISTINCT_SOLVERS",
                    _DEFAULT_RETIRE_AFTER_DISTINCT_SOLVERS)


def target_for(tier: int) -> int:
    return _env_int(f"CATHEDRAL_REFILL_TARGET_T{tier}", _DEFAULT_TARGETS.get(tier, 0))


def shape_for(tier: int) -> tuple[int, int]:
    """(n_vars, n_clauses) for a tier — live shape unless env-overridden (a
    documented divergence used only when gen is too slow on the host)."""
    base_n, base_m = _TIER_SHAPE.get(tier, (6000, 25560))
    n = _env_int(f"CATHEDRAL_REFILL_NVARS_T{tier}", base_n)
    m = _env_int(f"CATHEDRAL_REFILL_NCLAUSES_T{tier}", base_m)
    return n, m


def method_for(tier: int) -> str:
    """Planting method for a tier — env CATHEDRAL_REFILL_METHOD_T{N} overrides.
    Default: tier1='biased' (unchanged), tier2='ajm' (unbiased AJM planting).
    Setting CATHEDRAL_REFILL_METHOD_T2=biased reverts tier2 to biased planting."""
    default = _TIER_METHOD.get(tier, "biased")
    return os.environ.get(f"CATHEDRAL_REFILL_METHOD_T{tier}", default).strip().lower() or default


def default_seed_input() -> str:
    """Block-ish seed bucket. Defaults to the current UTC hour so a restart in
    the same hour reproduces the same mint sequence; a caller may inject a chain
    block hash instead for true block-determinism."""
    return datetime.now(timezone.utc).strftime("%Y%m%d%H")


def mint_seed(seed_input: str, tier: int, sequence: int) -> int:
    """Deterministic 63-bit integer seed from (seed_input, tier, sequence)."""
    h = hashlib.sha256(f"{seed_input}:{tier}:{sequence}".encode()).hexdigest()
    return int(h[:16], 16)


def mint_challenge_id(seed_input: str, tier: int, sequence: int) -> str:
    suffix = hashlib.sha256(f"{seed_input}:{tier}:{sequence}".encode()).hexdigest()[:16]
    return f"sat-t{tier}-random-3sat-{seed_input}-{suffix}"


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _iso_before(seconds: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def record_solve(store: Store, challenge_id: str, miner_hotkey: str) -> None:
    """DEPRECATED — the live submit path claims solves atomically via
    scoring.claim_solve (same table, same idempotency) inside the submit
    transaction. Kept only for ad-hoc tooling; do NOT wire into submit or
    solves would double-insert."""
    def _do(conn):
        conn.execute(
            "INSERT OR IGNORE INTO lane_challenge_solves(challenge_id, miner_hotkey, solved_at_iso) "
            "VALUES (?, ?, ?)", (challenge_id, miner_hotkey, _now_iso()))
    store.write(_do)


def active_local_count(store: Store, tier: int) -> int:
    rows = store.query(
        "SELECT COUNT(*) AS n FROM lane_challenges "
        "WHERE family_id=? AND tier=? AND status='active' AND cnf_source='local'",
        (_FAMILY, tier))
    return rows[0]["n"]


def retire_ready(store: Store, tier: int) -> int:
    """Retire active LOCAL challenges that are old enough or saturated. Returns
    count retired. Mirrors sat_fill._retire_open_window_ready semantics."""
    now = _now_iso()
    age_cutoff = _iso_before(retire_after_seconds())
    retired = 0

    # RETENTION: null cnf_text on retire so retired CNFs (~0.46MB each) don't
    # accumulate forever — the bug that filled the monolith volume to 96%. The
    # CNF is immutable and only served while a challenge is active, so a retired
    # challenge never needs its body again.
    def _age(conn):
        # Age = time since CREATION, not last activity. updated_at_iso is bumped
        # on every solve by the submit active-guard, so keying age off it keeps a
        # popular challenge alive forever and starves refill — the board freezes.
        # created_at_iso is the stable mint time.
        cur = conn.execute(
            "UPDATE lane_challenges SET status='retired', cnf_text='', updated_at_iso=? "
            "WHERE family_id=? AND tier=? AND status='active' AND cnf_source='local' "
            "AND created_at_iso <= ?",
            (now, _FAMILY, tier, age_cutoff))
        return int(cur.rowcount or 0)
    retired += store.write(_age)

    threshold = retire_after_distinct_solvers()

    def _solved(conn):
        cur = conn.execute(
            "UPDATE lane_challenges SET status='retired', cnf_text='', updated_at_iso=? "
            "WHERE family_id=? AND tier=? AND status='active' AND cnf_source='local' "
            "AND challenge_id IN ("
            "  SELECT challenge_id FROM lane_challenge_solves "
            "  GROUP BY challenge_id HAVING COUNT(DISTINCT miner_hotkey) >= ?)",
            (now, _FAMILY, tier, threshold))
        return int(cur.rowcount or 0)
    retired += store.write(_solved)
    if retired:
        # broadcast tier: the active set shrank — drop the cached board snapshot.
        from . import board_cache as _bc
        _bc.invalidate_all()
    return retired


def reclaim_retired_cnf(store: Store) -> int:
    """One-shot: null cnf_text on already-retired challenges (back-fills the
    retention fix for rows retired before it shipped). Returns rows cleared."""
    def _do(conn):
        cur = conn.execute(
            "UPDATE lane_challenges SET cnf_text='' "
            "WHERE status='retired' AND cnf_source='local' AND length(cnf_text)>0")
        return int(cur.rowcount or 0)
    return store.write(_do)


def refill_tier(store: Store, tier: int, *, seed_input: str | None = None,
                log=lambda *a, **k: None) -> dict:
    """Retire ready challenges, then mint up to the tier target. Returns a
    summary. Deterministic in (seed_input, tier, sequence)."""
    seed_input = seed_input or default_seed_input()
    retired = retire_ready(store, tier)
    target = target_for(tier)
    n_vars, n_clauses = shape_for(tier)
    planting_method = method_for(tier)
    if (n_vars, n_clauses) != _TIER_SHAPE.get(tier) or planting_method != _TIER_METHOD.get(tier, "biased"):
        log("refill_shape_divergence", tier=tier, n_vars=n_vars, n_clauses=n_clauses,
            live=_TIER_SHAPE.get(tier), method=planting_method)

    minted = 0
    # sequence walks forward until target reached; mint_challenge_id collisions
    # (already-present ids) are skipped so re-runs in the same bucket are idempotent.
    seq = store.query(
        "SELECT COUNT(*) AS n FROM lane_challenges WHERE family_id=? AND tier=? AND cnf_source='local'",
        (_FAMILY, tier))[0]["n"]
    guard = 0
    while active_local_count(store, tier) < target and guard < target * 4 + 8:
        guard += 1
        cid = mint_challenge_id(seed_input, tier, seq)
        seq += 1
        existing = store.query(
            "SELECT status FROM lane_challenges WHERE challenge_id=?", (cid,))
        if existing:
            continue  # already minted this (seed_input,tier,seq) — idempotent skip
        seed = mint_seed(seed_input, tier, seq - 1)
        cnf_text, _planted = _gen_cnf_in_process(seed, n_vars, n_clauses, method=planting_method)
        seed_challenge(store, challenge_id=cid, tier=tier, cnf_text=cnf_text,
                       status="active")
        # mark provenance + updated_at for age retirement (seed_challenge defaults
        # cnf_source='local' via the column default and leaves updated_at_iso NULL).
        def _stamp(conn, cid=cid):
            conn.execute(
                "UPDATE lane_challenges SET updated_at_iso=created_at_iso WHERE challenge_id=?",
                (cid,))
        store.write(_stamp)
        minted += 1
    return {"tier": tier, "retired": retired, "minted": minted,
            "active": active_local_count(store, tier), "target": target,
            "shape": (n_vars, n_clauses)}


def refill_once(store: Store, *, seed_input: str | None = None, log=lambda *a, **k: None) -> list[dict]:
    """One full refill+retire pass across all configured tiers."""
    out = []
    for tier in sorted(_DEFAULT_TARGETS):
        out.append(refill_tier(store, tier, seed_input=seed_input, log=log))
    return out


async def refill_loop(store: Store, *, interval_seconds: int | None = None,
                      log=lambda *a, **k: None, stop_event: asyncio.Event | None = None) -> None:
    """Asyncio task: periodic refill+retire. Gated by the caller (only started
    when refill_enabled()). gen runs in a thread so the event loop is never
    blocked by CNF minting. Cancels cleanly."""
    interval = interval_seconds or _env_int("CATHEDRAL_REFILL_INTERVAL_SECONDS",
                                            _DEFAULT_INTERVAL_SECONDS)
    log("refill_loop_start", interval=interval, targets=_DEFAULT_TARGETS)
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                summary = await asyncio.to_thread(refill_once, store, log=log)
                log("refill_pass", summary=summary)
            except Exception as e:  # never let one bad pass kill the loop
                log("refill_error", error=str(e))
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(interval),
                    timeout=interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        log("refill_loop_cancelled")
        raise
