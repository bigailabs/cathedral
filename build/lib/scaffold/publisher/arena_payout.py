"""Lane I — pay-on-disagreement-proven-hardness payout loop (TASK 2).

An breaker instance pays ONLY when hardness is PROVEN by disagreement, never
claimed (V4-DESIGN.md → Lane I):

  * the reigning champion TIMES OUT on the instance (host-observed — the one
    outcome a miner cannot self-certify), AND
  * some OTHER registered solver closes it with a VALID certificate (SAT witness
    re-checked / UNSAT DRAT re-checked by run_batch's referee).

A family that falls to a cheap trick stops separating solvers, so its reward
dries up automatically — the structural fix for the PUPPER failure mode.

Pricing (decaying share-of-separation):
    payment = BASE × max(0, (champion_par2 − best_closer_par2)/TIMEOUT) × 0.97^round
half-life ~23 rounds; reaches zero the round the champion closes it. Because the
champion timed out, its per-instance PAR-2 is PAR_K×timeout, and the separation
factor is clamped to [0,1] so BASE is the ceiling (a paid Lane I row is ≤ BASE).

Anti-gaming (both REQUIRED, V4-DESIGN.md):
  * QUARANTINE_ROUNDS = 3 — a closer registered in round R can only serve as
    closing evidence for instances submitted ≤ R−3 (kills submit-and-farm-same-
    round). Enforced as: closer.registered_round ≤ instance.submitted_round − 3.
  * MIN_BATCH_SCORE = 0.5 — the closer must solve ≥50% of the standard Lane S
    batch (kills trivially-specialized closers). Residual attack requires
    pre-registering a broadly competitive solver, which is aligned behavior.

On a paid round, a signed v6 row is emitted to the INSTANCE's owner hotkey.
Env-gated by CATHEDRAL_ARENA_PAYOUT_ENABLED (default OFF). Certificate checking
is NOT re-implemented — it rides solver_arena.run_batch, the same referee Lane S
uses, so a forged closer cert can never trigger a payout.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Callable

from ..contract import Outcome
from ..lanes.solver_arena import (
    Instance, PAR_K, SolverArenaLane, SolverSpec, par2_ms, run_batch,
)
from . import rows
from .store import Store, new_uuid

_QUARANTINE_ROUNDS = 3
_MIN_BATCH_SCORE = 0.5
_DEFAULT_BASE = 1.0
_DECAY = 0.97
_DEFAULT_INTERVAL_SECONDS = 60
_DEFAULT_TIER = 0
_DEFAULT_BATCH_SIZE = 6

AdapterResolver = Callable[[SolverSpec], "object | None"]


def arena_payout_enabled() -> bool:
    return os.environ.get("CATHEDRAL_ARENA_PAYOUT_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def lane_i_price(champion_par2_ms: float, best_closer_par2_ms: float,
                 timeout_ms: float, round_no: int, base: float = _DEFAULT_BASE) -> float:
    """payment = base × max(0,(champ_par2 − closer_par2)/TIMEOUT) × 0.97^round.

    The separation factor is clamped to [0,1] so base is the ceiling: with the
    champion timing out (par2 = PAR_K×timeout) and a fast closer, (champ−closer)/T
    can exceed 1, but a paid row must stay ≤ base (validators score [0,1])."""
    if timeout_ms <= 0:
        return 0.0
    sep = (champion_par2_ms - best_closer_par2_ms) / timeout_ms
    sep = max(0.0, min(1.0, sep))
    return round(base * sep * (_DECAY ** max(0, round_no)), 6)


def _batch_min_score(lane: SolverArenaLane, adapter, *, seed: int, tier: int,
                     batch_size: int) -> float:
    """Fraction of the standard Lane S batch this closer certifiably solves —
    the MIN_BATCH_SCORE gate input. Uses the same fresh-batch + run_batch referee
    as Lane S so a closer can't fake breadth."""
    batch = lane.build_batch(seed, tier, batch_size)
    results = run_batch(adapter, batch)
    if not results:
        return 0.0
    return sum(1 for r in results if r.solved) / len(results)


