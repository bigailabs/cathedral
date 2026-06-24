"""Solver Bench — PAR-2 ranking with REAL certification. The fastest certified
solver holds the crown; a liar (forged witness) earns nothing (cert gate);
identical source hashes dedup to one commitment.
"""
from __future__ import annotations

from game.arena import solverbench
from game.arena.solverbench import SolverProfile, run_bench


def test_par2_ranks_and_crowns_fastest():
    rows = run_bench()
    assert rows[0]["crown"] is True
    # PAR-2 is monotone non-decreasing down the board (lower = better)
    pars = [r["par2_ms"] for r in rows]
    assert pars == sorted(pars)
    assert rows[0]["name"] == "kissat-port"        # smallest skill latency wins


def test_liar_solver_earns_nothing():
    rows = {r["name"]: r for r in run_bench()}
    liar = rows["liar-solver"]
    assert liar["solved"] == 0                      # forged witnesses fail certification
    assert liar["crown"] is False
    assert liar["par2_ms"] >= rows["kissat-port"]["par2_ms"]   # heavily PAR-2-penalized


def test_honest_solvers_certify_all():
    rows = {r["name"]: r for r in run_bench()}
    for name in ("kissat-port", "cadical-port", "toy-dpll"):
        assert rows[name]["solved"] == rows[name]["total"]


def test_source_hash_dedup():
    # two solvers with the SAME source hash collapse to one commitment (anti-Sybil)
    twins = [SolverProfile("a", "sha-same", 5.0), SolverProfile("b", "sha-same", 5.0)]
    rows = run_bench(twins)
    assert len(rows) == 1


def test_faster_solver_has_lower_par2():
    fast = SolverProfile("fast", "sha-f", 2.0)
    slow = SolverProfile("slow", "sha-s", 120.0)
    rows = {r["name"]: r for r in run_bench([fast, slow])}
    assert rows["fast"]["par2_ms"] < rows["slow"]["par2_ms"]


# -- REAL solver race: genuinely-distinct real solvers, no simulation ----------

def test_real_solver_bench_uses_real_distinct_solvers():
    rows = solverbench.real_solver_bench()
    assert rows and all(r["solver_kind"] == "REAL" for r in rows)
    names = {r["name"] for r in rows}
    assert "scaffold-cdcl" in names                  # the scaffold solver always runs
    # every real solver certifies the whole planted-SAT batch (witness re-verified)
    for r in rows:
        assert r["solved"] == r["total"] and r["par2_ms"] > 0
    # PAR-2 monotone down the board; the lowest-PAR-2 solver holds the crown
    pars = [r["par2_ms"] for r in rows]
    assert pars == sorted(pars)
    assert rows[0]["crown"] is True
    assert sum(1 for r in rows if r["crown"]) == 1   # exactly one crown


def test_real_solver_bench_surfaced_in_console():
    from game.arena.engine import ArenaEngine
    rb = ArenaEngine().run(1).operator_console["real_solver_bench"]
    assert rb and rb[0].get("crown") is True and rb[0]["solver_kind"] == "REAL"
