"""End-to-end scaffold run: mint -> submit -> grade -> score -> weights, across
all three lanes, with honest and adversarial miners so the gates are visible.

    python -m scaffold.demo

Offline by default (PolarisClient(live=False) -> deterministic stub attestations
so it runs with zero deps / no network). Set POLARIS_LIVE=1 + POLARIS_URL +
POLARIS_KEY to route the Lane-B attestation through the real POST /v1/attest.
"""
from __future__ import annotations

import os

from .contract import PublicProblem, Submission
from .dimacs import solve_cnf
from .lanes.encoding import _find_counterexample
from .polaris import PolarisClient
from . import registry, validator

ISSUED_AT = "2026-06-04T00:00:00.000Z"
GOOD_IMAGE = "sha256:goodsolver"          # the pinned MRTD an honest miner runs


def _miners(problem: PublicProblem) -> list[Submission]:
    """In-process miner pool. Honest + adversarial submissions per lane. The
    pool sees only the PUBLIC problem — never HiddenMetadata."""
    fam = problem.task_family
    pi = problem.public_input
    subs: list[Submission] = []

    if fam == "sat_challenge_v1":
        sol = solve_cnf(pi["cnf"]) or []
        subs.append(Submission(problem.task_id, "honest_solver",
                               {"assignment": sol, "wall_ms": 50}))
        subs.append(Submission(problem.task_id, "slow_solver",
                               {"assignment": sol, "wall_ms": pi.get("n_clauses", 200) * 1000}))
        subs.append(Submission(problem.task_id, "liar",
                               {"assignment": [1] * pi["n_vars"], "wall_ms": 1}))

    elif fam == "solver_docker_v1":
        sol = solve_cnf(pi["cnf"]) or []
        subs.append(Submission(problem.task_id, "attested_runner",
                               {"image_digest": GOOD_IMAGE, "outcome": "sat",
                                "assignment": sol, "wall_ms": 80,
                                "solver_owner": "solver_authorA"}))
        # honest "this was genuinely hard for my solver" — attested timeout
        subs.append(Submission(problem.task_id, "honest_timeout",
                               {"image_digest": GOOD_IMAGE, "outcome": "timeout",
                                "nonce": problem.task_id, "pubkey_b64": "AA=="}))
        # fraud: claims timeout but cannot attest the pinned image ran
        subs.append(Submission(problem.task_id, "timeout_fraud",
                               {"image_digest": "sha256:unattestable", "outcome": "timeout",
                                "nonce": problem.task_id, "pubkey_b64": ""}))

    elif fam == "encoding_v1":
        tid = problem.task_id
        c = pi["contract"]
        w = pi["width"]
        cex = _find_counterexample(c["mutation"], w, c["bugx"])
        # sharp_encoder: faithful, does the work -> finds the bug if there is one
        if cex is not None:
            subs.append(Submission(tid, "sharp_encoder",
                                   {"verdict": "bug", "counterexample": cex, "encode": "faithful"}))
        else:
            subs.append(Submission(tid, "sharp_encoder",
                                   {"verdict": "safe", "encode": "faithful", "solved": True}))
        # crier: cries bug with a non-violating counterexample (caught per-submission)
        subs.append(Submission(tid, "crier",
                               {"verdict": "bug", "counterexample": 0, "encode": "faithful"}))
        # vacuous clique: AGREE on "safe" with an unsound encode -> collective
        # blindness; consensus alone would pass them, the equivalence gate won't
        for who in ("vacuous_a", "vacuous_b", "vacuous_c"):
            subs.append(Submission(tid, who,
                                   {"verdict": "safe", "encode": "vacuous", "solved": True}))
        # missed_safe: SOUND encode (passes equivalence) but says "safe" -> an
        # incomplete miss. Equivalence gate + traps pass it; only a peer's
        # verified counterexample (consensus) refutes it.
        subs.append(Submission(tid, "missed_safe",
                               {"verdict": "safe", "encode": "faithful", "solved": True}))

    return subs


def _print_report(report: validator.RoundReport) -> None:
    for fam, rows in report.by_lane().items():
        print(f"\n=== lane: {fam} ===")
        for r in rows:
            tag = "OK " if r.weighted_score > 0 else "   "
            reason = f"  ({r.rejection_reason})" if r.rejection_reason else ""
            extra = ""
            if "attested" in r.details:
                extra = f"  attested={r.details['attested']} stub={r.details.get('attest_stub')}"
            if r.consensus_flag:
                extra += f"  consensus={r.consensus_flag}"
            if "consensus_agree" in r.details:
                extra += f"(agree={r.details['consensus_agree']})"
            if r.details.get("is_trap"):
                extra += "  [TRAP]"
            print(f"  {tag}{r.miner_hotkey:18s} {r.outcome:7s} "
                  f"score={r.weighted_score:.4f}{reason}{extra}")
    print("\n=== normalized UID weights (what the validator sets on-chain) ===")
    for hk, w in sorted(report.weights.items(), key=lambda kv: -kv[1]):
        print(f"  {hk:18s} {w:.4f}")


def main() -> None:
    registry.install_default_lanes()
    live = os.environ.get("POLARIS_LIVE") == "1"
    if live:
        client = PolarisClient(live=True, base_url=os.environ.get("POLARIS_URL", ""),
                               api_key=os.environ.get("POLARIS_KEY", ""))
        # rebind the attested lane to the live client
        from .lanes.solver_docker import SolverDockerLane
        registry._REGISTRY["solver_docker_v1"] = SolverDockerLane(client)

    print("Cathedral scaffold — lanes:", [l.family_id for l in registry.active()])
    print(f"attestation: {'LIVE /v1/attest' if live else 'offline stub (POLARIS_LIVE=1 for real)'}")

    # seed0/per_lane chosen so the encoding lane's slice (seeds 13-15) hits a
    # SAFE instance, a buggy TRAP, and an ordinary bug in ONE round — i.e. every
    # faithfulness-gate path fires. Any seeds work; these just exercise them all.
    report = validator.run_round(_miners, seed0=7, issued_at_iso=ISSUED_AT,
                                 per_lane=3, tier=0)
    _print_report(report)

    print("\n--- map: real vs stub ---")
    print("  REAL : lane contract, registry, validator loop, 3-outcome grading,")
    print("         SAT witness check + DPLL solve, encoding faithfulness gates")
    print("         (equivalence/vacuity + windowed traps), CROSS-MINER CONSENSUS,")
    print("         attest-timeouts-only policy, solver pinning, weight normalize.")
    print("  STUB : UNSAT cert (shape-check; real = drat-trim), offline attest")
    print("         quote (real = POST /v1/attest on Stitch), miner transport")
    print("         (in-process; real = publisher submit endpoint), credit ledger")
    print("         (PolarisClient -> /api/billing, not exercised in this round).")


if __name__ == "__main__":
    main()
