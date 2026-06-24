"""REAL replay harnesses — run the actual pinned subtensor money-math on a
witness and check the invariant. This is what makes `replay_succeeds` a real
gate instead of a modeled one: a claimed finding only reproduces if the real
arithmetic violates the property on the submitted inputs.

The fixed-point ops are an EXACT pure-Python port of the U64F64 saturating math
in audit-hunter/cnf/encode_amm.py (itself verbatim from bt-kani / pallets/swap
impls.rs + swap_step.rs). No z3 needed for replay — replay is pure arithmetic,
so it is deterministic, fast, and dependency-free.

A miner's "proof" for a contract/chain target is a decoded witness (the inputs);
the arena re-runs the pinned function and accepts ONLY if the invariant is
actually violated (audit_lane.py's harness/invariant pattern). A non-reproducing
witness is rejected — "proof did not reproduce."
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

U64MAX = (1 << 64) - 1
U128MAX = (1 << 128) - 1
SCALE = 64


# -- U64F64 saturating fixed-point (exact port of encode_amm.py) --------------

def _sat128(v: int) -> int:
    return U128MAX if v > U128MAX else v

def _fixed_mul(a: int, b: int) -> int:          # (a*b)>>64, saturating
    return _sat128((a * b) >> SCALE)

def _fixed_div(a: int, b: int) -> int:          # (a<<64)/b, b==0 -> 0, saturating
    if b == 0:
        return 0
    return _sat128((a << SCALE) // b)

def _u64_to_fixed(x: int) -> int:
    return (x & U64MAX) << SCALE

def _u16_to_fixed(x: int) -> int:
    return (x & 0xFFFF) << SCALE

def _const_fixed(intval: int) -> int:
    return (intval << SCALE) & U128MAX

def _fixed_to_u64(a: int) -> int:
    v = a >> SCALE
    return U64MAX if v > U64MAX else v


def first_fee(amount: int, fee_rate: int) -> int:
    """amount * (fee_rate / u16_max) — the VERIFIED conservation path."""
    rate = _fixed_div(_u16_to_fixed(fee_rate), _const_fixed(0xFFFF))
    return _fixed_to_u64(_fixed_mul(_u64_to_fixed(amount), rate))


def recalc_fee(delta_in: int, fee_rate: int) -> int:
    """delta_in * (fee_rate / (u16_max - fee_rate)) — the UNVERIFIED recalc path.
    Denominator collapses toward 0 as fee_rate -> u16_max => fee can exceed amount."""
    u16max = _const_fixed(0xFFFF)
    fr = _u16_to_fixed(fee_rate)
    den = (u16max - fr) if fr < u16max else 0     # saturating_sub
    rate = _fixed_div(fr, den)
    return _fixed_to_u64(_fixed_mul(_u64_to_fixed(delta_in), rate))


# -- the pinned replay targets ------------------------------------------------

@dataclass(frozen=True)
class ReplayTarget:
    target_id: str
    family: str
    cls: str                       # contract | chain | subnet
    property_desc: str
    decode: tuple[str, ...]        # witness keys the harness consumes
    harness: Callable[[dict[str, Any]], dict[str, Any]]
    invariant: Callable[[dict[str, Any]], bool]   # True == property HOLDS (safe)
    severity: int
    known_witness: dict             # a real reproducing input (from the manifest)
    reachable: bool                # honest triage: is the violation reachable?
    code_sha256: str = ""          # content-addressed pinned target (audit_lane)
    source: str = "arena-port"     # arena-port | audit_lane (the REAL pinned code)


def _recalc_harness(w: dict) -> dict:
    amount, fee_rate, delta_in = int(w["amount"]), int(w["fee_rate"]), int(w["delta_in"])
    fee1 = first_fee(amount, fee_rate)
    poss = amount - fee1 if amount >= fee1 else 0
    fee2 = recalc_fee(delta_in, fee_rate)
    paid = delta_in + fee2
    return {"amount": amount, "delta_in": delta_in, "fee_rate": fee_rate,
            "fee2": fee2, "paid": paid, "poss": poss, "overcharge": max(0, paid - amount)}

def _recalc_inv(o: dict) -> bool:
    # property HOLDS iff the user never pays more than `amount` for a reachable delta_in.
    return not (o["delta_in"] <= o["poss"] and o["paid"] > o["amount"])


def _recalc_raw_inv(o: dict) -> bool:
    # the RAW recalc bound (z3 rule I1-div-by-zero): denominator collapse must
    # never make delta_in + recalc_fee exceed `amount`. No reachability gate —
    # this is exactly the invariant the z3 factory encodes, so the minted witness
    # reproduces here verbatim.
    return o["paid"] <= o["amount"]


def _conservation_harness(w: dict) -> dict:
    amount, fee_rate = int(w["amount"]), int(w["fee_rate"])
    fee1 = first_fee(amount, fee_rate)
    delta = amount - fee1 if amount >= fee1 else 0
    return {"amount": amount, "fee1": fee1, "delta": delta, "paid": fee1 + delta}

def _conservation_inv(o: dict) -> bool:
    # money is conserved: fee + delta == amount AND fee <= amount.
    return o["fee1"] <= o["amount"] and o["paid"] == o["amount"]


def _silent_zero_harness(w: dict) -> dict:
    amount, fee_rate = int(w["amount"]), int(w["fee_rate"])
    fee = first_fee(amount, fee_rate)
    return {"amount": amount, "fee_rate": fee_rate, "fee": fee}

def _silent_zero_inv(o: dict) -> bool:
    # property HOLDS: a non-zero swap with a non-zero fee_rate pays a non-zero fee.
    return not (o["amount"] > 0 and o["fee_rate"] > 0 and o["fee"] == 0)


def _split_harness(w: dict) -> dict:
    pool, r1, r2 = int(w["pool"]), int(w["rate1_bps"]), int(w["rate2_bps"])
    take1 = pool * r1 // 10_000
    take2 = pool * r2 // 10_000           # both off the SAME pool, no Σ-cap
    return {"pool": pool, "take1": take1, "take2": take2, "total": take1 + take2}

def _split_inv(o: dict) -> bool:
    return o["total"] <= o["pool"]        # can't distribute more than the pool


TARGETS: dict[str, ReplayTarget] = {
    t.target_id: t for t in [
        ReplayTarget(
            "subtensor-amm:recalc-overcharge@HEAD", "B_bounds", "contract",
            "recalc fee path must never make a user pay more than `amount`.",
            ("amount", "fee_rate", "delta_in"), _recalc_harness, _recalc_inv, 6,
            {"amount": 2934439936, "fee_rate": 65513, "delta_in": 985088},
            reachable=False),   # honest: fee_rate>32767 is governance-gated
        ReplayTarget(
            "subtensor-amm:first-fee-conservation@HEAD", "A_conservation", "chain",
            "first_fee path conserves value: fee + delta == amount, fee <= amount.",
            ("amount", "fee_rate"), _conservation_harness, _conservation_inv, 5,
            {"amount": 1_000_000, "fee_rate": 30}, reachable=True),
        ReplayTarget(
            "subtensor-pallet:multi-take-split@HEAD", "F_emission", "contract",
            "total distributed across takes must never exceed the pool (no minting).",
            ("pool", "rate1_bps", "rate2_bps"), _split_harness, _split_inv, 8,
            {"pool": 1_000_000, "rate1_bps": 7000, "rate2_bps": 6000}, reachable=True),
    ]
}


# -- wire the REAL audit-lane AuditTarget code (the actual pinned invariants) --

def _load_audit_lane():
    """Import the real audit_lane module (code/audit-lane/audit_lane.py). It is
    standalone (vendors its own contract), so its AuditTarget.harness/invariant
    run verbatim — the arena executes the ACTUAL pinned code, not a re-port."""
    import importlib.util
    import sys
    from pathlib import Path
    p = Path(__file__).resolve().parents[3] / "audit-lane" / "audit_lane.py"
    if not p.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("audit_lane", p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["audit_lane"] = mod          # dataclass needs it registered first
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


# known reproducing witnesses for the real audit_lane targets.
_AUDIT_LANE_WITNESS = {
    "sn5-hone:exact_match_rate@HEAD": {        # answer only the 3 it gets right, hide 7
        "predictions": [{"predicted_output": "a", "expected_output": "a"}] * 3,
        "total_tasks": 10},
    "example-pallet:multi_take_split@HEAD": {  # two takes off one pool, Σ > pool
        "pool": 1_000_000, "rate1_bps": 7000, "rate2_bps": 6000},
}
_AUDIT_LANE_META = {
    "sn5-hone:exact_match_rate@HEAD": ("G_scoring", "subnet",
        ("predictions", "total_tasks"), True),
    "example-pallet:multi_take_split@HEAD": ("F_emission", "contract",
        ("pool", "rate1_bps", "rate2_bps"), True),
}


def _register_audit_lane() -> list[str]:
    """Add the real audit_lane AuditTargets to TARGETS. Returns the ids added."""
    al = _load_audit_lane()
    added = []
    if al is None:
        return added
    for t in getattr(al, "TARGETS", []):
        meta = _AUDIT_LANE_META.get(t.target_id)
        wit = _AUDIT_LANE_WITNESS.get(t.target_id)
        if not meta or wit is None:
            continue
        family, cls, decode, reachable = meta
        TARGETS[t.target_id] = ReplayTarget(
            target_id=t.target_id, family=family, cls=cls,
            property_desc=t.property_desc, decode=decode,
            harness=t.harness, invariant=t.invariant, severity=t.base_severity,
            known_witness=wit, reachable=reachable,
            code_sha256=t.code_sha256, source="audit_lane")
        added.append(t.target_id)
    return added


AUDIT_LANE_TARGETS = _register_audit_lane()


# the z3 factory rules the arena mints into REAL replay targets. Each SAT rule's
# z3 model is the exploit input that its harness reproduces verbatim — the thing
# the miner solves and the thing that reproduces are the same instance. B2 stays
# first so MINTED_TARGETS[0] is the silent-zero target (back-compat).
_MINT_RULES = [
    # rule_id, target_id, family, decode, harness, invariant, severity, property_desc
    ("B2-fee-silent-zero", "subtensor-amm:first-fee-silent-zero@MINTED", "B_bounds",
     ("amount", "fee_rate"), _silent_zero_harness, _silent_zero_inv, 4,
     "amount>0 & fee_rate>0 => first_fee>0 (no silent zero fee)"),
    ("I1-div-by-zero", "subtensor-amm:recalc-overcharge-raw@MINTED", "I_safety",
     ("amount", "fee_rate", "delta_in"), _recalc_harness, _recalc_raw_inv, 6,
     "recalc denominator collapse: delta_in + recalc_fee must stay <= amount"),
]

# z3 rules whose NEGATED invariant is UNSAT — a proven-hardened invariant (no
# exploit exists). z3 says unsat AND an independent CDCL solver confirms it.
# Spans TWO pinned models: subtensor-amm (fee math) + subtensor-root-reborn
# (root staking / TAO split). (rule_id, model, width, family, desc)
_HARDENED_RULES = [
    ("A4-fee-split-conservation", "subtensor-amm", 16, "A_conservation",
     "AMM first_fee conserves value: fee + delta == amount, fee <= amount"),
    ("A4-tao-split-conservation", "subtensor-root-reborn", 8, "A_conservation",
     "root TAO split is exact: tao_s0 + remainder == tao_total (no rounding leak)"),
    ("F2-payout-cap-asymmetry", "subtensor-root-reborn", 8, "F_emission",
     "root partial redeem leaves no stranded holders (E>0 while P'>0)"),
    ("A1-deposit-no-dilution", "subtensor-root-reborn", 8, "A_conservation",
     "root deposit cannot dilute existing holders: E'/P' >= E/P "
     "(floor of shares makes the dilution a direct contradiction)"),
]

_HARDENED_MANIFEST = Path(__file__).resolve().parent / "out" / "hardened_manifest.json"


def compute_hardened() -> list[dict]:
    """The HEAVY path: mint each hardened rule via z3 and confirm UNSAT with a
    CDCL solver (Glucose). Run once to (re)build the manifest — NOT at import
    (the root-reborn CNFs take a few seconds each to confirm)."""
    from . import mint
    out = []
    for rule_id, model, width, family, desc in _HARDENED_RULES:
        m = mint.mint_invariant(rule_id, width, "realistic", model)
        if not m or m["result"] != "unsat" or not m.get("cnf_text"):
            continue
        cdcl = mint.solve_minted_cnf(m["cnf_text"])
        confirmed = bool(cdcl.get("available") and cdcl.get("sat") is False)
        out.append({"rule_id": rule_id, "model": model, "family": family,
                    "invariant": desc, "z3": "unsat", "cdcl_unsat": confirmed,
                    "cnf_sha256": m["cnf_sha256"], "vars": m.get("vars"),
                    "clauses": m.get("clauses"), "hardened": confirmed})
    return out


def _register_minted() -> list[str]:
    """Mint each SAT factory rule via the z3 factory and register it as a replay
    target whose witness is the z3 SOLUTION to the encoded invariant — so solving
    the CNF and reproducing the bug are ONE proof, across multiple invariant
    families. No-op if z3 is absent."""
    from . import mint
    added = []
    for rule_id, tid, family, decode, harness, inv, sev, desc in _MINT_RULES:
        m = mint.mint_invariant(rule_id, 16, "realistic")
        if not (m and m["result"] == "sat" and m["witness"]):
            continue
        wit = {k: m["witness"][k] for k in decode if k in m["witness"]}
        if not all(k in wit for k in decode):
            continue                                  # incomplete model -> skip
        TARGETS[tid] = ReplayTarget(
            target_id=tid, family=family, cls="contract", property_desc=desc,
            decode=decode, harness=harness, invariant=inv, severity=sev,
            known_witness=wit, reachable=False,        # honest triage: governance-gated
            code_sha256=m["cnf_sha256"], source="z3-factory-mint")
        added.append(tid)
    return added


def _register_hardened() -> list[dict]:
    """Load the precomputed hardened-proof manifest (fast). The heavy z3+CDCL work
    runs once via compute_hardened()/the precompute script, not at import. If the
    manifest is absent, fall back to confirming ONLY the cheap AMM A4 live so the
    module still works on a fresh checkout."""
    import json
    try:
        data = json.loads(_HARDENED_MANIFEST.read_text())
        if isinstance(data, list) and data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    # fallback: the cheap AMM conservation proof only (others need the manifest)
    from . import mint
    m = mint.mint_invariant("A4-fee-split-conservation", 16, "realistic", "subtensor-amm")
    if not m or m["result"] != "unsat" or not m.get("cnf_text"):
        return []
    cdcl = mint.solve_minted_cnf(m["cnf_text"])
    confirmed = bool(cdcl.get("available") and cdcl.get("sat") is False)
    return [{"rule_id": "A4-fee-split-conservation", "model": "subtensor-amm",
             "family": "A_conservation",
             "invariant": "AMM first_fee conserves value: fee + delta == amount, fee <= amount",
             "z3": "unsat", "cdcl_unsat": confirmed, "cnf_sha256": m["cnf_sha256"],
             "hardened": confirmed}]


MINTED_TARGETS = _register_minted()
MINTED_HARDENED = _register_hardened()


def minted_summary() -> dict:
    """For the operator console: which invariant families have a REAL minted
    proof (z3 mint -> CDCL solve -> harness reproduce) and which are proven
    hardened (z3 + CDCL unsat). Real, not claimed."""
    sat = []
    for tid in MINTED_TARGETS:
        t = TARGETS[tid]
        o = run_replay(tid, t.known_witness)
        sat.append({"target_id": tid, "family": t.family,
                    "reproduced": o.reproduced, "code_sha256": t.code_sha256})
    families = sorted({s["family"] for s in sat} |
                      {h["family"] for h in MINTED_HARDENED})
    return {"sat_minted": sat, "hardened": MINTED_HARDENED, "families": families}


@dataclass
class ReplayOutcome:
    target_id: str
    reproduced: bool               # the invariant was VIOLATED on this witness
    invariant_held: bool
    observed: dict
    reason: str = ""


def run_replay(target_id: str, witness: dict | None) -> ReplayOutcome:
    """Re-run the pinned money-math on the witness. reproduced=True iff the real
    invariant is violated (a genuine finding)."""
    tgt = TARGETS.get(target_id)
    if tgt is None:
        return ReplayOutcome(target_id, False, True, {}, "unknown_target")
    if not witness or not all(k in witness for k in tgt.decode):
        return ReplayOutcome(target_id, False, True, {},
                             f"witness_missing_keys:{tgt.decode}")
    try:
        observed = tgt.harness({k: witness[k] for k in tgt.decode})
    except Exception as e:                       # malformed witness -> no repro
        return ReplayOutcome(target_id, False, True, {}, f"harness_error:{type(e).__name__}")
    held = tgt.invariant(observed)
    return ReplayOutcome(target_id, reproduced=not held, invariant_held=held,
                         observed=observed,
                         reason="" if not held else "proof_did_not_reproduce")


if __name__ == "__main__":   # faithfulness self-check against the manifest witness
    for tid, t in TARGETS.items():
        o = run_replay(tid, t.known_witness)
        print(f"{tid}\n  witness={t.known_witness}\n  observed={o.observed}\n"
              f"  reproduced={o.reproduced} reachable={t.reachable}\n")
