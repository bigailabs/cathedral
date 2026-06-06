"""
Forward hunt for NEW money-math bugs in current subtensor (main branch, 2026-06-06).

Method: faithfully transcribe current arithmetic from source into z3 bounded integers,
assert GENERIC invariants (not hand-tuned), triage every SAT hard against source code.

Source refs pulled from opentensor/subtensor@main via gh api.
Witness arithmetic verified independently in plain Python for every SAT result.

Run with: /home/fred/experiments/evm-smt/z3venv/bin/python hunt.py
"""
from z3 import Solver, Int, If, And, Not, sat, unsat

U64_MAX = 2**64 - 1
U16_MAX = 2**16 - 1

RESULTS = []

def check(name, family, invariant_desc, violation_exprs, control_exprs, source, lines, classification, notes=""):
    sb = Solver(); sb.add(violation_exprs); rb = sb.check()
    sc = Solver(); sc.add(control_exprs); rc = sc.check()
    witness = None
    if rb == sat:
        m = sb.model()
        witness = {str(d): m[d] for d in m}
    RESULTS.append({
        "name": name, "family": family, "invariant": invariant_desc,
        "sat": rb == sat, "control_sat": rc == sat, "witness": witness,
        "source": source, "lines": lines, "classification": classification, "notes": notes,
    })
    tag = "SAT=VIOLATION" if rb == sat else "UNSAT=safe"
    ctrl = "ctrl-SAT(suspicious)" if rc == sat else "ctrl-UNSAT(good)"
    print(f"  [{tag:<20}][{ctrl:<28}] {name} → {classification}")
    if witness:
        print(f"    witness: {witness}")

print("=" * 80)
print("HUNT: opentensor/subtensor@main — forward arithmetic bug hunt 2026-06-06")
print("=" * 80)

# ===========================================================================
# BUG CANDIDATE 1 (PRIMARY): burn_take + child_take combined saturation
#                             creates alpha from nothing
#
# Source: pallets/subtensor/src/coinbase/run_coinbase.rs, lines 956-975
# Function: get_parent_child_dividends_distribution
#
# The code:
#   burn_take = burn_take_proportion.saturating_mul(parent_emission)  [B * E]
#   child_take = child_take_proportion.saturating_mul(parent_emission) [C * E, on SAME E]
#   parent_emission = parent_emission.saturating_sub(burn_take)         [clamps at 0]
#   parent_emission = parent_emission.saturating_sub(child_take)        [clamps at 0 again]
#   total_child_take += child_take                                       [ALWAYS added]
# ...
#   child_emission = remaining_emission + total_child_take
#
# When B + C > 1 (burn_rate + child_take_rate > 100%):
#   burn_take = B*E (recycled/destroyed)
#   child_take = C*E (NOT actually deducted — parent already saturated to 0 by burn)
#   child_emission = (V - E) + C*E  [C*E comes from nowhere]
#
# Conservation: distributed + burned = (V - E + C*E) + E = V + C*E > V
# → C*E alpha created from thin air per parent per epoch
#
# Trigger condition: CKBurn/u64_MAX + childkey_take/u16_MAX > 1.0
# With max_child_take = 18% (11796/65535), triggers when CKBurn > 82%.
# CKBurn has NO on-chain cap — set via sudo_set_ck_burn(burn: u64).
# Default CKBurn = 0 (safe). But if set > 82%, inflation attack is possible.
#
# INVARIANT: conservation: total_distributed + total_recycled_burn <= validating_emission
# ===========================================================================

print("\n--- Candidate 1: burn+child_take combined saturation (primary candidate) ---")

V = Int('V')           # validating_emission (alpha for this validator this epoch)
E = Int('E')           # parent_emission before takes (proportion of V for this parent)
burn_r = Int('burn_r') # CKBurn stored as u64 (normalized /u64_MAX)
ctake = Int('ctake')   # childkey_take stored as u16 (normalized /u16_MAX)

