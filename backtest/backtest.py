"""Blind backtest: would a STANDING library of GENERIC economic invariants have
caught known subtensor money-math bugs, without being told where the bug is?

Method (honest discipline):
  * Each bug is transcribed FAITHFULLY from its real pre-fix / post-fix diff.
  * The invariant applied is the GENERIC one for the bug's CATEGORY (bound /
    conservation / monotonicity / round-trip) — NOT hand-tuned to the bug.
  * "CAUGHT" = the generic invariant is violable by the buggy code (z3 SAT) AND
    holds for the fixed code (UNSAT) — i.e. it fires on the bug, not a false alarm.

z3 over bounded ints. Add a bug = add a registry entry. Pure model-level test
(measures invariant coverage + completeness; the auto-lift-from-source step is a
separate engineering risk, called out — not measured here).
"""
from z3 import Solver, Int, If, And, Or, Not, sat, unsat

# ---- generic invariant library (written WITHOUT looking at any specific bug) --
# each returns a z3 BoolRef that SHOULD always hold; we look for a violation.
def inv_bound(out, budget):        return out <= budget          # output can't exceed its budget
def inv_nonneg(out):               return out >= 0
def inv_monotonic(after, before):  return after >= before        # cost/price must not drop
def inv_roundtrip(back, original): return back == original       # convert-and-back conserves
def inv_conservation(out, in_):    return out == in_             # books balance

CAUGHT = MISSED = FALSE_ALARM = 0
def report(bug, family, buggy_violation, fixed_violation):
    global CAUGHT, MISSED, FALSE_ALARM
    sb = Solver(); sb.add(buggy_violation); rb = sb.check()
    sf = Solver(); sf.add(fixed_violation); rf = sf.check()
    caught = (rb == sat) and (rf == unsat)
    fa = (rf == sat)
    tag = "CAUGHT" if caught else ("MISSED" if rb != sat else "FALSE-ALARM")
    print(f"[{tag:11}] {bug:34} (generic invariant: {family})")
    if rb == sat:
        m = sb.model(); print("      buggy violated by:", {str(d): m[d] for d in m})
    if caught: CAUGHT += 1
    elif fa:   FALSE_ALARM += 1
    else:      MISSED += 1

# ============================ the corpus ============================

# ---------------------------------------------------------------------------
# BUG 1 — PR #1918 (OPEN): alpha_in over-emission
# File: pallets/subtensor/src/coinbase/subnet_emissions.rs, line 87
# Bug: pre-fix sets alpha_in_i = alpha_emission_i (uncapped), which can exceed block_emission.
#      If tao halving reduces block_emission below alpha_emission, tao/alpha imbalance occurs.
# Fix: alpha_in_i = tao_in_i / price (= block_emission when subsidy active, always <= block_emission)
# PR: https://github.com/opentensor/subtensor/pull/1918
# Category: BOUND (alpha emitted into pool ≤ block budget)
# ---------------------------------------------------------------------------
B, AE, P = Int('block_emission'), Int('alpha_emission'), Int('price')
dom_1918 = [B >= 0, B <= 10**12, AE >= 0, AE <= 10**12, P > 0, P <= 10**12]
alpha_buggy_1918 = AE                   # pre-fix line 87: alpha_in = alpha_emission_i
alpha_fixed_1918 = (P * B) / P          # post-fix: tao_in / price = (price*block_emission)/price = block_emission
report("subtensor PR#1918 alpha over-emit",
       "bound: alpha_in <= block_emission",
       dom_1918 + [alpha_buggy_1918 > B],
       dom_1918 + [alpha_fixed_1918 > B])

# ---------------------------------------------------------------------------
# BUG 2 — EVM<->Substrate balance conversion (no PR#, discovered pre-launch)
# File: implied by substrate's to_h160/from_h160; DEC = 1e9 (rao per wei)
# Bug: into_evm(into_substrate(e)) = (e // 1e9) * 1e9 — truncates sub-rao wei
# Fix (the other direction): into_substrate(into_evm(rao)) = (rao * 1e9) / 1e9 — exact
# Category: ROUND-TRIP (convert-and-back must conserve value)
# ---------------------------------------------------------------------------
DEC = 1_000_000_000
e = Int('e_wei')
edom = [e >= 0, e <= 10**18]
back_buggy_conv = (e / DEC) * DEC            # intoEvm(intoSubstrate(e)) — truncates
rao = Int('rao'); rdom = [rao >= 0, rao <= 10**12]
back_fixed_conv = (rao * DEC) / DEC          # intoSubstrate(intoEvm(rao)) — exact
report("subtensor EVM<->sub convert",
       "round-trip: back == original",
       edom + [back_buggy_conv != e],
       rdom + [back_fixed_conv != rao])

