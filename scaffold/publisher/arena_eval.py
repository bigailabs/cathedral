"""Lane S — arena eval loop + result→signed-row wiring (TASK 1).

The engine (scaffold.lanes.solver_arena) is adversarially tested but nothing
RUNS it on a cadence or turns a record-fall into signed feed rows. This module
is that wiring, mirroring refill.refill_loop's env-gated asyncio pattern:

  * arena_eval_tick — take the registered PENDING solvers, build a fresh hidden
    instance batch in the sandbox, run each through SolverArenaLane (which
    certifies EVERY result via run_batch and runs the ChampionMachine), and on a
    RecordFall emit a signed v6 row crediting the NEW champion's owner hotkey.
  * arena_eval_loop — periodic asyncio task, gated by CATHEDRAL_ARENA_EVAL_ENABLED
    (default OFF, like refill). gen + solver runs happen in a worker thread so the
    event loop is never blocked.

Certificate-gating + the adversarial guarantees are NOT re-implemented here: they
live in solver_arena.run_batch / ChampionMachine and are exercised by
arena_e2e's battery (liar / forged-DRAT / copied-champion / timeout-fraud all
score 0). This module only DRIVES that engine and serializes its record-falls
into the frozen row surface — so a forged solver can never be the source of a
paid row, because validate_submission already scored it 0 / no record-fall.

The adapter (the actual run capability for a registered solver) is resolved by a
caller-supplied `adapter_for(spec) -> SolverAdapter | None` so prod can plug a
real container runner while the gate injects deterministic stubs. A solver with
no resolvable adapter is skipped (no_eval_adapter), never crashes the tick.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Callable

from ..contract import GenerateCtx, Outcome, Submission
from ..lanes.solver_arena import SolverArenaLane, SolverSpec
from . import rows
from .store import Store, new_uuid

_FAMILY = "synthetic_boolean_v1"  # the FROZEN row task_type (rows.TASK_TYPE)
_DEFAULT_INTERVAL_SECONDS = 60
_DEFAULT_TIER = 0
_DEFAULT_BATCH_SIZE = 6

AdapterResolver = Callable[[SolverSpec], "object | None"]


def arena_eval_enabled() -> bool:
    return os.environ.get("CATHEDRAL_ARENA_EVAL_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def build_arena_rows(
    *,
    owner_hotkey: str,
    commitment_id: str,
    tier: int,
    weighted_score: float,
    par2_ms: float,
    champion_par2_ms: float,
    ran_at: str,
    epoch_salt: str,
    private_key_hex: str,
    row_uuid: str | None = None,
) -> list[dict]:
    """Serialize an arena record-fall into the frozen v6 (+v5compat) row shape,
    crediting the NEW champion's owner hotkey. The arena score rides in
    weighted_score / challenge_value exactly like a Lane A solve row, so released
    validators (schema_version IN (5,6)) score it with no change — Lane S records
    are just row VALUES we control (V4-DESIGN.md → Migration).

    answer_hash / verifier_details_hash bind the commitment + the certified PAR-2
    metrics so the row is auditable back to the eval that produced it."""
    row_uuid = row_uuid or new_uuid()
    answer_hash = rows._hash16(f"arena:{commitment_id}")
    answer_hash = (answer_hash * 4)[:64]
    vd = rows._hash16(f"par2:{par2_ms}:champ:{champion_par2_ms}")
    vd = (vd * 4)[:64]
    return rows.build_solve_rows(
        row_uuid=row_uuid,
        miner_hotkey=owner_hotkey,
        agent_id=new_uuid(),
        challenge_id=f"arena:{commitment_id}",
        tier=tier,
        weighted_score=weighted_score,
        answer_hash=answer_hash,
        verifier_details_hash=vd,
        ran_at=ran_at,
        epoch_salt=epoch_salt,
        solve_rank=1,            # the champion is rank 1 by definition
        solved=True,
        private_key_hex=private_key_hex,
    )


def _pending_solvers(store: Store) -> list[dict]:
    return [dict(r) for r in store.query(
        "SELECT source_sha256, source_url, container_digest, owner_hotkey "
        "FROM arena_solvers WHERE status='pending' ORDER BY created_at_iso ASC")]


def arena_eval_tick(
    store: Store,
    lane: SolverArenaLane,
    *,
    adapter_for: AdapterResolver,
    private_key_hex: str,
    epoch_salt: str,
    seed: int,
    tier: int = _DEFAULT_TIER,
    log=lambda *a, **k: None,
) -> dict:
    """One eval tick. Returns a summary. Pure given (store, lane, adapter_for):
    no clock/env read beyond the row timestamp. Champion state persists on `lane`
    across ticks (the validator constructs ONE lane and reuses it).

    For every pending solver: resolve its adapter, run it through the lane (which
    certifies the batch + runs the champion machine), and on a record-fall emit
    the signed rows + promote the new champion in the DB. A solver with no adapter
    is left pending (so a later tick with the runner available can score it)."""
    ctx = GenerateCtx(seed=seed, tier=tier, issued_at_iso=_now_iso())
    problem, hidden = lane.mint_challenge(ctx)
    pending = _pending_solvers(store)
    evaluated = 0
    falls = 0
    emitted_rows = 0
    for s in pending:
        spec = SolverSpec(s["source_url"], s["container_digest"], s["source_sha256"],
                          owner_hotkey=s["owner_hotkey"])
        adapter = adapter_for(spec)
        if adapter is None:
            log("arena_no_adapter", commitment=spec.commitment_id[:12])
            continue
        # wire the run capability for this commitment, then score it.
        lane.adapters[spec.commitment_id] = adapter
        sub = Submission(problem.task_id, spec.owner_hotkey, {
            "source_url": spec.source_url,
            "container_digest": spec.container_digest,
            "source_sha256": spec.source_sha256,
        })
        vr = lane.validate_submission(problem, hidden, sub)
        sr = lane.score(problem, vr)
        evaluated += 1
        det = vr.details
        fall = det.get("record_fall") if det.get("record_fell") else None
        # mark this commitment evaluated in the DB regardless of outcome — a
        # non-dethroning challenger doesn't get re-run (one-eval-per-commitment,
        # mirrored from the registry which validate_submission already updated).
        ran_at = _now_iso()
        if fall is not None:
            falls += 1
            rows_out = build_arena_rows(
                owner_hotkey=spec.owner_hotkey, commitment_id=spec.commitment_id,
                tier=tier, weighted_score=sr.weighted_score,
                par2_ms=float(det.get("par2_ms", 0.0)),
                champion_par2_ms=float(det.get("champion_par2_ms", float("inf"))),
                ran_at=ran_at, epoch_salt=epoch_salt, private_key_hex=private_key_hex)

            def _promote(conn, spec=spec, rows_out=rows_out):
                # demote any prior champion, crown the new one.
                conn.execute("UPDATE arena_solvers SET status='retired' "
                             "WHERE status='champion'")
                conn.execute("UPDATE arena_solvers SET status='champion' "
                             "WHERE source_sha256=?", (spec.commitment_id,))
                for r in rows_out:
                    conn.execute(
                        "INSERT OR IGNORE INTO eval_runs "
                        "(id, ran_at, eval_output_schema_version, miner_hotkey, "
                        "task_type, row_json) VALUES (?, ?, ?, ?, ?, ?)",
                        (r["id"], r["ran_at"], int(r["eval_output_schema_version"]),
                         r["miner_hotkey"], r["task_type"], json.dumps(r)))
            store.write(_promote)
            emitted_rows += len(rows_out)
            log("arena_record_fall", new_champion=spec.commitment_id[:12],
                owner=spec.owner_hotkey[:8], score=sr.weighted_score,
                par2=det.get("par2_ms"))
        else:
            # certified-but-no-dethrone (or scored 0 / adversarial): the solver is
            # done being evaluated; move it out of pending so it isn't re-run.
            def _evald(conn, spec=spec):
                conn.execute("UPDATE arena_solvers SET status='evaluated' "
                             "WHERE source_sha256=? AND status='pending'",
                             (spec.commitment_id,))
            store.write(_evald)
    summary = {"evaluated": evaluated, "record_falls": falls,
               "rows_emitted": emitted_rows, "pending_seen": len(pending),
               "champion": lane.champion.champion.commitment_id}
    log("arena_eval_tick", summary=summary)
    return summary


async def arena_eval_loop(
    store: Store,
    lane: SolverArenaLane,
    *,
    adapter_for: AdapterResolver,
    private_key_hex: str,
    epoch_salt: str,
    seed_base: int = 0,
    tier: int = _DEFAULT_TIER,
    interval_seconds: int | None = None,
    log=lambda *a, **k: None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Asyncio task: periodic arena eval. Gated by the caller (only started when
    arena_eval_enabled()). The tick (gen + sandboxed solver runs) runs in a
    worker thread so the event loop is never blocked. Cancels cleanly. Each tick
    advances the seed so successive ticks draw FRESH hidden batches (competition
    conditions: a solver can't be tuned to a known batch)."""
    interval = interval_seconds or _env_int("CATHEDRAL_ARENA_EVAL_INTERVAL_SECONDS",
                                            _DEFAULT_INTERVAL_SECONDS)
    log("arena_eval_loop_start", interval=interval, tier=tier)
    n = 0
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                await asyncio.to_thread(
                    arena_eval_tick, store, lane,
                    adapter_for=adapter_for, private_key_hex=private_key_hex,
                    epoch_salt=epoch_salt, seed=seed_base + n, tier=tier, log=log)
            except Exception as e:  # never let one bad tick kill the loop
                log("arena_eval_error", error=str(e))
            n += 1
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(interval),
                    timeout=interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        log("arena_eval_loop_cancelled")
        raise
