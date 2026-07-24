"""The publisher — the trusted referee of one round.

It owns everything the miner does not: minting each miner's unique challenges,
polling liveness, executing each miner's solver in the sandbox, measuring wall
time, verifying witnesses, and gating premium credit behind attested compute.
It trusts only (a) host-measured wall time and (b) the independent witness/quote
checks — never anything a miner asserts about itself.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from . import config
from .miner import MinerEnv
from .reward import SolveCredit

# Real scaffold primitives — imported, never reimplemented.
from scaffold.dimacs import solve_cnf
from scaffold.grading import speed_bonus
from scaffold.lanes.sandbox import run_solver
from scaffold.polaris import PolarisClient
from scaffold.publisher import per_miner as PM
from scaffold.verify import verify_attestation

REPO_ROOT = str(Path(__file__).resolve().parents[1])
RUNNER = str(Path(__file__).resolve().parent / "runner.py")


@dataclass
class SolveObservation:
    assignment: list[int]
    wall_ms: float
    timed_out: bool
    contained: bool
    reason: str = ""


class Publisher:
    def __init__(self, epoch: int = config.GAME_EPOCH):
        self.epoch = epoch
        self.polaris = PolarisClient(live=False)   # offline stub, real recipe

    # -- 1. liveness poll -----------------------------------------------------
    def poll_liveness(self, miner: MinerEnv, now_s: int) -> tuple[bool, str]:
        age = miner.heartbeat_age_s(now_s)
        if age > config.LIVENESS_WINDOW_S:
            return False, f"no_heartbeat (age={age})"
        return True, ""

    # -- 2. mint --------------------------------------------------------------
    def assign(self, hotkey: str) -> list[dict]:
        """Each live miner's unique per-epoch challenge set (descriptors only)."""
        return PM.miner_instance_set(hotkey, self.epoch)

    def cnf_for(self, hotkey: str, tier: int, seq: int) -> tuple[str, str]:
        cid, cnf = PM.get_miner_cnf(hotkey, self.epoch, tier, seq)
        return cid, cnf

    # -- 3. execute the miner's solver in the sandbox -------------------------
    def run_solver(self, miner: MinerEnv, cid: str, cnf_text: str,
                   leaked_answer: list[int] | None = None) -> SolveObservation:
        ws = miner.ensure_workspace()
        cnf_path = ws / f"{cid}.cnf"
        cnf_path.write_text(cnf_text, encoding="utf-8")

        argv = [sys.executable, RUNNER, "--cnf", str(cnf_path),
                "--behavior", miner.behavior, "--delay-ms", str(miner.delay_ms),
                "--repo", REPO_ROOT]
        if miner.behavior == "copy":
            ans_path = ws / f"{cid}.stolen"
            ans_path.write_text(" ".join(str(x) for x in (leaked_answer or [])),
                                encoding="utf-8")
            argv += ["--answer-file", str(ans_path)]

        r = run_solver(argv, timeout_s=config.SOLVE_TIMEOUT_S, cwd=REPO_ROOT)
        if r.timed_out:
            return SolveObservation([], r.wall_ms, True, r.contained, "solver_timeout")
        if r.returncode not in (0, None):
            return SolveObservation([], r.wall_ms, False, r.contained,
                                    f"solver_error rc={r.returncode}")
        assignment = _parse_v_lines(r.stdout)
        return SolveObservation(assignment, r.wall_ms, False, r.contained)

    # -- 4. attested-compute gate (tier-2 / premium) --------------------------
    def attest_gate(self, miner: MinerEnv, cid: str, wall_ms: float) -> tuple[bool, str]:
        nonce = f"nonce:{cid}"
        ok, res = verify_attestation(
            self.polaris, nonce=nonce, pubkey_b64=miner.slot.pubkey_b64,
            expected_image=miner.slot.image, workload=f"solve:{cid}",
            measured_elapsed_ms=wall_ms)
        if ok:
            return True, f"attested(stub={res.stub})"
        if not miner.slot.provisioned:
            return False, "no_provisioned_compute"
        return False, "attestation_failed"

    # -- 5. grade a full round into SolveCredits ------------------------------
    def grade_round(self, roster: list[MinerEnv], now_s: int) -> list[SolveCredit]:
        live = {m.hotkey: self.poll_liveness(m, now_s) for m in roster}
        credits: list[SolveCredit] = []

        for miner in roster:
            is_live, _ = live[miner.hotkey]
            if not is_live:
                continue   # liveness gate trips at the reward level (reward.py)
            for desc in self.assign(miner.hotkey):
                tier, seq = desc["tier"], desc["seq"]
                cid, cnf = self.cnf_for(miner.hotkey, tier, seq)

                leaked = None
                if miner.behavior == "copy" and miner.copies_from:
                    # the copier steals the victim's answer for the same slot
                    _vcid, vcnf = self.cnf_for(miner.copies_from, tier, seq)
                    leaked = solve_cnf(vcnf) or []

                obs = self.run_solver(miner, cid, cnf, leaked_answer=leaked)

                verified, vreason = self._verify(miner.hotkey, cid, tier, seq, obs.assignment)
                tier_w = PM.weight_for(tier)
                speed = speed_bonus(obs.wall_ms, config.TIER_REFERENCE_MS.get(tier, 100.0))

                if config.TIER_REQUIRES_ATTEST.get(tier, False) and verified:
                    agate, areason = self.attest_gate(miner, cid, obs.wall_ms)
                else:
                    agate, areason = True, "n/a"

                reason = vreason or (areason if not agate else "")
                credits.append(SolveCredit(
                    hotkey=miner.hotkey, challenge_id=cid, tier=tier,
                    verified=verified, attest_gate=agate,
                    tier_weight=tier_w, speed=speed, reason=reason))
        return credits

    # -- verification (deterministic, anti-copy) ------------------------------
    def _verify(self, hotkey: str, cid: str, tier: int, seq: int,
                assignment: list[int]) -> tuple[bool, str]:
        if not assignment:
            return False, "no_assignment"
        ok, reason = PM.verify_miner_submission_for(
            hotkey, self.epoch, tier, seq, cid, assignment)
        return ok, ("" if ok else (reason or "witness_check_failed"))


def _parse_v_lines(stdout: str) -> list[int]:
    lits: list[int] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("v "):
            for tok in line[2:].split():
                val = int(tok)
                if val == 0:
                    return lits
                lits.append(val)
    return lits
