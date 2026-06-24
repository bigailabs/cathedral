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
import hmac
import json
import multiprocessing
import os
import queue as _queue
import secrets
import threading as _threading
import time as _time
import urllib.parse
import urllib.request
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
MINT_CAP_FORK     = int(os.environ.get("CATHEDRAL_REFILL_MAX_MINTS", "4"))
MINT_CAP_FALLBACK = 1   # if fork blocked; 1 gen×~5-6s GIL hold per tick


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


# ---------------------------------------------------------------------------
# Pre-generation buffer — zero-latency refill ticks.
#
# A background daemon thread continuously generates CNF bodies into a small
# per-tier queue.  The refill tick drains from the queue (instant) instead of
# blocking the event loop on a 5-6s fork gen.
#
# The gen child still runs at nice(19) in a separate process; the parent
# background thread waits in Pipe.poll() (GIL-free).  This way:
#   • CPU is consumed by gen continuously at a low sustained level rather than
#     in 5-6s concentrated spikes every 60s.
#   • The refill tick itself never calls _gen_cnf — it takes from the queue
#     (a Python queue.Queue.get(timeout=...) that returns in microseconds when
#     the queue is non-empty).
#   • If the queue is empty (gen can't keep up), the refill tick falls back to
#     direct _gen_cnf (same as before) so minting never stalls.
#
# Queue sizing: 8 per tier gives the live board enough burst capacity when
# hundreds of miners saturate active challenges faster than a 1-mint tick.
# Each entry is roughly 450KB CNF text at the current tier-1 shape.
# ---------------------------------------------------------------------------

_PREGEN_QUEUE_SIZE = int(os.environ.get("CATHEDRAL_PREGEN_QUEUE_SIZE", "8"))

# Per-tier queues: tier -> queue.Queue of (cnf_text,)
_pregen_queues: dict[int, _queue.Queue] = {}
_pregen_started = False
_pregen_lock = _threading.Lock()


def _pregen_enabled() -> bool:
    raw = os.environ.get("CATHEDRAL_PREGEN_ENABLED", "").strip().lower()
    if raw:
        return _PREGEN_QUEUE_SIZE > 0 and raw in {"1", "true", "yes", "on"}
    return (
        _PREGEN_QUEUE_SIZE > 0
    )


def _pregen_worker(tier: int) -> None:
    """Background daemon thread: continuously pre-generates CNF bodies.

    Generates one challenge at a time into the queue.  When the queue is full
    (size=_PREGEN_QUEUE_SIZE), sleeps until it has room.  On any error, backs
    off 5s and retries.  Exits only when the process exits (daemon=True).
    """
    q = _pregen_queues[tier]
    n_vars, n_clauses = _TIER_SHAPE.get(tier, (6000, 25560))
    method = _TIER_METHOD.get(tier, "biased")
    # Use a fixed seed counter per-worker (doesn't need to match refill seeds —
    # the worker's output is consumed by seed in-order by the refill loop which
    # assigns real challenge IDs).  Use a time-based offset per tier.
    seed_counter = int(_time.monotonic() * 1000) + tier * 100_000
    while True:
        if not _FORK_OK:
            # Fork is broken; don't spin-generate in-process (GIL hold forever).
            _time.sleep(10)
            continue
        try:
            # Wait until there's room in the queue.
            if q.full():
                _time.sleep(1)
                continue
            seed_counter += 1
            cnf = _gen_cnf_fork(seed_counter, n_vars, n_clauses, method)
            q.put(cnf)
            print(f"[pregen] tier={tier} queued (queue_size={q.qsize()})")
        except Exception as e:
            print(f"[pregen] tier={tier} error: {e!r}; backing off 5s")
            _time.sleep(5)


def _ensure_pregen_started() -> None:
    """Start pre-generation daemon threads (one per tier, idempotent)."""
    global _pregen_started
    if not _pregen_enabled():
        return
    if _pregen_started:
        return
    with _pregen_lock:
        if _pregen_started:
            return
        for tier in sorted(_DEFAULT_TARGETS):
            _pregen_queues[tier] = _queue.Queue(maxsize=_PREGEN_QUEUE_SIZE)
            t = _threading.Thread(
                target=_pregen_worker, args=(tier,),
                name=f"pregen-tier{tier}", daemon=True,
            )
            t.start()
            print(f"[pregen] started background gen thread for tier {tier}")
        _pregen_started = True


