"""Solver Bench — the SAT-solving-market dimension of the arena, built on the
REAL scaffold solver-arena machinery (par2_ms, run_batch, ChampionMachine). Each
published solver is benchmarked on a seeded CNF batch; results are CERTIFIED
(a witness only counts if it re-verifies), ranked by PAR-2 (SAT-competition
standard), and the fastest holds the crown. A liar solver (forged witness) earns
nothing — the cert gate, same as the live lane.

Solvers run in-process here (small instances, deterministic) for a fast per-round
benchmark; the same adapters run under scaffold.lanes.sandbox in the real lane.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from scaffold.contract import Outcome
from scaffold.dimacs import gen_planted_3sat, solve_cnf
from scaffold.lanes import sandbox
from scaffold.lanes.solver_arena import (AdapterOutput, Instance, SolverSpec,
                                         par2_ms, run_batch)

# a seeded eval batch — small, deterministic, near the m/n≈4.26 phase transition.
_BATCH = [(11, 60, 255), (22, 70, 298), (33, 80, 340)]
TIMEOUT_MS = 4000.0


def _instances() -> list[Instance]:
    out = []
    for i, (seed, n, m) in enumerate(_BATCH):
        cnf, _ = gen_planted_3sat(seed, n, m, method="ajm")
        out.append(Instance(task_id=f"bench-{i}", cnf=cnf, timeout_ms=TIMEOUT_MS))
    return out


@dataclass(frozen=True)
class SolverProfile:
    name: str
    source_sha256: str
    skill_ms: float          # added latency (a faster solver => smaller)
    honest: bool = True      # a liar returns a non-satisfying witness


def default_solvers() -> list[SolverProfile]:
    return [
        SolverProfile("kissat-port", "sha-kissat", 4.0),
        SolverProfile("cadical-port", "sha-cadical", 18.0),
        SolverProfile("toy-dpll", "sha-toydpll", 70.0),
        SolverProfile("liar-solver", "sha-liar", 2.0, honest=False),
    ]


def _adapter(profile: SolverProfile):
    def _adapt(cnf: str, timeout_ms: float) -> AdapterOutput:
        t0 = time.perf_counter()
        sol = solve_cnf(cnf) or []
        if profile.skill_ms:
            time.sleep(profile.skill_ms / 1000.0)     # simulated solver effort (MEASURED)
        wall = (time.perf_counter() - t0) * 1000.0
        if not profile.honest and sol:
            sol = [-sol[0]] + sol[1:]                 # corrupt the witness -> cert fails
        run = sandbox.RunResult(stdout="", stderr="", returncode=0, wall_ms=round(wall, 3),
                                timed_out=wall > timeout_ms, contained=False)
        return AdapterOutput(claimed=Outcome.SAT, witness=sol, drat="", run=run)
    return _adapt


@dataclass
class BenchCard:
    name: str
    commitment_id: str
    par2_ms: float
    solved: int
    total: int
    crown: bool


def run_bench(solvers: list[SolverProfile] | None = None) -> list[dict]:
    """Benchmark each solver on the batch; certify; rank by PAR-2; crown the best.
    Returns UI-ready cards (sorted best-first). Reuses the real par2/champion code."""
    solvers = solvers if solvers is not None else default_solvers()
    instances = _instances()
    tmap = {i.task_id: i.timeout_ms for i in instances}

    rows = []
    seen: set[str] = set()                                  # source-hash dedup
    for p in solvers:
        spec = SolverSpec(source_url=p.name, container_digest="sha256:" + p.source_sha256,
                          source_sha256=p.source_sha256, owner_hotkey=p.name)
        if spec.commitment_id in seen:                      # identical source => one commitment
            continue
        seen.add(spec.commitment_id)
        results = run_batch(_adapter(p), instances)         # REAL certification (witness re-check)
        rows.append({"name": p.name, "commitment_id": spec.commitment_id,
                     "par2_ms": par2_ms(results, tmap),
                     "solved": sum(1 for r in results if r.solved), "total": len(instances)})

    rows.sort(key=lambda r: r["par2_ms"])                   # PAR-2: lower is better
    for i, r in enumerate(rows):
        r["crown"] = (i == 0 and r["solved"] > 0)           # fastest certified solver holds it
    return rows


def _real_solvers():
    """Two GENUINELY DIFFERENT real SAT solvers available in-process: the scaffold's
    reference CDCL and pysat's Glucose3 (an industrial solver). Returns
    [(name, solve_fn)] where solve_fn(cnf) -> assignment | None. Glucose3 is skipped
    if pysat is absent (the scaffold solver always runs)."""
    from scaffold.dimacs import solve_cnf
    sols = [("scaffold-cdcl", solve_cnf)]
    try:
        from pysat.formula import CNF
        from pysat.solvers import Glucose3

        def _glucose(cnf: str):
            f = CNF(from_string=cnf)
            s = Glucose3(bootstrap_with=f.clauses)
            sat = s.solve()
            m = s.get_model() if sat else None
            s.delete()
            return m if sat else None

        sols.append(("glucose3", _glucose))
    except Exception:
        pass
    return sols


def real_solver_bench() -> list[dict]:
    """A REAL solver race (NO simulated latency): each genuinely-distinct real solver
    solves the SAME seeded CNF batch, wall-clock MEASURED, the witness CERTIFIED via
    verify_witness, ranked by REAL PAR-2 (unsolved/uncertified -> 2x timeout, the
    SAT-competition rule). The fastest certified solver holds the crown. This is the
    real-implementation counterpart to run_bench()'s simulated-skill cards."""
    from scaffold.dimacs import verify_witness
    instances = _instances()
    rows = []
    for name, fn in _real_solvers():
        penalties: list[float] = []
        solved = 0
        for inst in instances:
            t0 = time.perf_counter()
            try:
                sol = fn(inst.cnf)
            except Exception:
                sol = None
            wall = (time.perf_counter() - t0) * 1000.0
            certified = bool(sol) and verify_witness(inst.cnf, sol)
            if certified and wall <= inst.timeout_ms:
                penalties.append(wall)
                solved += 1
            else:
                penalties.append(2 * inst.timeout_ms)       # PAR-2 penalty
        rows.append({"name": name, "solver_kind": "REAL",
                     "par2_ms": round(sum(penalties) / len(penalties), 3),
                     "solved": solved, "total": len(instances)})
    rows.sort(key=lambda r: r["par2_ms"])                   # PAR-2: lower is better
    for i, r in enumerate(rows):
        r["crown"] = (i == 0 and r["solved"] > 0)
    return rows
