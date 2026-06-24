"""Differential proof that every replay harness is a real discriminator.

`replay_succeeds` is only a meaningful gate if each pinned invariant separates
an exploit input from a benign one. A harness that always returns "violated" or
always returns "held" would let fake findings through or block real ones. That
is the "invalid replay harness" anti-cheat axis.

This module pins, for each registered replay target, both:

* the exploit witness: the real arithmetic violates the invariant
* a benign witness: the real arithmetic holds

It exposes a report the operator console and tests can use to assert the harness
genuinely discriminates: exploit observed != benign observed, and verdicts
differ.

Conserved targets are the defensive counterpart: the invariant holds across a
stress set and has no exploit. Both kinds are real; a harness that is neither is
a bug and fails the differential.

No z3, no network: pure arithmetic replay, deterministic.
"""
from __future__ import annotations

from game.arena import replay

# For each exploit target: a benign witness of the same shape on which the real
# pinned invariant holds. The target's own `known_witness` is the exploit.
BENIGN_WITNESSES: dict[str, dict] = {
    "subtensor-amm:recalc-overcharge@HEAD": {
        "amount": 1_000_000,
        "fee_rate": 30,
        "delta_in": 1000,
    },
    "subtensor-pallet:multi-take-split@HEAD": {
        "pool": 1_000_000,
        "rate1_bps": 3000,
        "rate2_bps": 4000,
    },
    "example-pallet:multi_take_split@HEAD": {
        "pool": 1_000_000,
        "rate1_bps": 3000,
        "rate2_bps": 4000,
    },
    "sn5-hone:exact_match_rate@HEAD": {
        "predictions": [{"predicted_output": "a", "expected_output": "a"}] * 10,
        "total_tasks": 10,
    },
    "subtensor-amm:first-fee-silent-zero@MINTED": {
        "amount": 1_000_000,
        "fee_rate": 30000,
    },
    "subtensor-amm:recalc-overcharge-raw@MINTED": {
        "amount": 1_000_000,
        "fee_rate": 30,
        "delta_in": 1000,
    },
}

# Conserved targets: no exploit exists; the invariant must hold across a stress
# set of inputs. Each entry is a list of witnesses. The root NAV invariants
# (deposit no-dilution, redeem round-trip, TAO split) are pulled from their
# harness module so the stress set lives next to the math it exercises.
from game.arena.harness_root import ROOT_STRESS as _ROOT_STRESS

CONSERVED_STRESS: dict[str, list[dict]] = {
    "subtensor-amm:first-fee-conservation@HEAD": [
        {"amount": 1_000_000, "fee_rate": 30},
        {"amount": 7, "fee_rate": 65535},
        {"amount": 2934439936, "fee_rate": 65513},
        {"amount": 1, "fee_rate": 1},
    ],
    **_ROOT_STRESS,
}


def _differential_for(target_id: str) -> dict | None:
    """Run exploit and benign witnesses for one registered replay target."""

    tgt = replay.TARGETS.get(target_id)
    if tgt is None:
        return None

    if target_id in CONSERVED_STRESS:
        held_all = True
        for witness in CONSERVED_STRESS[target_id]:
            outcome = replay.run_replay(target_id, witness)
            held_all = held_all and outcome.invariant_held and not outcome.reproduced
        return {
            "target_id": target_id,
            "family": tgt.family,
            "kind": "conserved",
            "conserved_holds": held_all,
            "discriminator": held_all,
        }

    benign = BENIGN_WITNESSES.get(target_id)
    if benign is None:
        return {
            "target_id": target_id,
            "family": tgt.family,
            "kind": "exploit",
            "exploit_reproduces": None,
            "benign_holds": None,
            "discriminator": False,
            "reason": "no_benign_witness_pinned",
        }

    exploit = replay.run_replay(target_id, tgt.known_witness)
    benign_outcome = replay.run_replay(target_id, benign)
    discriminates = (
        exploit.reproduced
        and not benign_outcome.reproduced
        and exploit.observed != benign_outcome.observed
    )
    return {
        "target_id": target_id,
        "family": tgt.family,
        "kind": "exploit",
        "exploit_reproduces": exploit.reproduced,
        "benign_holds": benign_outcome.invariant_held,
        "exploit_observed": exploit.observed,
        "benign_observed": benign_outcome.observed,
        "discriminator": discriminates,
    }


def differential_report() -> dict:
    """Return the verifier-quality artifact for all registered replay targets."""

    rows = [_differential_for(target_id) for target_id in replay.TARGETS]
    rows = [row for row in rows if row]
    return {
        "schema": "cathedral.arena.replay_differential.v1",
        "targets": rows,
        "total": len(rows),
        "discriminators": sum(1 for row in rows if row["discriminator"]),
        "exploit": sum(1 for row in rows if row["kind"] == "exploit"),
        "conserved": sum(1 for row in rows if row["kind"] == "conserved"),
        "all_real": all(row["discriminator"] for row in rows),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(differential_report(), indent=2, default=str))
