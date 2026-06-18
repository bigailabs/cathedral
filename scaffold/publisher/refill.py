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
import hashlib
import multiprocessing
import os
import time as _time
from datetime import datetime, timedelta, timezone

from ..dimacs import gen_planted_3sat
from .app import seed_challenge
from .store import Store

# ---------------------------------------------------------------------------
# Fork-based CNF generation — releases the GIL while the child runs.
#
# gen_planted_3sat is pure-Python CPU-bound (~0.1s locally, ~5-6s on Railway's
# shared CPU). asyncio.to_thread(gen_planted_3sat, ...) does NOT release the
# Python GIL — the worker thread holds it for the entire gen duration, starving
# the event loop and causing 5-12s+ request latency every 60s refill tick.
#
# Fix: run gen in a forked child process via multiprocessing.Process(fork).
#   • fork() copies the parent address space — scaffold.dimacs is already
#     imported; no sys.path or execve() needed; no seccomp risk.
#   • Parent receives the CNF over a Pipe. Connection.recv() calls os.read()
#     (C extension) which releases the GIL — the event loop runs freely while
#     the child generates.
#   • Child only imports `random` (already imported); fork-safe.
#
# If fork gen fails (container restriction, OOM, etc.) we fall back to
# in-process gen with MINT_CAP_FALLBACK=1 (one GIL-hold of ~5-6s per tick).
# ---------------------------------------------------------------------------

_FORK_TIMEOUT = int(os.environ.get("CATHEDRAL_GEN_FORK_TIMEOUT", "45"))
MINT_CAP_FORK     = int(os.environ.get("CATHEDRAL_REFILL_MAX_MINTS", "1"))
MINT_CAP_FALLBACK = 1   # if fork blocked; 1 gen×~5-6s GIL hold per 60s tick


def _worker_gen(conn, seed: int, n_vars: int, n_clauses: int, method: str) -> None:
    """Run inside a forked child: generate CNF and send over pipe.

    Runs at nice(19) — lowest scheduler priority — so the parent process
    (event loop, request handlers) retains full CPU access while the child
    generates. On a single-vCPU container this prevents CPU starvation of
    the parent's uvicorn event loop during the ~5s gen.
    """
    try:
        os.nice(19)  # yield CPU priority to parent; child still runs but deferred
    except OSError:
        pass  # containers may not permit nice; continue without it
    try:
        cnf, _ = gen_planted_3sat(seed, n_vars, n_clauses, method=method)
        conn.send(("ok", cnf))
    except Exception as exc:
        conn.send(("err", str(exc)))
    finally:
        conn.close()


def _gen_cnf_fork(seed: int, n_vars: int, n_clauses: int, method: str) -> str:
    """Generate CNF in a forked child process.

    The parent blocks on Pipe.recv() — a C-level os.read() that releases the
    GIL — so the asyncio event loop is free during the child's ~5-6s gen.
    """
    ctx = multiprocessing.get_context("fork")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_worker_gen,
                    args=(child_conn, seed, n_vars, n_clauses, method),
                    daemon=True)
    p.start()
    child_conn.close()          # parent never writes; release child end in parent
    try:
        if not parent_conn.poll(_FORK_TIMEOUT):
            p.terminate()
            p.join(5)
            raise RuntimeError(f"gen fork timed out after {_FORK_TIMEOUT}s "
                               f"(n={n_vars}, m={n_clauses}, method={method})")
        status, payload = parent_conn.recv()
    finally:
        parent_conn.close()
        p.join(5)
    if status != "ok":
        raise RuntimeError(f"gen fork error: {payload}")
    return payload


# Try one fork at module import time (runs in a thread via to_thread, NOT on
# the event loop — this is safe). Result cached in _FORK_OK.
# NOTE: This probe runs during _start_refill() which IS an async coroutine on
# the event loop, so we do NOT call the probe here synchronously. Instead we
# optimistically set _FORK_OK=True and let per-call failures flip it to False.
_FORK_OK: bool = True
print(f"[refill] fork_mode=optimistic mint_cap={MINT_CAP_FORK} fallback_cap={MINT_CAP_FALLBACK}")


def _gen_cnf(seed: int, n_vars: int, n_clauses: int, method: str) -> str:
    """Generate CNF. Uses fork (GIL-free) if _FORK_OK, else direct gen."""
    global _FORK_OK
    t0 = _time.monotonic()
    if _FORK_OK:
        try:
            cnf = _gen_cnf_fork(seed, n_vars, n_clauses, method)
            print(f"[refill] fork_gen ok n={n_vars} m={n_clauses} method={method} "
                  f"elapsed={_time.monotonic()-t0:.2f}s")
            return cnf
        except Exception as e:
            print(f"[refill] fork_gen failed ({e!r}); disabling fork, using in-process")
            _FORK_OK = False
    cnf, _ = gen_planted_3sat(seed, n_vars, n_clauses, method=method)
    print(f"[refill] inproc_gen ok n={n_vars} elapsed={_time.monotonic()-t0:.2f}s")
    return cnf

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


