"""Offline end-to-end driver for Lane S — milestone 3.

Wires the solver arena into a full round: seed the launch champion (the SC2025
winner stand-in), register + evaluate two stub solvers and (when installed) a
real kissat/cadical container, and run the adversarial battery the design names —
liar solver, forged DRAT, copied champion, timeout fraud — asserting EVERY
adversarial case scores 0 with a reason. Pure + offline by default (stub
adapters); a real solver binary is used only when present (have_real_solver()).

This is the Lane-S analogue of demo.py: it shows the mint -> register -> eval ->
certify -> champion-state -> score pipeline against honest AND hostile solvers,
and it is the source of truth rc_verify imports for the Lane S gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from shutil import which

from ..contract import GenerateCtx, Outcome, Submission
from ..dimacs import solve_cnf
from . import sandbox
from .solver_arena import (
    AdapterOutput, Instance, SolverArenaLane, SolverSpec, run_batch,
)


def _rr(wall_ms: float, timed_out: bool = False) -> sandbox.RunResult:
    return sandbox.RunResult("", "", 0 if not timed_out else None, wall_ms, timed_out, True)


def stub_adapter(behavior: str):
    """Deterministic stub solvers for offline e2e (no binary needed).

    honest_fast / honest_slow -> real (toy-DPLL) witness at different speeds.
    liar       -> claims SAT with a non-satisfying assignment (forged witness).
    forged_unsat -> claims UNSAT with a bogus DRAT proof.
    timeout    -> host-observed timeout (an honest hard instance / abstain).
    """
    def adapt(cnf: str, timeout_ms: float) -> AdapterOutput:
        if behavior == "timeout":
            return AdapterOutput(Outcome.TIMEOUT, [], "", _rr(timeout_ms, True))
        if behavior == "liar":
            return AdapterOutput(Outcome.SAT, [1] * 500, "", _rr(40.0))
        if behavior == "forged_unsat":
            return AdapterOutput(Outcome.UNSAT, [], "0\n", _rr(45.0))
        sol = solve_cnf(cnf) or []
        wall = 120.0 if behavior == "honest_fast" else 4000.0
        return AdapterOutput(Outcome.SAT, sol, "", _rr(wall))
    return adapt


def have_real_solver() -> tuple[str, str] | None:
    """Return (name, path) for an installed kissat/cadical, else None."""
    for name in ("kissat", "cadical"):
        p = which(name)
        if p:
            return name, p
    return None


def _spec(source_hash: str, owner: str) -> SolverSpec:
    return SolverSpec(f"https://example/{source_hash}",
                      "sha256:" + (source_hash * 8)[:64], source_hash, owner)


@dataclass
class ArenaRun:
    lane: SolverArenaLane
    rows: list                # (label, outcome, score, reason, details)
    record_falls: list


def run_arena_round(seed: int = 4242, tier: int = 0, batch_size: int = 6) -> ArenaRun:
    """One full Lane S round against honest + adversarial solvers. Offline."""
    lane = SolverArenaLane(batch_size=batch_size)
    ctx = GenerateCtx(seed=seed, tier=tier, issued_at_iso="x")
    problem, hidden = lane.mint_challenge(ctx)
    instances = lane._batch_from_hidden(hidden)
    timeouts = {i.task_id: i.timeout_ms for i in instances}

    # 1. seed the launch champion (SC2025 winner stand-in): a real but unhurried
    #    solver. Registered + marked evaluated so a copy of it dedups.
    champ_spec = _spec("sc2025champion", "sc2025-author")
    champ_results = run_batch(stub_adapter("honest_slow"), instances)
    lane.seed_launch_champion(champ_spec, champ_results, timeouts)

    # 2. wire the challenger adapters by commitment id.
    real = have_real_solver()
    challengers = [
        ("honest_fast", "fast_open_solver", "honest_fast"),
        ("liar_solver", "liar_src", "liar"),
        ("forged_unsat", "forged_src", "forged_unsat"),
        ("timeout_solver", "timeout_src", "timeout"),
        ("champion_copy", "sc2025champion", None),   # same source hash as champion
    ]
    if real:
        # a genuinely fast real solver, when one is installed in this env.
        import tempfile
        from pathlib import Path
        name, path = real

        def real_adapt(cnf: str, timeout_ms: float) -> AdapterOutput:
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / "p.cnf"
                f.write_text(cnf)
                run = sandbox.run_solver([path, str(f)], timeout_s=timeout_ms / 1000.0)
            if run.timed_out:
                return AdapterOutput(Outcome.TIMEOUT, [], "", run)
            model: list[int] = []
            for line in run.stdout.splitlines():
                if line.startswith("v "):
                    model += [int(x) for x in line[2:].split()
                              if x.lstrip("-").isdigit() and int(x) != 0]
            cl = "SATISFIABLE" in run.stdout
            return AdapterOutput(Outcome.SAT if cl else Outcome.TIMEOUT, model, "", run)
        challengers.insert(0, (f"real_{name}", "real_solver_src", "__real__"))
        lane.adapters["real_solver_src"] = real_adapt

    for _label, src_hash, beh in challengers:
        if beh is not None and beh != "__real__":
            lane.adapters[src_hash] = stub_adapter(beh)

    rows = []
    falls = []
    for label, src_hash, _beh in challengers:
        sub = Submission(problem.task_id, label, {
            "source_url": f"https://example/{src_hash}",
            "container_digest": "sha256:" + (src_hash * 8)[:64],
            "source_sha256": src_hash,
        })
        vr = lane.validate_submission(problem, hidden, sub)
        sr = lane.score(problem, vr)
        if vr.details.get("record_fell"):
            falls.append(vr.details.get("record_fall"))
        rows.append((label, vr.outcome.value, sr.weighted_score,
                     sr.rejection_reason or vr.rejection_reason, vr.details))
    return ArenaRun(lane, rows, falls)


if __name__ == "__main__":
    run = run_arena_round()
    print("Lane S offline end-to-end — solver arena")
    print(f"sandbox available (network-isolated): {sandbox.sandbox_available()}")
    for label, outcome, score, reason, det in run.rows:
        tag = "OK " if score > 0 else "   "
        extra = ""
        if det.get("record_fell"):
            extra = "  <<< RECORD FELL (jackpot + burn step)"
        elif det.get("is_champion"):
            extra = "  (reigning champion)"
        print(f"  {tag}{label:18s} {outcome:8s} score={score:.4f}"
              f"  par2={det.get('par2_ms','-')}  mvbs={det.get('marginal_vbs','-')}"
              f"  {('('+reason+')') if reason else ''}{extra}")
    print(f"\nrecord falls this round: {len(run.record_falls)}")