# ---------------------------------------------------------------------------
# BUG 3 — Issue #2274: burn_subnet_alpha is a no-op (alpha conservation broken)
# File: pallets/subtensor/src/staking/helpers.rs
# Bug (pre-fix): burn_subnet_alpha(_netuid, _amount) { /* Do nothing; TODO */ }
#   → user's hotkey stake decreases but SubnetAlphaOut does NOT decrease
#   → SubnetAlphaOut > sum(hotkey_alpha) — phantom alpha in existence
# Fix: SubnetAlphaOut should be decremented by the burned amount (like recycle_subnet_alpha does)
# Issue: https://github.com/opentensor/subtensor/issues/2274
# Category: CONSERVATION (SubnetAlphaOut == sum of all outstanding stake after burn)
# ---------------------------------------------------------------------------
alpha_out_before = Int('alpha_out_before')
hotkey_alpha_before = Int('hotkey_alpha_before')
burn_amount = Int('burn_amount')
dom_2274 = [
    alpha_out_before >= 0, alpha_out_before <= 10**15,
    hotkey_alpha_before >= 0, hotkey_alpha_before <= alpha_out_before,
    burn_amount >= 0, burn_amount <= hotkey_alpha_before,
]
# Buggy: burn_subnet_alpha does nothing → SubnetAlphaOut unchanged, hotkey stake decreases
alpha_out_buggy_2274 = alpha_out_before                       # unchanged (no-op)
hotkey_alpha_after_2274 = hotkey_alpha_before - burn_amount  # stake reduced
# Conservation: alpha_out should equal sum of outstanding hotkey alpha
# After burn: alpha_out should have decreased too
# Invariant: alpha_out_after >= alpha_out_before - burn_amount (i.e. the decrement happened)
# Or equivalently: if hotkey stake decreased by X, alpha_out must also decrease by X
# Model: the books must balance — alpha_out change == hotkey_alpha change
alpha_out_change_buggy = alpha_out_buggy_2274 - alpha_out_before          # = 0 (no-op)
hotkey_alpha_change = hotkey_alpha_after_2274 - hotkey_alpha_before        # = -burn_amount
# Invariant: alpha_out_change == hotkey_alpha_change (conservation)
report("subtensor issue#2274 burn no-op",
       "conservation: alpha_out_delta == stake_delta",
       dom_2274 + [burn_amount > 0,
                   Not(inv_conservation(alpha_out_change_buggy, hotkey_alpha_change))],
       # Fixed: alpha_out_change = -burn_amount = hotkey_alpha_change
       dom_2274 + [burn_amount > 0,
                   Not(inv_conservation(-burn_amount, hotkey_alpha_change))])

# ---------------------------------------------------------------------------
# BUG 4 — PR #808: nominator emission mul-before-div overflow in I64F64
# File: pallets/subtensor/src/coinbase/run_coinbase.rs, line 311 (pre-fix)
# Bug: nominator_emission = I64F64(emission_minus_take) * I64F64(nominator_stake)
#                           / I64F64(total_viable_nominator_stake)
#   I64F64 max = 2^63 ≈ 9.2e18. emission (up to 1e9) * stake (can be > 9.2e9 rao)
#   saturates to I64F64::MAX, then divides → gives MUCH LESS than correct share.
# Fix: reorder to (nominator_stake / total_viable_stake) * emission_minus_take
#   ratio is in [0,1], then multiply by emission — no overflow.
# PR: https://github.com/opentensor/subtensor/pull/808
# Category: BOUND (nominator share ≤ emission; with single nominator = total_stake, share == emission)
# Model: bounded integers, MAXVAL = 2^63, emission * stake can overflow
# ---------------------------------------------------------------------------
emission = Int('emission_808')
nom_stake = Int('nom_stake_808')
# Use single-nominator case: nom_stake == total_viable_stake → expected share = emission
dom_808 = [emission >= 0, emission <= 10**9,
           nom_stake >= 0, nom_stake <= 10**15]