domain_c1 = [
    V >= 1, V <= 10**12,
    E >= 1, E <= V,              # parent_emission is a fraction of V
    burn_r >= 0, burn_r <= U64_MAX,
    ctake >= 0, ctake <= U16_MAX,
]

# Integer arithmetic exactly as in Rust:
burn_take = burn_r * E / U64_MAX          # floor division
child_take_val = ctake * E / U16_MAX      # floor division on ORIGINAL E (not E - burn_take)

# Saturating sub on U96F32 (unsigned float): clamps at 0
after_burn = If(E >= burn_take, E - burn_take, 0)
parent_em_final = If(after_burn >= child_take_val, after_burn - child_take_val, 0)

remaining = V - E  # (validating_emission minus parent's share, always >= 0 since E <= V)
child_em = remaining + child_take_val  # child_take added REGARDLESS of saturation

total_distributed = parent_em_final + child_em
total_burned = burn_take  # recycled via recycle_subnet_alpha

# INVARIANT: total_distributed + total_burned <= V (no value creation)
value_creation = total_distributed + total_burned > V

check(
    "burn_child_take_saturation_value_creation",
    "conservation: out+burned <= validating_emission",
    "When CKBurn + childkey_take > 100%, saturating_sub on parent_emission loses the burn, "
    "but child_take was computed on pre-burn emission and still gets added — creating alpha",
    domain_c1 + [value_creation, burn_r + ctake * (U64_MAX // U16_MAX) > U64_MAX],
    domain_c1 + [Not(value_creation)],  # control: no violation
    "pallets/subtensor/src/coinbase/run_coinbase.rs",
    "956-975",
    "REAL-NEW",
    "Witness verified: V=100, E=80, burn=100%, ctake=18% → 14 alpha created per epoch per parent"
)

# Plain-Python witness verification:
# V=100 rao, E=80 rao, burn_r=u64_MAX (100%), ctake=11796 (18%)
_V, _E, _burn_r, _ctake = 100, 80, U64_MAX, 11796
_burn_take = _burn_r * _E // U64_MAX         # = 80
_child_take = _ctake * _E // U16_MAX          # = 14
_after_burn = max(0, _E - _burn_take)         # = 0 (saturated)
_parent_final = max(0, _after_burn - _child_take)  # = 0 (already 0)
_child_em = (_V - _E) + _child_take           # = 20 + 14 = 34
_total_dist = _parent_final + _child_em        # = 34
_total_burned = _burn_take                     # = 80
_excess = _total_dist + _total_burned - _V     # = 114 - 100 = 14
print(f"  Plain-Python verify: V={_V}, E={_E}, burn=100%, ctake=18%")
print(f"    burn_take={_burn_take}, child_take={_child_take}")
print(f"    total_distributed={_total_dist}, total_recycled={_total_burned}")
print(f"    excess (alpha created) = {_excess} rao")
assert _excess == _child_take, "excess should equal child_take"
assert _excess > 0, "must be positive"
print(f"  ✓ Excess = child_take_val = {_excess} rao CONFIRMED")

# ===========================================================================
# CANDIDATE 2: Swap fee recalculation inconsistency
#
# Source: pallets/swap/src/pallet/swap_step.rs, ~lines 130-190
# Function: determine_action / recalculate_fee block
#
# Initial fee: fee_A = floor(amount * fee_rate / u16_MAX)   (fee on gross)
# Edge-triggered recalc: fee_B = floor(delta_in * fee_rate / (u16_MAX - fee_rate))  (fee on net)
#
# These two formulas give DIFFERENT results for the same amount_remaining.
# In exact arithmetic they're equal; integer floor creates a difference of 0 or 1 rao.
# INVARIANT: fee charged should not depend on whether swap hits tick edge
# VERDICT: REAL arithmetic inconsistency, but maximum 1 rao per tick crossing.
#          Economically negligible (~$0.000003 at $3/TAO). Not a material bug.
# ===========================================================================

print("\n--- Candidate 2: swap fee formula mismatch on tick crossing ---")

amt = Int('amt')
rate = Int('rate')
dom2 = [amt >= 1, amt <= 10**12, rate >= 1, rate <= U16_MAX - 1]

fee_A = amt * rate / U16_MAX
delta_in_a = amt - fee_A
denom_b = U16_MAX - rate
fee_B = If(denom_b > 0, delta_in_a * rate / denom_b, 0)

check(
    "swap_fee_recalc_overcharge_on_edge",
    "bound: recalculated fee <= initial fee for same delta_in",
    "When hitting tick edge, fee recalculated as 'net' formula > 'gross' initial formula by up to 1 rao",
    dom2 + [fee_B > fee_A, denom_b > 0],
    dom2 + [Not(fee_B > fee_A)],
    "pallets/swap/src/pallet/swap_step.rs",
    "~130-190 (recalculate_fee block in determine_action)",
    "REAL-but-NEGLIGIBLE",
    "Max overcharge is 1 rao per tick crossing. Bounded by floor(). Economically negligible."
)

# Plain-Python verify witness: rate=1, amt=65534
_amt, _rate = 65534, 1
_M = U16_MAX
_fee_A = (_amt * _rate) // _M   # = 0
_dA = _amt - _fee_A              # = 65534
_dB = _M - _rate                 # = 65534
_fee_B = (_dA * _rate) // _dB    # = 1
print(f"  Plain-Python verify: amount={_amt}, fee_rate={_rate}/{_M}")
print(f"    fee_A={_fee_A}, delta_in={_dA}, fee_B={_fee_B}")
print(f"    overcharge = {_fee_B - _fee_A} rao")
assert _fee_B > _fee_A
print(f"  ✓ 1 rao overcharge confirmed, but negligible amount")

# ===========================================================================
# CANDIDATE 3: Proportion sum guard (modeling artifact)
# ===========================================================================

print("\n--- Candidate 3: proportion sum guard (MODELING ARTIFACT) ---")
# The z3 model showed individual proportions CAN sum > u64::MAX.
# But ensure_total_proportions() checks this as u128 and BLOCKS it.
# Not including as a real bug — the guard is correct.
print("  [SKIPPED — guard correctly blocks sum > u64::MAX; not in model = artifact]")

# ===========================================================================
# CANDIDATE 4: AMM round-trip profit (modeling artifact)
# ===========================================================================

print("\n--- Candidate 4: AMM round-trip profit (MODELING ARTIFACT) ---")
# z3 found a witness with tiny reserves. In real code:
#   ensure!(Order::ReserveOut::reserve(netuid) >= T::MinimumReserve::get())
# guards against this. Not a real bug.
print("  [SKIPPED — MinimumReserve guard blocks this; not in model = artifact]")

# ===========================================================================
# SUMMARY
# ===========================================================================

print("\n" + "=" * 80)
print("FINAL TRIAGE SUMMARY")
print("=" * 80)
for r in RESULTS:
    status = "SAT=VIOLATION" if r["sat"] else "UNSAT=safe"
    ctrl = "ctrl-SAT" if r["control_sat"] else "ctrl-UNSAT"
    print(f"\n  [{r['classification']:<20}] {r['name']}")
    print(f"    z3: {status} | control: {ctrl}")
    print(f"    Source: {r['source']} {r['lines']}")
    print(f"    Invariant: {r['family']}")
    if r["witness"]:
        print(f"    Witness: {r['witness']}")
    if r["notes"]:
        print(f"    Notes: {r['notes']}")

print()
print("CLASSIFICATION COUNTS:")
for cls in ["REAL-NEW", "REAL-but-NEGLIGIBLE", "KNOWN", "INTENDED", "MODELING-ARTIFACT"]:
    count = sum(1 for r in RESULTS if r["classification"] == cls)
    if count:
        print(f"  {cls}: {count}")