def settle_instance(
    store: Store,
    lane: SolverArenaLane,
    instance: dict,
    *,
    champion_spec: SolverSpec,
    champion_adapter,
    closers: list[tuple[SolverSpec, object, int]],   # (spec, adapter, registered_round)
    current_round: int,
    private_key_hex: str,
    epoch_salt: str,
    base: float = _DEFAULT_BASE,
    tier: int = _DEFAULT_TIER,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    log=lambda *a, **k: None,
) -> dict:
    """Try to pay one instance for `current_round`. Returns a verdict dict.

    Pays iff: champion TIMES OUT on the instance AND at least one quarantine-clear,
    min-batch-passing closer closes it with a VALID cert. Emits a signed row to
    the instance owner on payment. Idempotent per round (last_paid_round guard)."""
    iid = instance["instance_id"]
    if instance.get("last_paid_round") == current_round:
        return {"instance_id": iid, "paid": False, "reason": "already_paid_this_round"}

    cnf = instance.get("cnf_text") or ""
    if not cnf.strip():
        return {"instance_id": iid, "paid": False, "reason": "no_cnf_body"}

    timeout_ms = lane.build_batch(0, tier, 1)[0].timeout_ms
    inst = Instance(task_id=iid, cnf=cnf, timeout_ms=timeout_ms)
    tmap = {iid: timeout_ms}

    # 1. the champion must TIME OUT (host-observed) on this instance.
    champ_res = run_batch(champion_adapter, [inst])
    if not champ_res or champ_res[0].outcome != Outcome.TIMEOUT:
        return {"instance_id": iid, "paid": False, "reason": "champion_did_not_time_out"}
    champ_par2 = par2_ms(champ_res, tmap)  # = PAR_K × timeout_ms

    # 2. find the best quarantine-clear, broadly-competitive closer that closes it.
    quarantine_cutoff = instance["submitted_round"] - _QUARANTINE_ROUNDS
    best_closer: SolverSpec | None = None
    best_par2 = float("inf")
    for spec, adapter, reg_round in closers:
        if spec.commitment_id == champion_spec.commitment_id:
            continue  # the champion can't be its own disagreement evidence
        if reg_round > quarantine_cutoff:
            log("lane_i_closer_quarantined", closer=spec.commitment_id[:12],
                reg_round=reg_round, cutoff=quarantine_cutoff)
            continue
        # MIN_BATCH_SCORE: solve ≥50% of the standard batch (broad competitor).
        bscore = _batch_min_score(lane, adapter, seed=current_round, tier=tier,
                                  batch_size=batch_size)
        if bscore < _MIN_BATCH_SCORE:
            log("lane_i_closer_too_specialized", closer=spec.commitment_id[:12],
                batch_score=round(bscore, 3))
            continue
        # does it actually CLOSE this instance with a valid cert?
        res = run_batch(adapter, [inst])
        if not res or not res[0].solved:
            continue
        cp = par2_ms(res, tmap)
        if cp < best_par2:
            best_par2 = cp
            best_closer = spec

    if best_closer is None:
        return {"instance_id": iid, "paid": False, "reason": "no_valid_closer"}

    # 3. price + emit a signed row to the INSTANCE owner.
    price = lane_i_price(champ_par2, best_par2, timeout_ms, current_round, base=base)
    if price <= 0.0:
        return {"instance_id": iid, "paid": False, "reason": "zero_price",
                "champion_par2": champ_par2, "best_closer_par2": best_par2}

    ran_at = _now_iso()
    row_uuid = new_uuid()
    answer_hash = (rows._hash16(f"lanei:{iid}:{best_closer.commitment_id}") * 4)[:64]
    vd = (rows._hash16(f"sep:{champ_par2}:{best_par2}:r{current_round}") * 4)[:64]
    emitted = rows.build_solve_rows(
        row_uuid=row_uuid, miner_hotkey=instance["owner_hotkey"],
        agent_id=new_uuid(), challenge_id=f"lanei:{iid}", tier=tier,
        weighted_score=price, answer_hash=answer_hash, verifier_details_hash=vd,
        ran_at=ran_at, epoch_salt=epoch_salt, solve_rank=1, solved=True,
        private_key_hex=private_key_hex)

    def _pay(conn):
        cur = conn.execute(
            "UPDATE arena_instances SET status='paid', last_paid_round=?, paid_at_iso=? "
            "WHERE instance_id=? AND (last_paid_round IS NULL OR last_paid_round <> ?)",
            (current_round, ran_at, iid, current_round))
        if int(cur.rowcount or 0) != 1:
            return False
        for r in emitted:
            conn.execute(
                "INSERT OR IGNORE INTO eval_runs "
                "(id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (r["id"], r["ran_at"], int(r["eval_output_schema_version"]),
                 r["miner_hotkey"], r["task_type"], json.dumps(r)))
        return True
    paid = store.write(_pay)
    if not paid:
        return {"instance_id": iid, "paid": False, "reason": "already_paid_this_round"}
    log("lane_i_paid", instance=iid[:12], owner=instance["owner_hotkey"][:8],
        price=price, closer=best_closer.commitment_id[:12], round=current_round)
    return {"instance_id": iid, "paid": True, "price": price,
            "champion_par2": champ_par2, "best_closer_par2": best_par2,
            "closer": best_closer.commitment_id, "rows_emitted": len(emitted)}