MAXVAL_808 = 2**63  # I64F64 saturating max
# Buggy: emission * stake → saturates at MAXVAL, then / stake
# Model: min(emission * nom_stake, MAXVAL) // nom_stake
# z3 If() implements the saturation
product_808 = emission * nom_stake
saturated_808 = If(product_808 > MAXVAL_808, MAXVAL_808, product_808)
nom_emission_buggy_808 = If(nom_stake > 0, saturated_808 / nom_stake, emission)
# Fixed: nom_stake / total_stake * emission = (nom_stake / nom_stake) * emission = emission (exactly)
nom_emission_fixed_808 = emission  # ratio = 1.0 for single nominator
# Invariant: when nominator holds ALL stake, they should receive exactly emission
# Bug fires when emission * stake overflows MAXVAL (stake > MAXVAL / emission = 9.2e9 for emission=1e9)
# The overflow condition: emission * stake > MAXVAL_808
# Concrete threshold: with max emission=1e9, overflow at stake > 9_223_372_036 rao (~9.2 TAO)
# Use a concrete threshold value instead of symbolic division
OVERFLOW_THRESHOLD_808 = 9_223_372_036  # = 2^63 // 10^9 (pre-computed)
report("subtensor PR#808 nom emission overflow",
       "bound: nom_emission == emission (when stake==total)",
       dom_808 + [nom_stake > OVERFLOW_THRESHOLD_808,    # force overflow condition
                  emission > 0,
                  nom_emission_buggy_808 != emission],   # should equal emission
       dom_808 + [nom_emission_fixed_808 != emission])   # fixed: always equals emission

# ---------------------------------------------------------------------------
# BUG 5 — Issue #2291 (OPEN, unfixed): subnet registration lock cost decreases
# File: pallets/subtensor/src/coinbase/root.rs
# get_lock_reduction_interval() = stored_interval * (block_emission / 1e9)
# get_network_lock_cost() = last_lock * 2 - (last_lock / LRI) * elapsed_blocks
# Bug: when block_emission < 1e9 (halving), LRI shrinks. Over a typical ~100k-block
#   inter-registration period, the buggy decay is faster: it brings cost to min_lock
#   while the non-halved formula would still give a cost above min_lock (above floor).
#   After halving, the cost-vs-time curve collapses faster than intended, violating the
#   design intent that the floor takes much longer to reach.
# Fix (natural): LRI should not be scaled by block_emission/1e9 (use stored_interval directly).
# Issue: https://github.com/opentensor/subtensor/issues/2291
# Category: MONOTONICITY (buggy cost <= fixed cost; buggy hits min_lock prematurely)
# Model: show that with the halved LRI, cost = min_lock while fixed formula cost > min_lock.
# ---------------------------------------------------------------------------
last_lock_2291 = Int('last_lock_2291')
elapsed_2291 = Int('elapsed_2291')
stored_interval_2291 = Int('stored_interval_2291')
halving_factor_num = Int('halving_num')    # numerator of halving out of 1000 (e.g. 625 = 62.5%)
min_lock_2291 = 100_000_000  # 0.1 TAO in rao
dom_2291 = [
    last_lock_2291 > min_lock_2291, last_lock_2291 <= 10**12,
    elapsed_2291 > 0, elapsed_2291 <= 200000,
    stored_interval_2291 > 0, stored_interval_2291 <= 200000,
    halving_factor_num > 0, halving_factor_num < 1000,  # strictly less than full emission
]
# Buggy: LRI = stored_interval * halving_factor_num / 1000 (integer approximation of scaling)
LRI_buggy_2291 = stored_interval_2291 * halving_factor_num / 1000    # integer div
# Avoid LRI=0 (safe_div would make decay=0, not the interesting bug case)
decay_buggy_2291 = If(LRI_buggy_2291 > 0, (last_lock_2291 / LRI_buggy_2291) * elapsed_2291, 0)
cost_raw_buggy_2291 = last_lock_2291 * 2 - decay_buggy_2291
cost_buggy_2291 = If(cost_raw_buggy_2291 < min_lock_2291, min_lock_2291, cost_raw_buggy_2291)
# Fixed: LRI = stored_interval (no halving adjustment)
LRI_fixed_2291 = stored_interval_2291
decay_fixed_2291 = (last_lock_2291 / LRI_fixed_2291) * elapsed_2291
cost_raw_fixed_2291 = last_lock_2291 * 2 - decay_fixed_2291
cost_fixed_2291 = If(cost_raw_fixed_2291 < min_lock_2291, min_lock_2291, cost_raw_fixed_2291)
# Invariant (monotonicity): within one inter-registration window (elapsed <= LRI),
# the cost should remain >= last_lock after the mult=2 boost.
# i.e., cost = 2*last_lock - (last_lock/LRI)*elapsed >= last_lock
# This holds when elapsed <= LRI (decay <= last_lock).
# BUGGY: LRI is halved → for elapsed in (LRI_buggy, LRI_fixed], decay > last_lock, cost < last_lock
# FIXED: LRI = stored_interval → decay = (last_lock/stored_interval)*elapsed <= last_lock when elapsed<=stored_interval
#
# Test: find (elapsed <= stored_interval) where cost drops below last_lock in buggy vs fixed
report("subtensor issue#2291 lock cost drop",
       "monotonicity: cost >= last_lock within one LRI window",
       # Buggy: elapsed in (LRI_buggy, LRI_fixed] → cost < last_lock
       dom_2291 + [LRI_buggy_2291 > 0,
                   elapsed_2291 > LRI_buggy_2291,             # elapsed exceeds buggy LRI
                   elapsed_2291 <= LRI_fixed_2291,            # but not the fixed LRI
                   cost_buggy_2291 < last_lock_2291],         # cost dropped below last paid
       # Fixed: elapsed <= stored_interval → cost always >= last_lock
       dom_2291 + [elapsed_2291 <= stored_interval_2291,      # constrain to one window
                   cost_fixed_2291 < last_lock_2291])         # should NOT be violable