def _plan_tier(store: Store, tier: int, seed_input: str, mint_cap: int,
               log=lambda *a, **k: None) -> tuple[int, list[tuple[str, int, int, int, str]]]:
    """Retire stale challenges + plan mints for this pass. Returns (retired, work).
    work = list of (cid, seed, n_vars, n_clauses, method). Called in a thread."""
    retired = retire_ready(store, tier)
    target = target_for(tier)
    n_vars, n_clauses = shape_for(tier)
    planting_method = method_for(tier)
    if (n_vars, n_clauses) != _TIER_SHAPE.get(tier) or planting_method != _TIER_METHOD.get(tier, "biased"):
        log("refill_shape_divergence", tier=tier, n_vars=n_vars, n_clauses=n_clauses,
            live=_TIER_SHAPE.get(tier), method=planting_method)

    seq = store.query(
        "SELECT COUNT(*) AS n FROM lane_challenges WHERE family_id=? AND tier=? AND cnf_source='local'",
        (_FAMILY, tier))[0]["n"]
    work: list[tuple[str, int, int, int, str]] = []
    guard = 0
    while active_local_count(store, tier) < target and guard < target * 4 + 8:
        guard += 1
        if len(work) >= mint_cap:
            break
        cid = mint_challenge_id(seed_input, tier, seq)
        seq += 1
        if store.query("SELECT status FROM lane_challenges WHERE challenge_id=?", (cid,)):
            continue
        seed = mint_seed(seed_input, tier, seq - 1)
        work.append((cid, seed, n_vars, n_clauses, planting_method))
    return retired, work


def _commit_challenge(store: Store, cid: str, tier: int, cnf_text: str) -> None:
    """Write one minted challenge. Called in a thread."""
    seed_challenge(store, challenge_id=cid, tier=tier, cnf_text=cnf_text, status="active")
    def _stamp(conn, cid=cid):
        conn.execute(
            "UPDATE lane_challenges SET updated_at_iso=created_at_iso WHERE challenge_id=?", (cid,))
    store.write(_stamp)


# Synchronous versions kept for test compatibility.
def refill_tier(store: Store, tier: int, *, seed_input: str | None = None,
                log=lambda *a, **k: None) -> dict:
    """Synchronous refill (used in tests). Production uses refill_tier_async."""
    seed_input = seed_input or default_seed_input()
    mint_cap = MINT_CAP_FORK if _FORK_OK else MINT_CAP_FALLBACK
    retired, work = _plan_tier(store, tier, seed_input, mint_cap, log)
    n_vars_hint = _TIER_SHAPE.get(tier, (6000, 25560))[0]
    n_clauses_hint = _TIER_SHAPE.get(tier, (6000, 25560))[1]
    minted = 0
    for cid, seed, n_vars, n_clauses, method in work:
        cnf_text = _gen_cnf(seed, n_vars, n_clauses, method)
        _commit_challenge(store, cid, tier, cnf_text)
        minted += 1
    return {"tier": tier, "retired": retired, "minted": minted,
            "active": active_local_count(store, tier), "target": target_for(tier),
            "shape": (n_vars_hint, n_clauses_hint)}


def refill_once(store: Store, *, seed_input: str | None = None, log=lambda *a, **k: None) -> list[dict]:
    """Synchronous refill (used in tests). Production uses refill_once_async."""
    return [refill_tier(store, tier, seed_input=seed_input, log=log)
            for tier in sorted(_DEFAULT_TARGETS)]


async def refill_tier_async(store: Store, tier: int, *, seed_input: str | None = None,
                            log=lambda *a, **k: None) -> dict:
    """Async refill for one tier.
    1. to_thread: retire + plan (DB calls, releases GIL via I/O).
    2. Per mint: to_thread(_gen_cnf) — fork process if available (os.read releases
       GIL on Pipe.recv), otherwise direct gen with hard cap=1 (bounded GIL hold).
    3. to_thread: write challenge to DB.
    4. sleep(0): yield to event loop between mints.
    """
    seed_input = seed_input or default_seed_input()
    mint_cap = MINT_CAP_FORK if _FORK_OK else MINT_CAP_FALLBACK
    n_vars_hint, n_clauses_hint = shape_for(tier)

    retired, work = await asyncio.to_thread(_plan_tier, store, tier, seed_input, mint_cap, log)

    minted = 0
    for cid, seed, n_vars, n_clauses, method in work:
        cnf_text = await asyncio.to_thread(_gen_cnf, seed, n_vars, n_clauses, method)
        await asyncio.to_thread(_commit_challenge, store, cid, tier, cnf_text)
        minted += 1
        await asyncio.sleep(0)  # yield to the event loop between mints

    active = await asyncio.to_thread(active_local_count, store, tier)
    return {"tier": tier, "retired": retired, "minted": minted,
            "active": active, "target": target_for(tier),
            "shape": (n_vars_hint, n_clauses_hint)}


async def refill_once_async(store: Store, *, seed_input: str | None = None,
                            log=lambda *a, **k: None) -> list[dict]:
    """One full async refill+retire pass across all configured tiers."""
    return [await refill_tier_async(store, tier, seed_input=seed_input, log=log)
            for tier in sorted(_DEFAULT_TARGETS)]


async def refill_loop(store: Store, *, interval_seconds: int | None = None,
                      log=lambda *a, **k: None, stop_event: asyncio.Event | None = None) -> None:
    """Asyncio task: periodic refill+retire.
    Uses refill_once_async so each gen call is isolated in its own to_thread.
    With fork gen: child process holds its own GIL; parent Pipe.recv releases
    the parent GIL so the event loop stays responsive during generation.
    Fallback: hard cap=1 mint per tier per tick (bounded GIL hold per 60s).
    """
    interval = interval_seconds or _env_int("CATHEDRAL_REFILL_INTERVAL_SECONDS",
                                            _DEFAULT_INTERVAL_SECONDS)
    log("refill_loop_start", interval=interval, targets=_DEFAULT_TARGETS,
        fork_ok=_FORK_OK,
        mint_cap=MINT_CAP_FORK if _FORK_OK else MINT_CAP_FALLBACK)
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                summary = await refill_once_async(store, log=log)
                log("refill_pass", summary=summary)
            except Exception as e:
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