def _pending_instances(store: Store) -> list[dict]:
    return [dict(r) for r in store.query(
        "SELECT * FROM arena_instances WHERE status IN ('pending','paid') "
        "ORDER BY created_at_iso ASC")]


def arena_payout_tick(
    store: Store,
    lane: SolverArenaLane,
    *,
    champion_spec: SolverSpec,
    champion_adapter,
    closers: list[tuple[SolverSpec, object, int]],
    current_round: int,
    private_key_hex: str,
    epoch_salt: str,
    base: float | None = None,
    tier: int = _DEFAULT_TIER,
    log=lambda *a, **k: None,
) -> dict:
    """Settle every eligible pending instance for `current_round`."""
    base = _env_float("CATHEDRAL_ARENA_PAYOUT_BASE", _DEFAULT_BASE) if base is None else base
    verdicts = []
    for inst in _pending_instances(store):
        v = settle_instance(
            store, lane, inst, champion_spec=champion_spec,
            champion_adapter=champion_adapter, closers=closers,
            current_round=current_round, private_key_hex=private_key_hex,
            epoch_salt=epoch_salt, base=base, tier=tier, log=log)
        verdicts.append(v)
    paid = sum(1 for v in verdicts if v.get("paid"))
    summary = {"round": current_round, "instances_seen": len(verdicts), "paid": paid,
               "verdicts": verdicts}
    log("arena_payout_tick", round=current_round, paid=paid, seen=len(verdicts))
    return summary


async def arena_payout_loop(
    store: Store,
    lane: SolverArenaLane,
    *,
    round_source: Callable[[], int],
    champion_provider: Callable[[], "tuple[SolverSpec, object] | None"],
    closers_provider: Callable[[], list[tuple[SolverSpec, object, int]]],
    private_key_hex: str,
    epoch_salt: str,
    interval_seconds: int | None = None,
    tier: int = _DEFAULT_TIER,
    log=lambda *a, **k: None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Asyncio task: periodic Lane I settlement. Gated by the caller (only started
    when arena_payout_enabled()). The champion + closers + current round are
    pulled via providers each tick (so a newly-crowned champion / newly-cleared
    closer is picked up). Runs in a worker thread; cancels cleanly."""
    interval = interval_seconds or _env_int("CATHEDRAL_ARENA_PAYOUT_INTERVAL_SECONDS",
                                            _DEFAULT_INTERVAL_SECONDS)
    log("arena_payout_loop_start", interval=interval, tier=tier)
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                champ = champion_provider()
                if champ is not None:
                    spec, adapter = champ
                    await asyncio.to_thread(
                        arena_payout_tick, store, lane,
                        champion_spec=spec, champion_adapter=adapter,
                        closers=closers_provider(), current_round=round_source(),
                        private_key_hex=private_key_hex, epoch_salt=epoch_salt,
                        tier=tier, log=log)
            except Exception as e:
                log("arena_payout_error", error=str(e))
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(interval),
                    timeout=interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        log("arena_payout_loop_cancelled")
        raise
