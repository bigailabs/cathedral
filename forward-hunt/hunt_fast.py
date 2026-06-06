"""
Forward hunt — fast version.
Focused z3 queries only, no exhaustive Python loops.
"""
from z3 import Solver, Int, If, And, Or, Not, sat, unsat

U64_MAX = 2**64 - 1
U16_MAX = 2**16 - 1

RESULTS = []

def check(name, family, invariant_desc, violation_expr, control_expr, source, lines):
    sb = Solver(); sb.add(violation_expr); rb = sb.check()
    sc = Solver(); sc.add(control_expr); rc = sc.check()
    witness = None
    if rb == sat:
        m = sb.model()
        witness = {str(d): m[d] for d in m}
    RESULTS.append((name, family, rb, rc, witness, source, lines))
    tag = "SAT(VIOLATION)" if rb == sat else "UNSAT(safe)"
    ctrl = "ctrl-SAT(suspicious)" if rc == sat else "ctrl-UNSAT(ok)"
    print(f"  [{tag:<20}] [{ctrl:<25}] {name}")
    if witness:
        print(f"    witness: {witness}")

# -----------------------------------------------------------------------
# QUERY 1: dividend distribution conservation (1 parent, different coldkey)
# Source: pallets/subtensor/src/coinbase/run_coinbase.rs lines 870-1000
# Invariant: total_distributed + total_burned == validating_emission
# (should be exact — child absorbs rounding remainder)
# -----------------------------------------------------------------------
print("Q1: Dividend conservation (1 parent, different coldkey)")
V = Int('V')          # validating_emission
sc_ = Int('sc_')      # self_contribution
pa = Int('pa')        # parent alpha
pp = Int('pp')        # parent_proportion (raw u64)
burn_r = Int('burn_r')  # burn_rate_num (CKBurn, stored as u64, normalized /u64_MAX)
ctake = Int('ctake')  # child_take_num (u16, normalized /u16_MAX)

dom1 = [
    V >= 0, V <= 10**12,
    sc_ >= 0, sc_ <= 10**12,
    pa >= 0, pa <= 10**12,
    pp > 0, pp <= U64_MAX,
    burn_r >= 0, burn_r <= U64_MAX,
    ctake >= 0, ctake <= U16_MAX,
]

parent_alpha_contrib = pa * pp / U64_MAX
total_contrib = sc_ + parent_alpha_contrib

# emission_factor = parent_alpha_contrib / total_contrib (integer)
parent_em_before = If(total_contrib > 0, V * parent_alpha_contrib / total_contrib, 0)
remaining = V - parent_em_before

burn_take = burn_r * parent_em_before / U64_MAX
child_take_v = ctake * parent_em_before / U16_MAX

parent_em_after = parent_em_before - burn_take - child_take_v
child_em = remaining + child_take_v

total_out = parent_em_after + child_em
total_burned = burn_take

# Conservation: total_out + total_burned == V
violation1 = Not(total_out + total_burned == V)
# Control (trivially true formula): not violated
control1 = [total_out + total_burned > V]   # try to find value creation

check("q1_dividend_conservation_exact",
      "conservation: out+burned==V",
      "total_distributed + burned_recycled should equal validating_emission exactly",
      dom1 + [violation1, total_contrib > 0],
      dom1 + control1 + [total_contrib > 0],
      "run_coinbase.rs", "870-1000")

# -----------------------------------------------------------------------
# QUERY 2: swap fee recalculation inconsistency
# Source: pallets/swap/src/pallet/swap_step.rs ~lines 80-190
#
# Initial path: fee_A = floor(amount * rate / u16_MAX)
#               delta_in = amount - fee_A
# Edge path recalc: fee_B = floor(delta_in * rate / (u16_MAX - rate))
#
# Invariant: for same amount, fee_A <= fee_B  (i.e., edge path never charges less)
# Actually: we want to check if fee_B > fee_A (overcharge in edge path)
# -----------------------------------------------------------------------
print("\nQ2: Swap fee recalculation — does edge path charge more than initial path?")
amt = Int('amt')
rate = Int('rate')

dom2 = [
    amt >= 1, amt <= 10**12,
    rate >= 1, rate <= U16_MAX - 1,
]

