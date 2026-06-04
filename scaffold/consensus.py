"""Cross-miner consensus — SHARED core logic (not per-lane).

A bug-finding lane cannot cheaply self-verify a NEGATIVE claim ("safe / no bug")
— proving safety means solving the whole bounded instance. Consensus is the
cheap cross-check: a single self-verifying COUNTEREXAMPLE is ground truth that
refutes every peer "safe" claim. So across the miners on one challenge:

  * if ANY miner produced a verified find -> every "safe" peer is an OUTLIER and
    is dropped; the finder(s) are ESCALATED. A real minority find beats a blind
    majority — naive majority voting would punish the one correct miner.
  * if NO miner produced a verified find -> the round is unanimous-none and
    consensus has nothing to flag. Collective blindness (everyone agreeing on a
    vacuous/unsound encode) is INVISIBLE to consensus and must be caught by the
    lane's equivalence gate + windowed traps.

That asymmetry is the point: consensus catches cross-miner DISAGREEMENT; the
equivalence gate catches collective AGREEMENT on an unsound encode. Neither
alone is sufficient.
"""
from __future__ import annotations

from dataclasses import dataclass

from .contract import Outcome

# how a graded outcome maps onto the consensus classes
FIND = "find"          # a self-verified counterexample (ground truth)
NONE = "none"          # a "no bug / safe" claim (only as good as the work behind it)
INVALID = "invalid"    # malformed / already-rejected; carries no consensus signal


@dataclass
class ConsensusVerdict:
    override_score: float | None     # None -> keep the lane's score unchanged
    reason: str | None
    flag: str                        # find | outlier | unanimous_none | agree | n/a


def classify(outcome: str) -> str:
    if outcome == Outcome.SAT.value:
        return FIND
    if outcome in (Outcome.UNSAT.value, Outcome.TIMEOUT.value):
        return NONE
    return INVALID


def agreement(classes: list[str]) -> tuple[str, float]:
    """Majority class + agreement fraction over the round's submissions."""
    if not classes:
        return ("", 0.0)
    counts: dict[str, int] = {}
    for c in classes:
        counts[c] = counts.get(c, 0) + 1
    top = max(counts, key=lambda k: counts[k])
    return (top, round(counts[top] / len(classes), 4))


def resolve(classes: list[str]) -> list[ConsensusVerdict]:
    """Apply the consensus policy across one challenge's submissions."""
    any_find = FIND in classes
    out: list[ConsensusVerdict] = []
    for c in classes:
        if not any_find:
            # nobody found a counterexample; consensus is blind here
            out.append(ConsensusVerdict(None, None,
                                        "unanimous_none" if c == NONE else "n/a"))
        elif c == FIND:
            out.append(ConsensusVerdict(None, None, "verified_find"))
        elif c == NONE:
            # a peer exhibited a verified counterexample -> this "safe" is wrong
            out.append(ConsensusVerdict(0.0, "refuted_by_peer_counterexample", "outlier"))
        else:
            out.append(ConsensusVerdict(None, None, "n/a"))
    return out