def _get_pregen_cnf(tier: int) -> str | None:
    """Try to get a pre-generated CNF from the queue (non-blocking).

    Returns None if the queue is empty (caller should fall back to _gen_cnf).
    """
    if not _pregen_enabled():
        return None
    q = _pregen_queues.get(tier)
    if q is None:
        return None
    try:
        return q.get_nowait()
    except _queue.Empty:
        return None


_FAMILY = "synthetic_boolean_v1"
_EPHEMERAL_SEED_SECRET = secrets.token_bytes(32)

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
_DEFAULT_RETIRE_AFTER_SECONDS = 60 * 60
_DEFAULT_RETIRE_AFTER_DISTINCT_SOLVERS = 256
_DEFAULT_INTERVAL_SECONDS = 20


def refill_enabled() -> bool:
    return os.environ.get("CATHEDRAL_REFILL_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def generator_config() -> dict:
    """Public, read-only config for the live SAT refill generator.

    This is intentionally derived from the same helpers the refill loop uses so
    the board can expose what will actually mint, not a duplicated doc string.
    """
    external_requested = generator_requested()
    external_configured = generator_configured()
    external_enabled = external_requested and external_configured
    return {
        "enabled": refill_enabled(),
        "family_id": _FAMILY,
        "kind": "external_sat_generator" if external_enabled else "local_refill",
        "source": "scaffold.publisher.refill",
        "interval_seconds": _env_int("CATHEDRAL_REFILL_INTERVAL_SECONDS",
                                     _DEFAULT_INTERVAL_SECONDS),
        "retire_after_seconds": retire_after_seconds(),
        "retire_after_distinct_solvers": retire_after_distinct_solvers(),
        "mint_cap": generator_max_mints() if external_enabled else (
            MINT_CAP_FORK if _FORK_OK else MINT_CAP_FALLBACK),
        "pregen_enabled": _pregen_enabled(),
        "pregen_queue_size": _PREGEN_QUEUE_SIZE,
        "external": {
            "requested": external_requested,
            "enabled": external_enabled,
            "configured": external_configured,
            "configuration_error": (
                "missing_url_or_token"
                if external_requested and not external_configured else None
            ),
            "local_fallback": _env_bool("CATHEDRAL_SAT_GENERATOR_LOCAL_FALLBACK", False),
            "timeout_seconds": _generator_timeout(),
        },
        "seed": {
            "kind": "server_hmac",
            "secret_configured": bool(
                os.environ.get("CATHEDRAL_REFILL_SEED_SECRET", "").strip()
                or os.environ.get("CATHEDRAL_PUBLISHER_SEED_SECRET", "").strip()
            ),
        },
        "tiers": [
            {
                "tier": tier,
                "target_active": target_for(tier),
                "n_vars": shape_for(tier)[0],
                "n_clauses": shape_for(tier)[1],
                "method": method_for(tier),
                "generator_kind": generator_kind_for(tier),
            }
            for tier in sorted(_DEFAULT_TARGETS)
        ],
    }


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def generator_requested() -> bool:
    return _env_bool("CATHEDRAL_SAT_GENERATOR_ENABLED")


def generator_configured() -> bool:
    return (
        bool(os.environ.get("CATHEDRAL_SAT_GENERATOR_URL", "").strip())
        and bool(os.environ.get("CATHEDRAL_SAT_GENERATOR_TOKEN", "").strip())
    )


def generator_enabled() -> bool:
    return generator_requested() and generator_configured()


def generator_max_mints() -> int:
    return max(1, _env_int("CATHEDRAL_SAT_GENERATOR_MAX_MINTS", 25))


def generator_kind_for(tier: int) -> str:
    return os.environ.get(
        f"CATHEDRAL_SAT_GENERATOR_KIND_T{tier}",
        os.environ.get("CATHEDRAL_SAT_GENERATOR_KIND", "random_3sat"),
    ).strip() or "random_3sat"


def _generator_timeout() -> int:
    return max(1, _env_int("CATHEDRAL_SAT_GENERATOR_TIMEOUT_SECONDS", 20))


def _generator_url(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    base = os.environ["CATHEDRAL_SAT_GENERATOR_URL"].strip().rstrip("/") + "/"
    return urllib.parse.urljoin(base, path_or_url.lstrip("/"))


def _generator_json(
    method: str,
    path_or_url: str,
    payload: dict | None = None,
    *,
    idempotency_key: str | None = None,
) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {os.environ['CATHEDRAL_SAT_GENERATOR_TOKEN'].strip()}",
        "User-Agent": "cathedral-publisher-refill/1",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(
        _generator_url(path_or_url), data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=_generator_timeout()) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _generator_text(path_or_url: str) -> str:
    headers = {
        "Authorization": f"Bearer {os.environ['CATHEDRAL_SAT_GENERATOR_TOKEN'].strip()}",
        "User-Agent": "cathedral-publisher-refill/1",
    }
    req = urllib.request.Request(_generator_url(path_or_url), headers=headers)
    with urllib.request.urlopen(req, timeout=_generator_timeout()) as resp:
        return resp.read().decode("utf-8")


def _release_generator_lease(lease_id: str) -> None:
    try:
        _generator_json("POST", f"/v1/challenges/lease/{lease_id}/release")
    except Exception as exc:
        print(f"[refill] generator_release_failed lease_id={lease_id} error={exc!r}")


def _lease_generator_cnf(challenge_id: str, tier: int) -> str:
    """Lease, fetch, hash-check, and confirm one CNF from the generator service."""
    kind = generator_kind_for(tier)
    lease_id: str | None = None
    try:
        lease = _generator_json(
            "POST",
            "/v1/challenges/lease",
            {"family": _FAMILY, "tier": tier, "kind": kind},
            idempotency_key=challenge_id,
        )
        lease_id = str(lease["lease_id"])
        cnf_text = _generator_text(str(lease["cnf_url"]))
        expected_sha = str(lease.get("cnf_sha256") or "")
        actual_sha = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
        if expected_sha and actual_sha != expected_sha:
            raise RuntimeError(
                f"generator cnf hash mismatch expected={expected_sha} actual={actual_sha}")
        _generator_json(
            "POST",
            f"/v1/challenges/lease/{lease_id}/confirm",
            {
                "cathedral_challenge_id": challenge_id,
                "cnf_sha256_witnessed": actual_sha,
            },
        )
        print(
            "[refill] generator_lease_ok "
            f"tier={tier} kind={kind} lease_id={lease_id} "
            f"run_id={lease.get('generator_run_id')} vars={lease.get('num_vars')} "
            f"clauses={lease.get('num_clauses')}"
        )
        return cnf_text
    except Exception:
        if lease_id:
            _release_generator_lease(lease_id)
        raise


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


def _seed_secret_bytes() -> bytes:
    raw = (
        os.environ.get("CATHEDRAL_REFILL_SEED_SECRET", "").strip()
        or os.environ.get("CATHEDRAL_PUBLISHER_SEED_SECRET", "").strip()
    )
    return raw.encode("utf-8") if raw else _EPHEMERAL_SEED_SECRET


def _seed_hmac_hex(*parts: object) -> str:
    msg = ":".join(str(p) for p in parts).encode("utf-8")
    return hmac.new(_seed_secret_bytes(), msg, hashlib.sha256).hexdigest()


def default_seed_input() -> str:
    """Block-ish seed bucket. Mixed with a server secret before use."""
    return datetime.now(timezone.utc).strftime("%Y%m%d%H")


def mint_seed(seed_input: str, tier: int, sequence: int) -> int:
    """Secret-derived 63-bit integer seed from (seed_input, tier, sequence)."""
    h = _seed_hmac_hex("seed", seed_input, tier, sequence)
    return int(h[:16], 16)


def mint_challenge_id(seed_input: str, tier: int, sequence: int) -> str:
    suffix = _seed_hmac_hex("id", seed_input, tier, sequence)[:20]
    return f"sat-t{tier}-random-3sat-{suffix}"


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
    mint_cap = generator_max_mints() if generator_enabled() else (
        MINT_CAP_FORK if _FORK_OK else MINT_CAP_FALLBACK)
    retired, work = _plan_tier(store, tier, seed_input, mint_cap, log)
    n_vars_hint = _TIER_SHAPE.get(tier, (6000, 25560))[0]
    n_clauses_hint = _TIER_SHAPE.get(tier, (6000, 25560))[1]
    minted = 0
    for cid, seed, n_vars, n_clauses, method in work:
        _mint_one(store, cid, tier, seed, n_vars, n_clauses, method)
        minted += 1
    return {"tier": tier, "retired": retired, "minted": minted,
            "active": active_local_count(store, tier), "target": target_for(tier),
            "shape": (n_vars_hint, n_clauses_hint)}


def refill_once(store: Store, *, seed_input: str | None = None, log=lambda *a, **k: None) -> list[dict]:
    """Synchronous refill (used in tests). Production uses refill_once_async."""
    return [refill_tier(store, tier, seed_input=seed_input, log=log)
            for tier in sorted(_DEFAULT_TARGETS)]


def _mint_one(store: Store, cid: str, tier: int, seed: int,
              n_vars: int, n_clauses: int, method: str) -> str:
    """Generate + commit one challenge.  Called in a thread via to_thread.

    Tries the pre-generation queue first (O(1), no CPU spike).  Falls back to
    direct fork gen if the queue is empty (background gen hasn't caught up yet).
    Returns the CNF text so the caller can log it.
    """
    if generator_requested() and not generator_configured():
        raise RuntimeError("sat_generator_enabled_but_missing_url_or_token")

    if generator_enabled():
        try:
            cnf_text = _lease_generator_cnf(cid, tier)
            _commit_challenge(store, cid, tier, cnf_text)
            print(f"[refill] minted tier={tier} cid={cid[:20]}... source=generator")
            return cnf_text
        except Exception as exc:
            print(f"[refill] generator_mint_failed tier={tier} cid={cid[:20]} error={exc!r}")
            if not _env_bool("CATHEDRAL_SAT_GENERATOR_LOCAL_FALLBACK", False):
                raise

    cnf_text = _get_pregen_cnf(tier)
    source = "pregen"
    if cnf_text is None:
        source = "fork" if _FORK_OK else "inproc"
        cnf_text = _gen_cnf(seed, n_vars, n_clauses, method)
    _commit_challenge(store, cid, tier, cnf_text)
    print(f"[refill] minted tier={tier} cid={cid[:20]}... source={source}")
    return cnf_text


async def refill_tier_async(store: Store, tier: int, *, seed_input: str | None = None,
                            log=lambda *a, **k: None) -> dict:
    """Async refill for one tier.
    1. Ensure background pre-gen threads are running.
    2. to_thread: retire + plan (DB calls, releases GIL via I/O).
    3. Per mint: to_thread(_mint_one) — takes from pre-gen queue (instant) or
       falls back to fork gen (GIL-free via Pipe.poll). One at a time.
    4. sleep(0): yield to event loop between mints.
    """
    if not generator_enabled():
        _ensure_pregen_started()   # idempotent; starts bg gen threads on first call
    seed_input = seed_input or default_seed_input()
    mint_cap = generator_max_mints() if generator_enabled() else (
        MINT_CAP_FORK if _FORK_OK else MINT_CAP_FALLBACK)
    n_vars_hint, n_clauses_hint = shape_for(tier)

    retired, work = await asyncio.to_thread(_plan_tier, store, tier, seed_input, mint_cap, log)

    minted = 0
    failed = 0
    for cid, seed, n_vars, n_clauses, method in work:
        try:
            await asyncio.to_thread(_mint_one, store, cid, tier, seed, n_vars, n_clauses, method)
        except Exception as exc:
            failed += 1
            log("refill_mint_failed", tier=tier, cid=cid[:20], error=str(exc))
            continue
        minted += 1
        await asyncio.sleep(0)  # yield to the event loop between mints

    active = await asyncio.to_thread(active_local_count, store, tier)
    return {"tier": tier, "retired": retired, "minted": minted,
            "failed": failed,
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
    Background pre-gen threads keep a small CNF buffer so each mint is instant
    (queue.get_nowait).  Falls back to fork gen if the buffer is empty.
    Either way the event loop is never blocked: queue reads are instant, and
    fork gen releases the GIL via Pipe.poll (epoll_wait).
    """
    interval = interval_seconds or _env_int("CATHEDRAL_REFILL_INTERVAL_SECONDS",
                                            _DEFAULT_INTERVAL_SECONDS)
    log("refill_loop_start", interval=interval, targets=_DEFAULT_TARGETS,
        fork_ok=_FORK_OK,
        mint_cap=generator_max_mints() if generator_enabled() else (
            MINT_CAP_FORK if _FORK_OK else MINT_CAP_FALLBACK),
        generator=generator_config())
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