fee_A = amt * rate / U16_MAX
delta_in_a = amt - fee_A
denom_b = U16_MAX - rate
fee_B = If(denom_b > 0, delta_in_a * rate / denom_b, 0)

# Amount to take in edge path = delta_in_a + fee_B
# Amount to take in initial path = delta_in_a + fee_A = amt (by definition)
# So user pays more in edge path if fee_B > fee_A
overcharge = fee_B > fee_A
undercharge = fee_B < fee_A

check("q2a_fee_overcharge_edge_path",
      "bound: fee_edge_path > fee_initial_path",
      "when hitting tick edge, user pays MORE fees than initial estimate",
      dom2 + [overcharge, denom_b > 0],
      dom2 + [Not(overcharge), denom_b > 0],
      "swap_step.rs", "~130-190")

check("q2b_fee_undercharge_edge_path",
      "bound: fee_edge_path < fee_initial_path (fee evasion)",
      "when hitting tick edge, user pays LESS fees — fee evasion possible",
      dom2 + [undercharge, denom_b > 0],
      dom2 + [Not(undercharge), denom_b > 0],
      "swap_step.rs", "~130-190")

# -----------------------------------------------------------------------
# QUERY 3: childkey proportion sum overflow check
# Source: pallets/subtensor/src/staking/set_children.rs lines 24-38
# ensure_total_proportions sums as u128, checks <= u64::MAX.
# But in get_self_contribution, it normalizes as: prop / u64::MAX.
# If each of 5 children has prop = u64::MAX / 5 + 1 (just over 1/5th),
# can sum exceed u64::MAX?
# The guard uses u128 sum: 5 * (u64::MAX/5 + 1) = u64::MAX - 4 + 5 = u64::MAX + 1 > u64::MAX.
# So this would be BLOCKED. Let's verify the guard is sufficient.
# -----------------------------------------------------------------------
print("\nQ3: Child proportion sum — can proportions overflow sum > u64::MAX?")
p1, p2, p3, p4, p5 = [Int(f'p{i}') for i in range(1, 6)]

dom3 = [
    p1 >= 0, p1 <= U64_MAX,
    p2 >= 0, p2 <= U64_MAX,
    p3 >= 0, p3 <= U64_MAX,
    p4 >= 0, p4 <= U64_MAX,
    p5 >= 0, p5 <= U64_MAX,
]

# This is checked as u128, but z3 Int is arbitrary precision so we can check directly
total_props_u128 = p1 + p2 + p3 + p4 + p5

# Can sum exceed u64::MAX while each individual prop <= u64::MAX?
check("q3_prop_sum_overflow",
      "bound: sum(proportions) <= u64::MAX enforced",
      "individual props all valid but sum > u64::MAX (would bypass guard)",
      dom3 + [total_props_u128 > U64_MAX],  # This WILL be SAT (4 props at u64::MAX/2 each)
      dom3 + [total_props_u128 > U64_MAX, p1 + p2 + p3 + p4 + p5 <= U64_MAX],  # impossible
      "set_children.rs", "24-38")

# The real question is: is the guard actually applied? It IS in ensure_total_proportions.
# But let me check: in get_self_contribution, is the sum-of-proportions bounded?

# In get_self_contribution (run_coinbase.rs ~826-860):
#   remaining_proportion = 1.0 - sum(prop_i / u64::MAX)
# Since sum(prop_i) <= u64::MAX is enforced, sum_normalized <= 1.0, so remaining >= 0.
# BUT: U96F32 saturation: if somehow remaining goes negative via saturating_sub → clamps to 0.
# Is there a case where remaining_proportion = 0 but sum_props < u64::MAX?
# Only via floating point error in U96F32.

# -----------------------------------------------------------------------
# QUERY 4: Verify fee overcharge is real with concrete witness
# -----------------------------------------------------------------------
print("\nQ4: Confirm fee overcharge with concrete witness (bounded domain)")
amt2 = Int('amt2')
rate2 = Int('rate2')
dom4 = [
    amt2 >= 1, amt2 <= 100000,
    rate2 >= 1, rate2 <= 60000,  # up to ~91% fee rate
]
fee_A2 = amt2 * rate2 / U16_MAX
delta_A2 = amt2 - fee_A2
denom2 = U16_MAX - rate2
fee_B2 = If(denom2 > 0, delta_A2 * rate2 / denom2, 0)
overcharge2 = fee_B2 > fee_A2

