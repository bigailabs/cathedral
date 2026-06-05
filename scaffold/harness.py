"""Tripartite e2e harness — the live validator test rig.

N sim miners share ONE on-chain hotkey but carry distinct WORKER-IDs. The chain
sees one identity; the validator's grading / consensus / dashboard see N workers
(so consensus and per-worker scoring exercise for real under a single registered
hotkey — no per-HK registration cost).

Runs R rounds against the (read-only) live netuid metagraph, grading honest +
exploit workers across all 3 lanes, accumulating growth, and computing the
weight vector each round. Broadcast is GATED (chain.ChainClient) and hard-refused
on production netuid 39 — DRY-RUN by default. Lane B routes a CAPPED number of
real /v1/attest calls (cost tracked), then falls back to the offline stub.

Run:  python -m scaffold.harness            # dry-run vs live 39 metagraph
Env:  ATTEST_KEY=...    -> enables capped real /v1/attest for Lane B
      TRIPARTITE_NETUID -> default 39 (read-only); broadcast still refused on 39
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

from .contract import PublicProblem, Submission
from .dimacs import solve_cnf
from .solve_real import solve_cnf_real, have_solver
from .lanes.encoding import _find_counterexample


def _solve(cnf: str) -> list[int]:
    """Sim miners solve with the real solver (cryptominisat5) when present,
    falling back to the toy DPLL only where no real solver is installed."""
    if have_solver():
        r = solve_cnf_real(cnf)
        if r.get("status") == "SAT":
            return r["model"]
    return solve_cnf(cnf) or []
from .lanes.solver_docker import SolverDockerLane
from .polaris import AttestResult, PolarisClient
from . import chain, registry, validator

SHARED_HOTKEY = os.environ.get("TRIPARTITE_HOTKEY", "sim-shared-hk(one-registered-identity)")
NETUID = int(os.environ.get("TRIPARTITE_NETUID", "39"))
NETWORK = os.environ.get("TRIPARTITE_NETWORK", "finney")
PULLABLE_IMAGE = "alpine:3.20"          # a real image so live /v1/attest can pull+run

# 14 workers (1 shared hotkey), honest + exploit, across all 3 lanes
ROSTER = [
    ("A-honest-1", "sat_challenge_v1", "honest"),
    ("A-honest-2", "sat_challenge_v1", "honest"),
    ("A-slow-3", "sat_challenge_v1", "slow"),
    ("A-liar-4", "sat_challenge_v1", "liar"),                 # exploit
    ("B-runner-1", "solver_docker_v1", "runner"),
    ("B-runner-2", "solver_docker_v1", "runner"),
    ("B-timeout-3", "solver_docker_v1", "honest_timeout"),
    ("B-fraud-4", "solver_docker_v1", "timeout_fraud"),       # exploit
    ("C-sharp-1", "encoding_v1", "sharp"),
    ("C-sharp-2", "encoding_v1", "sharp"),
    ("C-crier-3", "encoding_v1", "crier"),                    # exploit
    ("C-vacuous-4", "encoding_v1", "vacuous"),                # exploit
    ("C-vacuous-5", "encoding_v1", "vacuous"),                # exploit
    ("C-missed-6", "encoding_v1", "missed"),                  # exploit (overfit/miss)
]


class CappedAttest:
    """Real /v1/attest up to `cap` calls (cost tracked), then the offline stub."""

    def __init__(self, cap: int):
        key = os.environ.get("ATTEST_KEY", "")
        self.live = (PolarisClient(live=True, base_url="https://next.polaris.computer",
                                   api_key=key) if key else None)
        self.stub = PolarisClient(live=False)
        self.cap = cap
        self.calls = 0
        self.live_ok = 0
        self.cost = 0.0

    def attest(self, **kw) -> AttestResult:
        if self.live and self.calls < self.cap:
            self.calls += 1
            try:
                r = self.live.attest(**kw)
                self.cost += r.cost_usd
                if r.intel_verified:
                    self.live_ok += 1
                return r
            except Exception:
                return AttestResult(False, b"\x00" * 64, "", "", 0.0, False)
        return self.stub.attest(**kw)


def _submission(worker, problem: PublicProblem) -> Submission | None:
    wid, _lane, beh = worker
    tid, pi = problem.task_id, problem.public_input
    fam = problem.task_family
    if fam == "sat_challenge_v1":
        sol = _solve(pi["cnf"])
        if beh == "honest":
            return Submission(tid, wid, {"assignment": sol, "wall_ms": 40})
        if beh == "slow":
            return Submission(tid, wid, {"assignment": sol, "wall_ms": pi.get("n_clauses", 200) * 1000})
        if beh == "liar":
            return Submission(tid, wid, {"assignment": [1] * pi["n_vars"], "wall_ms": 1})
    if fam == "solver_docker_v1":
        sol = _solve(pi["cnf"])
        if beh == "runner":
            sub = {"image_digest": PULLABLE_IMAGE, "outcome": "sat",
                   "assignment": sol, "solver_owner": wid}
            if wid.endswith("1"):       # runner-1 attests its solve -> trustworthy speed bonus
                sub.update({"attest_solve": True, "nonce": os.urandom(8).hex(),
                            "pubkey_b64": base64.b64encode(os.urandom(32)).decode(),
                            "solve_elapsed_ms": pi.get("n_clauses", 200) * 1.5})
            return Submission(tid, wid, sub)
        if beh == "honest_timeout":
            return Submission(tid, wid, {"image_digest": PULLABLE_IMAGE, "outcome": "timeout",
                                         "nonce": os.urandom(16).hex(),
                                         "pubkey_b64": base64.b64encode(os.urandom(32)).decode()})
        if beh == "timeout_fraud":
            return Submission(tid, wid, {"image_digest": "sha256:unattestable", "outcome": "timeout",
                                         "nonce": tid, "pubkey_b64": ""})
    if fam == "encoding_v1" and pi.get("backend") == "z3":
        from scaffold.lanes import encoding_real as ER
        width, mutation = pi["width"], pi["mutation"]
        tg = pi["trigger"]
        # honest miner encodes the PUBLIC contract (incl. the trigger gate) and
        # SOLVES it with z3 — the witness is computed, never guessed.
        cexr = ER.check(width, mutation, tg["m1"], tg["m2"], tg["k"], tg["t"]).get("counterexample")
        if beh == "sharp":
            # sharp-2 reports a slower solve than sharp-1 so the speed term
            # actually separates two correct finders.
            wall = 25.0 if wid.endswith("1") else 110.0
            return (Submission(tid, wid, {"verdict": "bug", "counterexample": cexr,
                                          "encode": "faithful", "wall_ms": wall})
                    if cexr is not None else
                    Submission(tid, wid, {"verdict": "safe", "encode": "faithful", "solved": True}))
        if beh == "crier":
            # blind guess: a constant witness no longer satisfies the trigger.
            return Submission(tid, wid, {"verdict": "bug", "counterexample": 0,
                                         "encode": "faithful", "wall_ms": 1.0})
        if beh == "vacuous":
            return Submission(tid, wid, {"verdict": "safe", "encode": "vacuous", "solved": True})
        if beh == "missed":
            return Submission(tid, wid, {"verdict": "safe", "encode": "faithful", "solved": True})
        return None
    if fam == "encoding_v1":
        c, w = pi["contract"], pi["width"]
        cex = _find_counterexample(c["mutation"], w, c["bugx"])
        if beh == "sharp":
            return (Submission(tid, wid, {"verdict": "bug", "counterexample": cex, "encode": "faithful"})
                    if cex is not None else
                    Submission(tid, wid, {"verdict": "safe", "encode": "faithful", "solved": True}))
        if beh == "crier":
            return Submission(tid, wid, {"verdict": "bug", "counterexample": 0, "encode": "faithful"})
        if beh == "vacuous":
            return Submission(tid, wid, {"verdict": "safe", "encode": "vacuous", "solved": True})
        if beh == "missed":
            return Submission(tid, wid, {"verdict": "safe", "encode": "faithful", "solved": True})
    return None


def run(rounds: int = 5, per_lane: int = 2, attest_cap: int = 2,
        out: Path = Path("data/harness_state.json")) -> dict:
    registry._REGISTRY.clear()
    registry.install_default_lanes()
    cap = CappedAttest(attest_cap)
    registry._REGISTRY["solver_docker_v1"] = SolverDockerLane(cap)   # capped live attest

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    broadcast = os.environ.get("CATHEDRAL_BROADCAST") == "1"
    prov = chain.make_provenance(validator_label="tripartite-vali", hotkey=SHARED_HOTKEY,
                                 netuid=NETUID, network=NETWORK, started_at_iso=started,
                                 broadcast=broadcast)
    cc = chain.ChainClient(netuid=NETUID, network=NETWORK, hotkey=SHARED_HOTKEY, broadcast=broadcast)
    meta = cc.read_metagraph()
    our_uid = None
    if meta.get("available") and SHARED_HOTKEY in meta.get("hotkeys", []):
        our_uid = meta["hotkeys"].index(SHARED_HOTKEY)

    def miners(problem: PublicProblem) -> list[Submission]:
        return [s for s in (_submission(w, problem) for w in ROSTER if w[1] == problem.task_family) if s]

    cum: dict[str, float] = {}
    lane_counts: dict[str, int] = {}
    gates: dict[str, int] = {}
    cflags: dict[str, int] = {}
    growth: list[dict] = []
    seed = 1000
    for rnd in range(rounds):
        rep = validator.run_round(miners, seed0=seed, issued_at_iso=started,
                                  per_lane=per_lane, tier=0)
        seed += 100
        for g in rep.graded:
            cum[g.miner_hotkey] = cum.get(g.miner_hotkey, 0.0) + g.weighted_score
            lane_counts[g.family_id] = lane_counts.get(g.family_id, 0) + 1
            if g.rejection_reason:
                gates[g.rejection_reason] = gates.get(g.rejection_reason, 0) + 1
            if g.consensus_flag:
                cflags[g.consensus_flag] = cflags.get(g.consensus_flag, 0) + 1
        growth.append({"round": rnd + 1, "graded_total": sum(lane_counts.values()),
                       "earning_workers": sum(1 for v in cum.values() if v > 0)})

    wv = cc.map_weights(cum, meta, our_uid=our_uid)
    submit = cc.set_weights(wv)
    state = {
        "guard": prov.banner(), "provenance": prov.__dict__, "metagraph": meta,
        "rounds": rounds, "workers": len(ROSTER),
        "per_worker_score": dict(sorted(cum.items(), key=lambda kv: -kv[1])),
        "lane_throughput": lane_counts, "gates_fired": gates, "consensus_flags": cflags,
        "attest": {"cap": attest_cap, "live_calls": cap.calls, "live_verified": cap.live_ok,
                   "cost_usd": round(cap.cost, 6)},
        "weight_vector_by_worker": wv.nonzero(), "weight_vector_on_chain_uids": wv.by_uid,
        "submit_result": submit, "growth": growth,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, indent=2))
    return state


if __name__ == "__main__":
    import sys
    r = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    st = run(rounds=r)
    print(st["guard"])
    print("submit:", st["submit_result"])
    print("wrote data/harness_state.json")
