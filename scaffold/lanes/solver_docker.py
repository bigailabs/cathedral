"""Lane B — solver-Docker submission, run ATTESTED.

Miners submit a solver as a Docker image; it runs in an attested TDX box. The
image digest IS the MRTD (pinned + precomputable — the Entrius pattern): a
matching MRTD in the quote proves *that exact image* produced the result.

Grading is the shared three-outcome:
  * SAT     -> witness self-verifies (verify_witness). NO attestation needed.
  * UNSAT   -> proof cert self-verifies (verify_unsat_cert). NO attestation.
  * TIMEOUT -> unfalsifiable locally, so it is the ONE outcome an attested run
               vouches for: verify_attestation binds the pinned image to a real
               TDX run that hit the limit. (grading.attestation_required)

This extends solver_improvement_v1 (the unmerged clean-reference lane): same
batch/verify shape + runner/solver score split, plus the attested-Docker
execution + UNSAT lane that the reference didn't have.
"""
from __future__ import annotations

import hashlib
import math

from ..contract import (
    GenerateCtx, HiddenMetadata, Outcome, PublicProblem, ScoreResult,
    Submission, VerifierResult,
)
from ..dimacs import gen_planted_3sat, verify_witness
from ..polaris import PolarisClient
from ..verify import verify_unsat_cert, verify_attestation, attested_elapsed_ms
from .. import grading, timing

FAMILY_ID = "solver_docker_v1"
SCHEMA_VERSION = 1
RUNNER_POOL = 0.80          # score split (from solver_improvement_v1)
SOLVER_POOL = 0.20
FLOOR = 0.7                 # correctness credit; the top 0.3 is unlocked by attested speed
_TIERS = {0: (20, 80, 60), 1: (60, 255, 300), 2: (120, 510, 1800)}