check("q4_fee_overcharge_concrete",
      "bound: edge_fee <= initial_fee",
      "concrete small domain: edge path recalculated fee > initial fee for same amount",
      dom4 + [overcharge2, denom2 > 0],
      dom4 + [Not(overcharge2)],
      "swap_step.rs", "~130-190")

# -----------------------------------------------------------------------
# QUERY 5: No-value-creation in inheritance when sum_prop = u64::MAX exactly
# In get_inherited, with 1 parent giving 100%:
#   inherited(child) = raw_child - 0 + raw_parent
#   inherited(parent) = raw_parent - raw_parent + 0 = 0
#   sum_inherited = raw_child + raw_parent = sum_raw. OK.
# Edge case: prop = u64::MAX, so normalized = u64::MAX / u64::MAX.
# In U96F32: 18446744073709551615 / 18446744073709551615 = 1.0 exactly? Or 0.999...?
# If it rounds down to 0.9999..., parent keeps a tiny bit.
# This is a fixed-point precision artifact, not a z3 model we can check.
# -----------------------------------------------------------------------

# -----------------------------------------------------------------------
# QUERY 6: Round-trip stake-then-unstake value creation via AMM
# Source: pallets/subtensor/src/staking (stake_into_subnet, unstake_from_subnet)
# The AMM is now Uniswap-v3 style. In v3: swap alpha→tao→alpha should not create value.
# In simple constant-product (old model), round-trip conservation is exact.
# In v3 with fees, round-trip always destroys value (fees lost).
# With no fees (drop_fees=true in some paths), can round-trip CREATE value?
#
# The critical path: stake (buy alpha with tao) then immediately unstake (sell alpha for tao).
# In a constant-product AMM with no fees:
#   Buy: tao_reserve' = tao_reserve + tao_in; alpha_out = alpha_reserve - tao_reserve*alpha_reserve/tao_reserve'
#   Sell: alpha_reserve' = alpha_reserve - alpha_out; tao_out = tao_reserve' - tao_reserve'*alpha_reserve'/alpha_reserve'
#   Wait — this is getting complex. Let me model the simpler version.
#
# For constant-product AMM (used in legacy path if any):
#   k = tao_res * alpha_res
#   buy_alpha: tao_in → alpha_out = alpha_res - k/(tao_res + tao_in)
#   sell_alpha: alpha_in → tao_out = tao_res - k/(alpha_res + alpha_in)
#
# Round-trip: tao_start, buy alpha_out, then sell alpha_out, get tao_back
# tao_back = k / (alpha_res + alpha_out) * alpha_res — Hmm, let me be more careful.
#
# In subtensor, the AMM is now v3 (concentrated liquidity). The old constant-product
# formulas don't apply. The v3 math uses sqrt_price and liquidity L.
# Modeling v3 round-trip in z3 would be very complex.
# Let's instead check the simpler: does the code have any explicit constant-product remnants?
# -----------------------------------------------------------------------

print("\nQ5: Simple constant-product AMM round-trip (legacy or simplified check)")
tao_res = Int('tao_res')
alpha_res = Int('alpha_res')
tao_in = Int('tao_in')

dom5 = [
    tao_res > 0, tao_res <= 10**12,
    alpha_res > 0, alpha_res <= 10**15,
    tao_in > 0, tao_in <= 10**12,
]

k = tao_res * alpha_res
# Buy: get alpha_out = alpha_res - k / (tao_res + tao_in)
#   with integer division, alpha_out = alpha_res - k / (tao_res + tao_in)
alpha_out = alpha_res - k / (tao_res + tao_in)

# Sell back alpha_out: new alpha_res' = alpha_res + alpha_out - alpha_out = alpha_res
# Wait, we SOLD alpha_out from the UPDATED pool. After buying, pool is:
# tao_res' = tao_res + tao_in, alpha_res' = k / (tao_res + tao_in)
# k' = tao_res' * alpha_res' = (tao_res + tao_in) * (k/(tao_res + tao_in)) = k (exact if no rounding)

# With integer division: alpha_res' = k / (tao_res + tao_in)  [floor]
# alpha_out = alpha_res - alpha_res' = alpha_res - k/(tao_res+tao_in)  [where alpha_res' is floored]

