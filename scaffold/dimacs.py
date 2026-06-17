"""Minimal DIMACS CNF: a seeded planted-SAT generator + an independent witness
verifier. Self-contained mirror of synthetic_boolean_v1/dimacs.py's verify path.

REAL primitive. The witness check is the correctness gate Lane A and Lane B
rely on: a faster-but-wrong solve scores 0 because this returns False.
"""
from __future__ import annotations

import random


def gen_planted_3sat(seed: int, n_vars: int, n_clauses: int,
                     method: str = "biased") -> tuple[str, list[int]]:
    """Generate a random 3-SAT CNF guaranteed satisfiable by a planted
    assignment. Returns (dimacs_text, planted_assignment as signed literals).

    Deterministic in `seed` (Rule: no unseeded randomness). The planted model
    is the publisher's hidden witness; miners never see it.

    method='biased' (default, tier1): draw a clause and flip one literal
    whenever the model fails it.  This biases clause polarities toward the
    planted model, making any-size instance easy for heuristic solvers.
    Kept as the default so tier1 is byte-identical to the pre-AJM board.

    method='ajm' (tier2): Achlioptas–Jia–Moore "two hidden assignments"
    (SAT 2005).  Each clause is drawn at random and KEPT only if the planted
    model satisfies it with exactly 1 or 2 true literals (reject all-false=0
    which breaks the planted model, and all-true=3 which breaks its bitwise
    complement).  Because kept-clause distribution is symmetric under flipping
    every sign there is NO literal-frequency bias for a solver to exploit,
    making instances genuinely hard near the phase transition (m/n ≈ 4.26).
    """
    rng = random.Random(seed)
    model = {v: rng.choice((True, False)) for v in range(1, n_vars + 1)}
    clauses: list[tuple[int, int, int]] = []
    while len(clauses) < n_clauses:
        vs = rng.sample(range(1, n_vars + 1), 3)
        lits = [v if rng.random() < 0.5 else -v for v in vs]
        if method == "ajm":
            # keep iff 1 or 2 literals are true under the planted model
            n_true = sum(1 for lit in lits if (lit > 0) == model[abs(lit)])
            if n_true == 0 or n_true == 3:
                continue
        else:
            # biased: ensure planted model satisfies clause; flip one lit if not
            if not any((lit > 0) == model[abs(lit)] for lit in lits):
                i = rng.randrange(3)
                lits[i] = -lits[i]
        clauses.append(tuple(lits))  # type: ignore[arg-type]
    header = f"p cnf {n_vars} {n_clauses}\n"
    body = "".join(f"{a} {b} {c} 0\n" for a, b, c in clauses)
    planted = [v if model[v] else -v for v in range(1, n_vars + 1)]
    return header + body, planted


def parse_cnf(text: str) -> tuple[int, list[list[int]]]:
    """Parse DIMACS into (n_vars, clauses). Total — tolerates comments."""
    n_vars = 0
    clauses: list[list[int]] = []
    cur: list[int] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] == "c":
            continue
        if line[0] == "p":
            parts = line.split()
            if len(parts) >= 3:
                n_vars = int(parts[2])
            continue
        for tok in line.split():
            lit = int(tok)
            if lit == 0:
                if cur:
                    clauses.append(cur)
                    cur = []
            else:
                cur.append(lit)
    if cur:
        clauses.append(cur)
    return n_vars, clauses


def solve_cnf(cnf_text: str) -> list[int] | None:
    """Tiny DPLL with unit propagation — enough for an honest miner to solve the
    scaffold's small planted instances. Returns a complete signed-literal
    assignment, or None if UNSAT. Not a competitive solver; a real miner brings
    cadical/kissat.

    Branching uses a deterministic literal-frequency heuristic over the
    CURRENTLY-UNSATISFIED clauses (a Jeroslow-Wang-style most-occurring-literal
    pick), trying the more-common polarity first. This stays a COMPLETE,
    deterministic DPLL (both polarities are explored on backtrack) but finds the
    planted assignment without the deep blind backtracking that made the naive
    lowest-var/True-first order exponential on the tier-2 instances. Correctness
    is unchanged — only the search order improved."""
    n_vars, clauses = parse_cnf(cnf_text)

    def dpll(assign: dict[int, bool]) -> dict[int, bool] | None:
        a = dict(assign)
        changed = True
        while changed:                          # unit propagation
            changed = False
            for cl in clauses:
                unassigned, sat = [], False
                for lit in cl:
                    v = abs(lit)
                    if v in a:
                        if a[v] == (lit > 0):
                            sat = True
                            break
                    else:
                        unassigned.append(lit)
                if sat:
                    continue
                if not unassigned:
                    return None                  # conflict
                if len(unassigned) == 1:
                    u = unassigned[0]
                    a[abs(u)] = u > 0
                    changed = True
        # pick the literal occurring most in still-unsatisfied clauses; branch on
        # its variable, trying the more-frequent polarity first (deterministic).
        score: dict[int, int] = {}
        for cl in clauses:
            if any(a.get(abs(lit)) == (lit > 0) for lit in cl):
                continue                         # clause already satisfied
            for lit in cl:
                if abs(lit) not in a:
                    score[lit] = score.get(lit, 0) + 1
        if not score:
            # no unsatisfied clause has an unassigned literal; fill any free vars.
            for v in range(1, n_vars + 1):
                a.setdefault(v, True)
            return a
        best_lit = max(score, key=lambda lit: (score[lit], -abs(lit), -lit))
        var = abs(best_lit)
        for guess in (best_lit > 0, best_lit <= 0):
            res = dpll({**a, var: guess})
            if res is not None:
                return res
        return None

    sol = dpll({})
    if sol is None:
        return None
    return [v if sol.get(v, True) else -v for v in range(1, n_vars + 1)]


def verify_witness(cnf_text: str, assignment: list[int]) -> bool:
    """Independently check a satisfying assignment. `assignment` is a list of
    signed literals (e.g. [1, -2, 3] => x1=T, x2=F, x3=T). Returns True iff
    every clause has a satisfied literal AND the assignment is complete.
    """
    n_vars, clauses = parse_cnf(cnf_text)
    val: dict[int, bool] = {}
    for lit in assignment:
        if not isinstance(lit, int) or lit == 0:
            return False                        # malformed token
        v = abs(lit)
        if v > n_vars:                          # out-of-range variable padding
            return False
        if v in val and val[v] != (lit > 0):    # self-contradictory assignment
            return False
        val[v] = lit > 0
    if len(val) != n_vars:                       # must assign EXACTLY vars 1..n
        return False
    for clause in clauses:
        if not any(val.get(abs(lit)) == (lit > 0) for lit in clause):
            return False
    return True