class SolverDockerLane:
    family_id = FAMILY_ID
    schema_version = SCHEMA_VERSION

    def __init__(self, polaris: PolarisClient | None = None):
        # offline-by-default client; pass a live one to hit real /v1/attest
        self.polaris = polaris or PolarisClient(live=False)

    def mint_challenge(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        n_vars, n_clauses, tl = _TIERS.get(ctx.tier, _TIERS[1])
        cnf, _ = gen_planted_3sat(ctx.seed, n_vars, n_clauses)
        task_id = hashlib.sha256(f"{FAMILY_ID}:{ctx.seed}:{ctx.tier}".encode()).hexdigest()[:32]
        problem = PublicProblem(
            task_family=FAMILY_ID, schema_version=SCHEMA_VERSION, task_id=task_id,
            difficulty_tier=ctx.tier,
            public_input={"cnf": cnf, "time_limit_seconds": tl},
            time_limit_seconds=tl,
        )
        hidden = HiddenMetadata(task_id=task_id, generator_version="planted-3sat/1",
                                hidden_payload={})
        return problem, hidden

    def validate_submission(
        self, problem: PublicProblem, hidden: HiddenMetadata, submission: Submission
    ) -> VerifierResult:
        ans = submission.answer
        image_digest = ans.get("image_digest")            # == MRTD to attest against
        claimed = ans.get("outcome")
        wall_ms = float(ans.get("wall_ms", problem.time_limit_seconds * 1000))
        if not image_digest or claimed not in ("sat", "unsat", "timeout"):
            return VerifierResult(False, Outcome.INVALID, 0.0, "malformed_submission")
        det = {"wall_ms": wall_ms, "image_digest": image_digest,
               "solver_owner": ans.get("solver_owner", submission.miner_hotkey)}

        if claimed == "sat":
            if not verify_witness(problem.public_input["cnf"], ans.get("assignment", [])):
                return VerifierResult(True, Outcome.INVALID, 0.0, "bad_witness", det)
            # OPT-IN: a runner may attest the solve run to earn a TRUSTWORTHY
            # speed bonus — the elapsed is measured by the attested image and
            # BOUND into the quote (tamper-evident), never self-reported. No
            # attestation -> verified-witness base only (speed not credited).
            if ans.get("attest_solve") and ans.get("pubkey_b64"):
                att_ok, res = verify_attestation(
                    self.polaris, nonce=ans.get("nonce", problem.task_id),
                    pubkey_b64=ans.get("pubkey_b64", ""), expected_image=image_digest,
                    workload=f"solve {problem.task_id}",
                    measured_elapsed_ms=ans.get("solve_elapsed_ms"))
                if att_ok:
                    det["attested"] = True
                    det["attested_elapsed_ms"] = attested_elapsed_ms(res)
            return VerifierResult(True, Outcome.SAT, 1.0, None, det)

        if claimed == "unsat":
            chk = verify_unsat_cert(problem.public_input["cnf"], ans.get("drat", ""))
            det["unsat_cert_stub"] = chk.stub
            # credit UNSAT only on a REAL cert check. The current checker is a
            # stub (shape-only), so a stub-pass earns NOTHING — otherwise a fake
            # "0\n" proof would score 1.0 (it must not).
            if chk.ok and not chk.stub:
                return VerifierResult(True, Outcome.UNSAT, 1.0, None, det)
            reason = "unsat_cert_unverified_stub" if chk.stub else chk.reason
            return VerifierResult(True, Outcome.INVALID, 0.0, reason, det)

        # timeout: vouch via attestation that THIS image produced this result in
        # a genuine TDX box. Binding rides on report_data (image||result), not
        # MRTD (which measures the base VM, proven invariant 2026-06-04).
        att_ok, res = verify_attestation(
            self.polaris, nonce=ans.get("nonce", problem.task_id),
            pubkey_b64=ans.get("pubkey_b64", ""), expected_image=image_digest,
            workload=f"solve {problem.task_id} to {problem.time_limit_seconds}s",
        )
        det.update({"attested": att_ok, "attest_stub": res.stub,
                    "bound_image": res.image_digest})
        return VerifierResult(True, Outcome.TIMEOUT, 0.0,
                              None if att_ok else "attestation_failed", det)

    def score(self, problem: PublicProblem, verifier: VerifierResult,
              *, wall_ms: float | None = None) -> ScoreResult:
        # A verified solve earns a CORRECTNESS FLOOR; the rest of the credit is
        # unlocked by proving you were FAST — and speed is credited ONLY from an
        # ATTESTED elapsed (bound in the quote, tamper-evident), never a self-
        # reported wall_ms or the validator's clock (a remote solver's runtime is
        # only trustworthy via its own quote). So attesting can only ever HELP:
        #   no attestation         -> FLOOR (correctness proven, speed unproven)
        #   attested + slow         -> ~FLOOR
        #   attested + fast         -> up to 1.0
        d = verifier.details
        if verifier.parsed_ok and verifier.outcome in (Outcome.SAT, Outcome.UNSAT) \
                and math.isfinite(verifier.raw_metric) and verifier.raw_metric > 0:
            att = d.get("attested_elapsed_ms")
            speed = (grading.speed_bonus(att, timing.reference_ms(problem))
                     if att is not None else 0.0)
            score = round(min(1.0, FLOOR + (1.0 - FLOOR) * speed), 6)
            parts = {"base": 1.0, "speed": speed,
                     "attested": 1.0 if att is not None else 0.0,
                     "runner_share": round(score * RUNNER_POOL, 6),
                     "solver_share": round(score * SOLVER_POOL, 6)}
            return ScoreResult(score, None, parts)
        # timeout / invalid -> grade carries the attest policy + reason (score 0)
        sr = grading.grade(
            verifier, wall_ms=0.0, time_limit_ms=problem.time_limit_seconds * 1000,
            attested_ok=d.get("attested"), speed_aware=False,
        )
        # runner/solver split recorded for the publisher's reward layer
        parts = dict(sr.score_parts)
        parts["runner_share"] = round(sr.weighted_score * RUNNER_POOL, 6)
        parts["solver_share"] = round(sr.weighted_score * SOLVER_POOL, 6)
        return ScoreResult(sr.weighted_score, sr.rejection_reason, parts)
