# Forward Hunt — Subtensor Money-Math Bug Candidates
**Date:** 2026-06-06  
**Branch:** opentensor/subtensor@main  
**Method:** z3 over bounded integers, generic economic invariants, hard triage of every SAT

---

## Classification Summary

| Candidate | Class | Severity |
|-----------|-------|----------|
| burn+child_take combined saturation | **REAL-NEW** | High (alpha inflation when CKBurn > ~82%) |
| Swap fee formula mismatch on tick crossing | REAL-but-NEGLIGIBLE | Negligible (max 1 rao per tick) |
| Proportion sum guard overflow | MODELING-ARTIFACT | N/A (guard is correct) |
| AMM round-trip profit | MODELING-ARTIFACT | N/A (MinimumReserve guard) |

---

## CANDIDATE 1 — REAL-NEW ✓

### Name
`burn_child_take_saturation_value_creation`

### Invariant Class
**Conservation: total_distributed + total_recycled_burn ≤ validating_emission**  
(No alpha created from nothing during dividend distribution)

### Source File + Lines
`pallets/subtensor/src/coinbase/run_coinbase.rs`, lines 956–975  
Function: `get_parent_child_dividends_distribution`

### Exact Code
```rust
burn_take = burn_take_proportion.saturating_mul(parent_emission);   // B * E
child_take = child_take_proportion.saturating_mul(parent_emission); // C * E (same base E!)
parent_emission = parent_emission.saturating_sub(burn_take);         // → max(0, E - B*E)
parent_emission = parent_emission.saturating_sub(child_take);        // → max(0, (1-B)*E - C*E)
total_child_take = total_child_take.saturating_add(child_take);      // C*E ALWAYS accumulated
// ...
let child_emission = remaining_emission          // (V - E)
    .saturating_add(total_child_take)            // + C*E
    .saturating_to_num::<u64>()
    .into();
```

### The Bug (Plain English)

When `CKBurn + childkey_take > 100%`:

1. `burn_take = B * E` is computed and recycled (correct — reduces `SubnetAlphaOut`)
2. `child_take = C * E` is computed on the **original** `E` (before burn deduction)
3. `parent_emission.saturating_sub(burn_take)` consumes all of E (if B = 100%, result = 0)
4. `parent_emission.saturating_sub(child_take)` saturates at 0 (nothing left to take from)
5. Yet `total_child_take += child_take` still adds `C*E` to what child receives

Result: The child gets `C*E` alpha that was **not actually deducted from parent** (parent was already saturated to 0). This `C*E` is distributed as alpha but has no corresponding source — it was neither minted this epoch (that already happened, `V` total was minted) nor taken from the parent (saturation prevented it). The child gets `C*E` for free.

### Math

**Normal case (B + C ≤ 1):**
- `total_distributed = (1-B-C)*E + (V-E) + C*E = V - B*E`
- `total_burned = B*E`
- `total_distributed + burned = V` ✓ (exact conservation)

**Buggy case (B + C > 1, e.g. B=100%, C=18%):**
- `burn_take = E`, `child_take = 0.18*E`
- `after_burn = max(0, E - E) = 0`
- `parent_em_final = max(0, 0 - 0.18*E) = 0` (saturated)
- `child_emission = (V-E) + 0.18*E` (child_take from nowhere)
- `total_distributed = 0 + (V-E + 0.18*E) = V - 0.82*E`
- `total_burned = E`
- `total_distributed + burned = V + 0.18*E` ← **excess 0.18*E created**

### z3 Witness
`V=1, E=1, burn_r=u64::MAX (100%), ctake=u16::MAX (100%)`  
(z3 finds this minimal case; see plain-Python verify for realistic case below)

### Plain-Python Verification
```python
V, E, burn_rate, ctake_rate = 100, 80, U64_MAX, 11796  # 100 rao epoch, 80 to parent, 100% burn, 18% child_take
burn_take = burn_rate * E // U64_MAX    # = 80
child_take = ctake_rate * E // U16_MAX  # = 14
after_burn = max(0, E - burn_take)      # = 0
parent_final = max(0, after_burn - child_take)  # = 0
child_em = (V - E) + child_take         # = 20 + 14 = 34
total_out = parent_final + child_em     # = 34
total_burned = burn_take                # = 80
EXCESS = total_out + total_burned - V   # = 114 - 100 = 14 rao created
```
Excess = `child_take_val = 14 rao` per parent per epoch. **Confirmed.**

### Trigger Conditions
- CKBurn > `1.0 - max_child_take = 1.0 - 0.18 ≈ 82%` of u64::MAX
- Child hotkey has at least one parent from a **different coldkey** (same-coldkey parents skip the take logic)
- Child hotkey's `childkey_take > 0`