tao_res_prime = tao_res + tao_in
alpha_res_prime = k / (tao_res_prime)   # integer floor division

# Now sell alpha_out back:
# k' = tao_res_prime * alpha_res_prime  (this might be < k due to floor!)
k_prime = tao_res_prime * alpha_res_prime
new_tao_res = tao_res_prime + alpha_out  # Wait, that's wrong — sell alpha to get tao
# Selling alpha_out:
# alpha_res'' = alpha_res_prime + alpha_out
alpha_res_double_prime = alpha_res_prime + alpha_out
# tao_out = tao_res_prime - k' / alpha_res_double_prime
tao_out = tao_res_prime - k_prime / alpha_res_double_prime

# Round-trip check: tao_out should <= tao_in (no value creation)
roundtrip_profit = tao_out > tao_in  # value created from nothing!

check("q5_amm_roundtrip_profit",
      "round-trip: sell-immediately-after-buy returns <= tao_in",
      "constant-product AMM: buy alpha then immediately sell gives back MORE tao than input",
      dom5 + [roundtrip_profit, alpha_out > 0],
      dom5 + [Not(roundtrip_profit), alpha_out > 0],
      "pallets/swap/src/pallet/impls.rs (legacy constant-product analysis)",
      "conceptual model")

# -----------------------------------------------------------------------
# QUERY 7: Focused check on dividend distribution with burn — dust check
# -----------------------------------------------------------------------
print("\nQ6: Dividend dust with burn + child_take (can floor compound?)")
V2 = Int('V2')
pa2 = Int('pa2')
sc2 = Int('sc2')
burn2 = Int('burn2')  # burn numerator, /u64_MAX
ctake2 = Int('ctake2')  # child take num, /u16_MAX

dom6 = [
    V2 >= 2, V2 <= 10**12,
    pa2 >= 1, pa2 <= 10**12,
    sc2 >= 0, sc2 <= 10**12,
    burn2 >= 0, burn2 <= U64_MAX,
    ctake2 >= 0, ctake2 <= U16_MAX,
]

tc2 = sc2 + pa2
p_em2 = V2 * pa2 / tc2    # parent_emission_before_take (floor)
rem2 = V2 - p_em2

bk2 = burn2 * p_em2 / U64_MAX    # burn_take (floor)
ct2 = ctake2 * p_em2 / U16_MAX   # child_take (floor)

p_em2_after = p_em2 - bk2 - ct2  # can be negative if bk+ct > p_em2?
child_em2 = rem2 + ct2

# Invariant: parent_emission_after >= 0
parent_goes_negative = p_em2_after < 0

check("q6_parent_emission_goes_negative",
      "bound: parent_emission_after >= 0",
      "burn_take + child_take > parent_emission_before → parent gets negative assignment",
      dom6 + [parent_goes_negative, tc2 > 0],
      dom6 + [Not(parent_goes_negative), tc2 > 0],
      "run_coinbase.rs", "958-970")

# What if burn_take_proportion + child_take_proportion > 1?
# burn_r / u64_MAX + ctake / u16_MAX > 1 is possible if both are large.
# E.g., burn_r = u64_MAX (100% burn), ctake = u16_MAX (100% child take):
# parent_em after = parent_em - parent_em - parent_em = -parent_em < 0

check("q6b_combined_take_exceeds_parent_emission",
      "bound: burn+child_take rate <= 100% of parent_emission",
      "burn_take_proportion(~1.0) + child_take_proportion(~1.0) can exceed 100% of parent_emission",
      dom6 + [burn2 + ctake2 * (U64_MAX // U16_MAX) > U64_MAX, pa2 > 0, V2 > 0, tc2 > 0,
              p_em2 > 0, parent_goes_negative],
      dom6 + [Not(parent_goes_negative)],
      "run_coinbase.rs", "958-970")

# -----------------------------------------------------------------------
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
for name, family, rb, rc, witness, source, lines in RESULTS:
    tag = "SAT=VIOLATION" if rb == sat else "UNSAT=safe"
    ctrl = "ctrl-SAT" if rc == sat else "ctrl-UNSAT"
    print(f"\n  [{tag}][{ctrl}] {name}")
    print(f"    {source} {lines}")
    if witness:
        print(f"    witness: {witness}")
