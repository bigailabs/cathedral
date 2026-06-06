# HydraDX Omnipool — Money-Math Hunt Results

**Source:** `galacticcouncil/hydradx-math`  
**Commit:** `380b80b59bbf62abb8848fb8a10bb206861eab41`  
**Files:** `src/omnipool/math.rs`, `types.rs`, `invariants.rs`  
**Date:** 2026-06-06  
**Scope:** Omnipool pallet core math only (sell, buy, add/remove liquidity, imbalance, fee)

---

## Check Summary

**Total checks: 26** (18 generic-invariant + 8 algebraic/Z3)  
**Candidates raised: 3**  
**Real-new findings: 0**

---

## Candidate Triage

### C07 — `calculate_imbalance_in_hub_swap`: impl >> proportional spec

**Initial concern:** The formula `floor(dq*(Q-L)/(Q+dq)) + 1 + dq` produces a delta_imbalance
roughly `2*dq`, which is ~19x larger than the "proportional" spec `L*dq/Q`.

**Independent re-derivation:**  
The imbalance invariant is `f(Q,L) = Q*(Q-L)` must be non-increasing.  
After sell_hub: `Q' = Q+dq`, `L' = L + delta_L`. Minimum `delta_L` needed:

```
delta_L_min = dq*(2Q + dq - L) / (Q + dq)
            = dq + dq*(Q-L)/(Q+dq)
```

The code computes: `floor(dq*(Q-L)/(Q+dq)) + 1 + dq = dq + ceil(dq*(Q-L)/(Q+dq))`  
which equals `ceil(delta_L_min)` — exactly the minimum needed, rounded up.

**Verdict: ARTIFACT.** My "proportional spec" `L*dq/Q` was wrong. The code formula is the mathematically correct minimum required by the invariant. The `+dq` term is not a bug.

---

### C11 — `calculate_add_liquidity_state_changes`: FixedU128 price truncation (1 unit)

**Initial concern:** `FixedU128::checked_from_rational(Q,R)` truncates the price, causing
`delta_hub` to under-estimate exact `Q*amount/R` by at most 1 unit. Price drifts down by
`<10^{-12}` relative after each add.

**Triage:**  
- Drift magnitude: `<1/(amount)` in hub terms, `<10^{-10}%` relative
- Direction: rounds in pool's favour (LP gets slightly less hub credit)
- No value extraction possible
- Consistent with the share-rounding direction (pool favoured throughout)
- The `assert_approx_eq!` in invariants.rs (`1e-10` tolerance) explicitly covers this

**Verdict: INTENDED.** Standard pool-favour rounding, documented by tolerance in tests.

---

### C17 — `calculate_imbalance_in_hub_swap`: L=0 creates imbalance (~2*dq)

**Initial concern:** When `L=0` (pool perfectly balanced), selling hub creates
`delta_imbalance ≈ 2*dq` — seems wrong to create imbalance from nothing.

**Independent re-derivation:**  
When `L=0`: `delta_L_min = dq*(2Q+dq)/(Q+dq) ≈ 2*dq`.  
This is required: `Q*(Q-0) = Q²` must stay `>= (Q+dq)*(Q+dq - delta_L)`,
which forces `delta_L >= dq*(2Q+dq)/(Q+dq)`. The invariant demands ~2dq even from a clean start.

Verification: `code_impl == ceil(delta_L_min)` confirmed numerically for multiple cases.

**Verdict: ARTIFACT** (same root as C07). Formula is provably correct.

---

## All 26 Checks (Clean)

| ID | Function | Invariant | Result |
|----|----------|-----------|--------|
| C01 | sell | delta_hub_in = floor(amt*Q/(R+amt)) | clean |
| C02 | sell | hub conservation: net = -delta_imbalance | clean |
| C03 | sell | asset_in R*Q non-decreasing | clean |
| C04 | sell | asset_out R*Q non-decreasing | clean |
| C05 | sell | delta_reserve_out >= 0 | clean |
| C06 | sell_hub | delta_reserve_out = floor(R*dq/(Q+dq)) | clean |
| C07 | imbalance | impl formula == ceil(minimum required) | candidate→artifact |
| C08 | buy | delta_hub_out(with_fee) >= no-fee | clean |
| C09 | buy | FixedU128 div: delta_hub_in >= ceil(dho/fee_comp) | clean |
| C10 | buy | delta_reserve_in >= 0 end-to-end | clean |
| C11 | add_liq | price drift <= 1 unit (pool favour) | candidate→intended |
| C12 | add_liq | share dilution: delta_s/S_new <= amt/R_new | clean |
| C13 | add_liq | floor(delta_s) <= exact | clean |
| C14 | remove_liq | double-floor delta_hub <= spec | clean |
| C15 | remove_liq | delta_b <= sr always | clean |
| C16 | remove_liq | hub_transferred <= delta_reserve * price | clean |
| C17 | imbalance | L=0 case: formula is ceil(minimum) | candidate→artifact |
| C18 | round-trip | remove(add(x)) <= x | clean |
| Z01 | buy_hub | hub_denominator guard via checked_sub | clean |
| Z02 | buy | delta_hub_in >= Q_in guard redundant but correct | clean |
| Z03 | remove_liq | delta_shares = sr - delta_b >= 0 (algebraic) | clean |
| Z04 | sell | u128*u128 product fits in U256 | clean |
| Z05 | add_liq | shares*amount fits in U256 | clean |
| Z06 | remove_liq | delta_hub >= 0 (unsigned chain) | clean |
| Z07 | fee | withdrawal_fee in [min_fee, 1.0] | clean |
| Z08 | imbalance | positive imbalance unreachable | clean |

---

## Honest Verdict

**Zero real-new findings.**

All three candidates resolved to ARTIFACT/INTENDED after independent re-derivation:
- The imbalance formula is mathematically correct (minimum required by Q*(Q-L) invariant).
- The FixedU128 price truncation is deliberate pool-favour rounding (documented by tolerance).

The core math is sound for the invariants checked. No value-extractable rounding error,
no wrong-divisor, no missing guard, no overflow, no round-trip surplus.