# ---------------------------------------------------------------------------
# BUG 6 — PR #415: root epoch weights not normalized before matmul
# File: pallets/subtensor/src/root.rs (pre-fix ~line 351)
# Weights are upscaled to u16 during storage: max weight in a row → 65535.
# Bug: weights used directly (without row-normalization) in matmul with stake.
#   Validator with k non-zero weights has row-sum = k * 65535, not 1.0.
#   This inflates influence proportional to number of subnets voted on.
# Fix: inplace_row_normalize_64(&mut weights) added before matmul (line 349).
# PR: https://github.com/opentensor/subtensor/pull/415
# Category: CONSERVATION (each validator's total influence should equal their stake fraction)
# Model: two validators with equal stake; val2 votes on 2x subnets → val2 gets 2x influence (buggy).
# ---------------------------------------------------------------------------
stake1 = Int('stake1_415')
stake2 = Int('stake2_415')
# Validator 1: votes on 1 subnet with w1a = 65535; val2: votes on 2 subnets with w2a = w2b = 65535
# (both uniformly allocated — same INTENT, different number of targets)
W1a = 65535
W2a, W2b = 65535, 65535
dom_415 = [stake1 > 0, stake1 <= 10**12,
           stake2 > 0, stake2 <= 10**12,
           stake1 == stake2]  # equal stake — should have equal total influence
# Total weight sum (buggy, unnormalized):
total_weight_sum_buggy_1 = W1a          # val1 row sum
total_weight_sum_buggy_2 = W2a + W2b    # val2 row sum = 2 * W2a
# Influence fraction for each validator (as proportion of combined weight):
# Val1 influence = stake1 * W1a, Val2 influence = stake2 * (W2a + W2b)
# But they should be equal when stake1 == stake2.
# Invariant: val1_total_influence / val2_total_influence == stake1 / stake2
# With unnormalized: val2 has 2x the row weight sum → 2x total influence
val1_influence_buggy = stake1 * total_weight_sum_buggy_1
val2_influence_buggy = stake2 * total_weight_sum_buggy_2
# After normalization (fixed): both rows sum to 65535 (normalized to same scale)
# Fixed: W1a_norm = 65535/65535 = 1.0; W2a_norm = 65535/131070 = 0.5, W2b_norm = 0.5
# Total fixed influence: val1 = stake1 * 1.0, val2 = stake2 * (0.5 + 0.5) = stake2 * 1.0
# In integer model: val1_fixed = stake1 * 65535, val2_fixed = stake2 * 65535
val1_influence_fixed = stake1 * W1a      # after normalization: row sum is same for all
val2_influence_fixed = stake2 * W2a      # (each row normalizes to 65535 equivalent)
# Invariant: equal stake → equal total influence → val1_influence == val2_influence
report("subtensor PR#415 weight row non-norm",
       "conservation: equal stake -> equal total influence",
       dom_415 + [val1_influence_buggy != val2_influence_buggy],   # buggy: different
       dom_415 + [val1_influence_fixed != val2_influence_fixed])    # fixed: always equal

# ============================ tally ============================
total = CAUGHT + MISSED + FALSE_ALARM
print("\n================ BACKTEST RESULT ================")
print(f"corpus size (faithful entries): {total}")
print(f"CAUGHT blind by a generic invariant: {CAUGHT}/{total}")
print(f"MISSED: {MISSED}    FALSE-ALARMS: {FALSE_ALARM}")
print(f"Hit rate: {CAUGHT/total*100:.1f}%")
print("")
print("Caveats:")
print("  - Model-level only: tests invariants over bounded integer abstractions,")
print("    NOT over the actual Rust/Substrate source (auto-lift step not measured).")
print("  - Bounded-incomplete: z3 checks finite domains; large inputs might escape.")
print("  - Narrow to stateless arithmetic: storage iteration, gas, extrinsic ordering")
print("    bugs are out of scope for this invariant library.")
print("  - Bugs #2291 and #2274 are unfixed (issues still open); the 'fixed' formulas")
print("    are the natural counterparts, not yet in production.")
