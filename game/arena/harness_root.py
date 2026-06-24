"""Replay harnesses for the subtensor ROOT NAV share model.

This is a second pinned invariant family beyond the AMM fee math, so replay
coverage is real across more than one model.

It is a faithful pure-arithmetic port of the audit-hunter root share model:

    E = escrow value (alpha)
    P = principal shares
    growth = E / P (>= 1)

    deposit  : shares = floor(bought * P / E); E += bought; P += shares
    redeem   : payout = min(floor(owed * E' / P'), E')
    tao split: s0 = floor(tao_total * w0 / wsum); remainder = tao_total - s0

Three pinned invariants are replayed as deterministic integer arithmetic:

* A1 no-dilution: E' / P' >= E / P
* A2 round-trip: redeem(deposit(b)) <= b
* A4 TAO split: s0 + remainder == tao_total exactly

All three hold by construction of floor division. They are conserved targets:
the proven-safe / hardened side of the arena.
"""
from __future__ import annotations


def _deposit(bought: int, escrow: int, principal: int) -> tuple[int, int, int]:
    shares = (bought * principal) // escrow
    return shares, escrow + bought, principal + shares


def _a1_harness(witness: dict) -> dict:
    bought = int(witness["bought"])
    escrow = int(witness["E"])
    principal = int(witness["P"])
    shares = (bought * principal) // escrow
    # E' / P' >= E / P iff bought * P >= E * shares.
    return {
        "bought": bought,
        "E": escrow,
        "P": principal,
        "shares": shares,
        "lhs": bought * principal,
        "rhs": escrow * shares,
    }


def _a1_inv(observed: dict) -> bool:
    return observed["lhs"] >= observed["rhs"]


def _a2_harness(witness: dict) -> dict:
    bought = int(witness["bought"])
    escrow = int(witness["E"])
    principal = int(witness["P"])
    shares, escrow_after, principal_after = _deposit(bought, escrow, principal)
    payout = min((shares * escrow_after) // principal_after, escrow_after) if principal_after else 0
    return {
        "bought": bought,
        "E": escrow,
        "P": principal,
        "shares": shares,
        "E2": escrow_after,
        "P2": principal_after,
        "payout": payout,
    }


def _a2_inv(observed: dict) -> bool:
    return observed["payout"] <= observed["bought"]


def _a4_harness(witness: dict) -> dict:
    tao_total = int(witness["tao_total"])
    w0 = int(witness["w0"])
    w1 = int(witness["w1"])
    wsum = w0 + w1
    s0 = (tao_total * w0) // wsum if wsum else 0
    s1 = tao_total - s0
    return {
        "tao_total": tao_total,
        "w0": w0,
        "w1": w1,
        "s0": s0,
        "s1": s1,
        "sum": s0 + s1,
    }


def _a4_inv(observed: dict) -> bool:
    return observed["sum"] == observed["tao_total"]


_ROOT_SPECS = [
    (
        "subtensor-root:deposit-no-dilution@HEAD",
        "A_conservation",
        ("bought", "E", "P"),
        _a1_harness,
        _a1_inv,
        7,
        {"bought": 500_000, "E": 1_000_000, "P": 800_000},
        "deposit: E'/P' >= E/P (existing holders not diluted)",
    ),
    (
        "subtensor-root:redeem-roundtrip@HEAD",
        "R_roundtrip",
        ("bought", "E", "P"),
        _a2_harness,
        _a2_inv,
        8,
        {"bought": 500_000, "E": 1_000_000, "P": 800_000},
        "round-trip: redeem(deposit(bought)) <= bought (no free value)",
    ),
    (
        "subtensor-root:tao-split-conservation@HEAD",
        "A_conservation",
        ("tao_total", "w0", "w1"),
        _a4_harness,
        _a4_inv,
        6,
        {"tao_total": 1_000_000, "w0": 3, "w1": 7},
        "TAO split: tao_s0 + remainder == tao_total (exact)",
    ),
]


ROOT_STRESS = {
    "subtensor-root:deposit-no-dilution@HEAD": [
        {"bought": 500_000, "E": 1_000_000, "P": 800_000},
        {"bought": 1, "E": 2, "P": 1},
        {"bought": 999_999_999, "E": 7, "P": 3},
        {"bought": 0, "E": 1_000, "P": 1_000},
    ],
    "subtensor-root:redeem-roundtrip@HEAD": [
        {"bought": 500_000, "E": 1_000_000, "P": 800_000},
        {"bought": 3, "E": 5, "P": 2},
        {"bought": 10**9, "E": 10**9, "P": 1},
        {"bought": 1, "E": 10**9, "P": 10**9},
    ],
    "subtensor-root:tao-split-conservation@HEAD": [
        {"tao_total": 1_000_000, "w0": 3, "w1": 7},
        {"tao_total": 1, "w0": 1, "w1": 1},
        {"tao_total": 10**9, "w0": 999_983, "w1": 17},
        {"tao_total": 7, "w0": 0, "w1": 5},
    ],
}


def register_root_targets() -> list[str]:
    """Register root NAV invariants in replay.TARGETS and return target ids."""

    from . import replay

    added = []
    for target_id, family, decode, harness, invariant, severity, witness, desc in _ROOT_SPECS:
        replay.TARGETS[target_id] = replay.ReplayTarget(
            target_id=target_id,
            family=family,
            cls="chain",
            property_desc=desc,
            decode=decode,
            harness=harness,
            invariant=invariant,
            severity=severity,
            known_witness=witness,
            reachable=True,
            source="root-reborn-port",
        )
        added.append(target_id)
    return added