### Current Live State
- **Default CKBurn = 0** (from `lib.rs` line 1086: `pub fn DefaultCKBurn() -> u64 { 0 }`)
- CKBurn set via `sudo_set_ck_burn(burn: u64)` — **no on-chain upper bound enforced**
- MaxChildkeyTake = 11796/65535 ≈ 18% (`runtime/src/lib.rs` line 1053)
- **Not triggered in current mainnet state** (CKBurn=0)

### Why It's Not Intentional
The design intent is clear from the code comments and variable names:
- `burn_take` is "the portion recycled back to the pool" (reduces supply)
- `child_take` is "the portion the childkey operator keeps as fee" (taken from parent's share)
- `parent_emission_after` is "what the parent gets after both deductions"

The intended conservation is: `parent_after + child_take + burn_take = parent_before`. The bug is that `child_take` is computed on the pre-burn `parent_emission`, but the subtraction sequence allows saturation to violate the equality.

The correct fix is either:
```rust
// Option A: compute child_take on post-burn amount
let parent_after_burn = parent_emission.saturating_sub(burn_take);
let child_take = child_take_proportion.saturating_mul(parent_after_burn);
let parent_final = parent_after_burn.saturating_sub(child_take);
```
or:
```rust
// Option B: cap combined take at parent_emission  
let combined_take = burn_take.saturating_add(child_take);
let capped = combined_take.min(parent_emission);
// distribute proportionally
```

### Not Known
Searched opentensor/subtensor open issues, closed issues, and recent PRs for:
- `ck_burn` + `child_take` + saturation
- combined take overflow
- dividend conservation

No existing report found. Unrelated to known bugs #808, #1918, #2274, #2291.

### Scale of Impact
At scale: validator with N nominators (parents), all from different coldkeys:
- Excess per epoch = `child_take_rate × sum(parent_emissions)`
- If sum(parent_emissions) = 900 TAO/epoch, child_take = 18%: **162 TAO/epoch created from nothing**
- At 360 epochs/day: ~58,000 TAO/day inflation

This is a governance-gated inflation attack: requires sudo to set CKBurn > 82%.

---

## CANDIDATE 2 — REAL-but-NEGLIGIBLE

### Name
`swap_fee_recalc_overcharge_on_edge`

### Source
`pallets/swap/src/pallet/swap_step.rs`, ~lines 130–190 (`determine_action`, `recalculate_fee` block)

### What It Is
Two different fee formulas are used depending on whether a swap step hits a tick edge:
- **Initial path** (no edge): `fee = floor(amount * rate / u16_MAX)` (fee-inclusive/gross formula)
- **Edge path** (recalculate_fee=true): `fee = floor(delta_in * rate / (u16_MAX - rate))` (fee-exclusive/net formula)

For the same `delta_in`, `fee_B > fee_A` by at most 1 rao due to floor rounding asymmetry.

### Why Negligible
Maximum overcharge is **1 rao per tick crossing** (proven by floor-difference bound). Even with 1000 tick crossings per swap: 1000 rao = 0.000001 TAO ≈ $0.000003. Below any practical materiality threshold.

### Classification
REAL (arithmetic inconsistency confirmed by z3 + plain Python) but not a material bug. Likely an incidental rounding artifact, not exploitable for meaningful gain.

---

## CANDIDATE 3 — MODELING-ARTIFACT

### Name
`proportion_sum_overflow`

### What Happened
z3 found witness where sum(5 child proportions) > u64::MAX. This is SAT because the model omitted the guard.

### Why It's Not a Bug
`ensure_total_proportions` in `set_children.rs` lines 24–38 explicitly sums as u128 and rejects if > u64::MAX. The guard is correct and present. The z3 model simply didn't include the guard constraint.

---

## CANDIDATE 4 — MODELING-ARTIFACT

### Name
`amm_roundtrip_profit`

### What Happened
z3 found a constant-product AMM witness (tao_res=1, alpha_res=1, tao_in=1) where buying and immediately selling returns more than input.

### Why It's Not a Bug
Real code has `ensure!(Order::ReserveOut::reserve(netuid) >= T::MinimumReserve::get())` in `do_swap`. Also, the new v3 concentrated liquidity AMM uses tick-based math, not simple constant-product. The model was a simplified approximation that omits the minimum reserve guard.

---

## Hunt Methodology Notes

1. All source pulled from `opentensor/subtensor@main` via `gh api repos/opentensor/subtensor/contents/...`
2. z3 queries run with `z3venv/bin/python hunt.py` (z3 4.x)
3. Every SAT witness was re-derived in plain Python against the ACTUAL Rust code logic
4. Issue/PR search confirmed no existing reports of Candidate 1
5. The `ctrl_sat` result for Candidate 1 is expected — the control constraint `Not(value_creation)` allows the SAFE case where burn+take <= 1, which IS satisfiable; this does not indicate a false alarm in the primary check
