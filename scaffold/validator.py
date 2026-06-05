"""The validator loop — uniform across lanes, the core's only orchestration.

    for each active lane:
        mint N challenges            (lane.mint_challenge)
        dispatch the PUBLIC problem to the miner pool   (hidden stays here)
        collect submissions
        validate + score each        (lane.validate_submission -> lane.score)
    aggregate per-hotkey scores -> normalized UID weights

Miners are a pluggable callable `MinerPool` so the loop never depends on a
transport. In the scaffold the demo supplies in-process miners (honest +
adversarial); in deployment this is the publisher's submit endpoint feeding
the same contract. Dispatch is the only place that knows about >1 lane, and it
treats them identically — adding Lane D would not touch this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .contract import GenerateCtx, Lane, PublicProblem, Submission
from . import consensus, registry, timing

# A miner pool: given a public problem, return zero or more submissions.
MinerPool = Callable[[PublicProblem], list[Submission]]


@dataclass
class GradedSubmission:
    family_id: str
    task_id: str
    miner_hotkey: str
    weighted_score: float
    outcome: str
    rejection_reason: str | None
    details: dict = field(default_factory=dict)
    consensus_flag: str = ""          # set by the cross-miner consensus pass


@dataclass
class RoundReport:
    graded: list[GradedSubmission] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)

    def by_lane(self) -> dict[str, list[GradedSubmission]]:
        out: dict[str, list[GradedSubmission]] = {}
        for g in self.graded:
            out.setdefault(g.family_id, []).append(g)
        return out


def _normalize(scores: dict[str, float]) -> dict[str, float]:
    total = sum(scores.values())
    if total <= 0:
        return {k: 0.0 for k in scores}
    return {k: round(v / total, 6) for k, v in scores.items()}


def run_round(
    miners: MinerPool,
    *,
    seed0: int,
    issued_at_iso: str,
    per_lane: int = 2,
    tier: int = 1,
    lanes: list[Lane] | None = None,
) -> RoundReport:
    """One validator round across all active lanes. Pure given `miners`."""
    lanes = lanes if lanes is not None else registry.active()
    report = RoundReport()
    hotkey_scores: dict[str, float] = {}

    seed = seed0
    for lane in lanes:
        wants_consensus = bool(getattr(lane, "supports_consensus", False))
        for _ in range(per_lane):
            ctx = GenerateCtx(seed=seed, tier=tier, issued_at_iso=issued_at_iso)
            problem, hidden = lane.mint_challenge(ctx)
            seed += 1

            # 1. per-submission verify + score (the per-miner gates).
            #    Gate on task identity and ONE scored submission per hotkey: a
            #    wrong-task answer is dropped, and a hotkey cannot multiply its
            #    weight by replaying the same solve.
            group: list[GradedSubmission] = []
            seen: set[str] = set()
            for sub in miners(problem):
                if sub.task_id != problem.task_id or sub.miner_hotkey in seen:
                    continue
                seen.add(sub.miner_hotkey)
                vr = lane.validate_submission(problem, hidden, sub)
                sr = lane.score(problem, vr,
                                wall_ms=timing.observed_wall_ms(sub.miner_hotkey, problem))
                group.append(GradedSubmission(
                    family_id=lane.family_id, task_id=problem.task_id,
                    miner_hotkey=sub.miner_hotkey, weighted_score=sr.weighted_score,
                    outcome=vr.outcome.value, rejection_reason=sr.rejection_reason,
                    details=dict(vr.details),
                ))

            # 2. cross-miner consensus pass (only for lanes that opt in). A
            #    peer's verified counterexample refutes every "safe" peer; a
            #    minority find is escalated, not punished.
            if wants_consensus and group:
                classes = [consensus.classify(g.outcome) for g in group]
                _maj, agree = consensus.agreement(classes)
                for g, cv in zip(group, consensus.resolve(classes)):
                    g.consensus_flag = cv.flag
                    g.details["consensus_agree"] = agree
                    if cv.override_score is not None:
                        g.weighted_score = cv.override_score
                        g.rejection_reason = cv.reason

            # 3. commit the (possibly consensus-adjusted) scores
            for g in group:
                report.graded.append(g)
                hotkey_scores[g.miner_hotkey] = (
                    hotkey_scores.get(g.miner_hotkey, 0.0) + g.weighted_score
                )

    report.weights = _normalize(hotkey_scores)
    return report
