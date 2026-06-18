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
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from ..dimacs import gen_planted_3sat
from .app import seed_challenge
from .store import Store

# ---------------------------------------------------------------------------
# Subprocess-based CNF generation.
#
# gen_planted_3sat is pure-Python CPU-bound (~0.1s locally, ~0.5-2s on Railway's
# shared CPU). asyncio.to_thread(gen_planted_3sat, ...) does NOT release the
# Python GIL — the worker thread holds it for the entire gen duration, starving
# the event loop. A cold-start fill of 25+25=50 challenges can freeze the
# service for 25-50s, causing 6-12s+ request latency.
#
# Fix: run gen in a subprocess. subprocess.run() waits via os.waitpid() which
# releases the GIL — the event loop runs freely while the child generates. The
# child has its own interpreter, its own GIL, no contention.
#
# _SUBPROCESS_OK is set at import time by a quick probe. If subprocess creation
# is blocked (seccomp/container restrictions), the probe catches it and we fall
# back to in-process gen with a hard cap of 1 mint per pass (MINT_CAP_FALLBACK).
# At ~1s per gen, 1 mint per 60s tick = ~1.7% of requests may see that 1s GIL
# hold. Acceptable steady-state; cold start fills slowly (25 ticks = 25 min).
# ---------------------------------------------------------------------------
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_GEN_SCRIPT = (
    "import sys;"
    "sys.path.insert(0,sys.argv[1]);"
    "from scaffold.dimacs import gen_planted_3sat;"
    "cnf,_=gen_planted_3sat(int(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4]),method=sys.argv[5]);"
    "sys.stdout.write(cnf)"
)

_SUBPROCESS_TIMEOUT = int(os.environ.get("CATHEDRAL_GEN_SUBPROCESS_TIMEOUT", "30"))
MINT_CAP_SUBPROCESS = int(os.environ.get("CATHEDRAL_REFILL_MAX_MINTS", "3"))
MINT_CAP_FALLBACK   = 1   # if subprocess blocked; 1 gen×~1s GIL hold per 60s tick


def _probe_subprocess() -> bool:
    """Test subprocess creation AND scaffold import. Returns True only if a child
    process can import scaffold.dimacs and produce valid output. Probed at import
    time; if this fails, all gen calls use direct in-process gen with cap=1."""
    try:
        # Use the actual gen script with a tiny CNF (10 vars, 43 clauses) so we
        # confirm the full import+gen path works, not just subprocess creation.
        r = subprocess.run(
            [sys.executable, "-c", _GEN_SCRIPT,
             _APP_ROOT, "42", "10", "43", "ajm"],
            capture_output=True, text=True, timeout=15,
        )
        return (r.returncode == 0
                and r.stdout.startswith("p cnf ")
                and len(r.stdout) > 20)
    except Exception as e:
        print(f"[refill] subprocess probe failed ({e!r}); using in-process gen")
        return False


# Probe once at import time so startup logs reveal the mode.
_SUBPROCESS_OK: bool = _probe_subprocess()
print(f"[refill] subprocess_ok={_SUBPROCESS_OK} app_root={_APP_ROOT}")


def _gen_cnf_subprocess(seed: int, n_vars: int, n_clauses: int, method: str) -> str:
    """Run gen_planted_3sat in a child process. The parent waits via os.waitpid
    (releases the GIL) — the event loop is free during generation."""
    r = subprocess.run(
        [sys.executable, "-c", _GEN_SCRIPT,
         _APP_ROOT, str(seed), str(n_vars), str(n_clauses), method],
        capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"gen subprocess rc={r.returncode}: {r.stderr[:300]}")
    if not r.stdout.startswith("p cnf "):
        raise RuntimeError(
            f"gen subprocess bad output: {r.stdout[:50]!r} stderr={r.stderr[:100]!r}")
    return r.stdout


def _gen_cnf(seed: int, n_vars: int, n_clauses: int, method: str) -> str:
    """Generate CNF, using subprocess if available (GIL-free), else direct gen."""
    if _SUBPROCESS_OK:
        try:
            return _gen_cnf_subprocess(seed, n_vars, n_clauses, method)
        except Exception as e:
            print(f"[refill] subprocess gen failed ({e!r}); using in-process gen")
    cnf, _ = gen_planted_3sat(seed, n_vars, n_clauses, method=method)
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
    mint_cap = MINT_CAP_SUBPROCESS if _SUBPROCESS_OK else MINT_CAP_FALLBACK
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
    2. Per mint: to_thread(_gen_cnf) — subprocess if available (os.waitpid releases
       GIL), otherwise direct gen with hard cap=1 (bounded ~1s GIL hold per tick).
    3. to_thread: write challenge to DB.
    4. sleep(0): yield to event loop between mints.
    """
    seed_input = seed_input or default_seed_input()
    mint_cap = MINT_CAP_SUBPROCESS if _SUBPROCESS_OK else MINT_CAP_FALLBACK
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
    With subprocess available: gen runs in a child process (GIL fully released).
    Fallback: hard cap=1 mint per tier per tick (~1s GIL hold per 60s = acceptable).
    """
    interval = interval_seconds or _env_int("CATHEDRAL_REFILL_INTERVAL_SECONDS",
                                            _DEFAULT_INTERVAL_SECONDS)
    log("refill_loop_start", interval=interval, targets=_DEFAULT_TARGETS,
        subprocess_ok=_SUBPROCESS_OK,
        mint_cap=MINT_CAP_SUBPROCESS if _SUBPROCESS_OK else MINT_CAP_FALLBACK)
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
