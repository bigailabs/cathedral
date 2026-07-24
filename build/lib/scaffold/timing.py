"""Server-measured wall-clock — the validator's clock, never the miner's word.

Reward tracks SPEED, but a miner-supplied wall_ms is self-reported and trivially
faked (claim 1ms, win every race). The only trustworthy timings are measured by
the VERIFIER, not declared by the prover:

  * the validator's own receipt clock (first-valid-wins by arrival order), or
  * an attested runner's elapsed, bound into its TDX quote (report_data).

The live loop has no real network/solve latency to measure, so this models the
validator's OBSERVED per-submission latency from the worker's rig + the problem
size — a stand-in for that receipt/attested elapsed. The property that survives
the simulation: the number fed to scoring is computed HERE, server-side, from
the worker identity + the public problem. NOTHING in submission.answer can
change it, so a miner cannot self-report a fast time to win.
"""
from __future__ import annotations

import hashlib

from .contract import PublicProblem


def _size(problem: PublicProblem) -> float:
    pi = problem.public_input
    return float(pi.get("n_clauses") or pi.get("width") or 32)


def reference_ms(problem: PublicProblem) -> float:
    """The challenge's expected (average-rig) solve time — the half-credit point
    of the speed curve. A solve faster than this scores >0.5, slower <0.5, so
    speed reward spreads across the field instead of saturating near the hard
    time limit. Scales with problem size."""
    return _size(problem) * 4.0


def observed_wall_ms(hotkey: str, problem: PublicProblem) -> float:
    """The validator's measured wall-clock for this worker's submission. In
    production this is the receipt clock or the attested elapsed; here it is
    derived from the worker's rig + problem size, server-side and untamperable."""
    h = int(hashlib.sha256(hotkey.encode()).hexdigest()[:8], 16)
    rig = 0.4 + (h % 1000) / 1000.0           # per-rig speed factor in [0.4, 1.4)
    if "slow" in hotkey:                       # a genuinely slow rig, as observed
        rig *= 25.0
    return round(_size(problem) * 4.0 * rig, 1)   # ms the validator clock observed
