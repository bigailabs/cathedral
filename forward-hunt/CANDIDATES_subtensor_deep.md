# Subtensor Deep Hunt — Candidates Receipt

**Commit:** `1104f2aab5acdf69fe967a787c7ae1cc5fdf170c`  
**Source:** opentensor/subtensor@main  
**Hunt session:** 2026-06-06  
**Scope:** Full arithmetic surface, pallets/subtensor + runtime; stateless-arithmetic invariant violations only  

---

## PROPOSED Finding: Bug Class A — Double-Deduction in `get_parent_child_dividends_distribution`

**Status:** PROPOSED (never verified — a human verifies)  
**Class:** stateless-arithmetic invariant violation  
**Severity:** high (LATENT at current params; activates via governance)

### Location
- **File:** `pallets/subtensor/src/coinbase/run_coinbase.rs`
- **Lines:** 957–970
- **Source URL:** https://github.com/opentensor/subtensor/blob/1104f2aab5acdf69fe967a787c7ae1cc5fdf170c/pallets/subtensor/src/coinbase/run_coinbase.rs#L957-L970

### Exact Source Lines (verbatim from commit)
```rust
// Lines 956-975 (parent_owner != childkey_owner branch):
burn_take = burn_take_proportion.saturating_mul(parent_emission);
child_take = child_take_proportion.saturating_mul(parent_emission);
parent_emission = parent_emission.saturating_sub(burn_take);
parent_emission = parent_emission.saturating_sub(child_take);
total_child_take = total_child_take.saturating_add(child_take);

Self::recycle_subnet_alpha(
    netuid,
    AlphaBalance::from(burn_take.saturating_to_num::<u64>()),
);
```

### Invariant Violated
```
burn_take + child_take + parent_emission_final == parent_emission_original
```

### Why the Bug Exists
Both `burn_take` and `child_take` are computed off the ORIGINAL `parent_emission` before any deduction. Then they are deducted sequentially via `saturating_sub`. When `burn_rate + child_rate > 1.0`:

1. `parent_emission -= burn_take` → clamped at 0 (saturating)
2. `parent_emission -= child_take` → already 0, stays 0 (saturating)
3. But `burn_take` and `child_take` are UNCONDITIONALLY given to recipients at their FULL computed values

**Excess created:** `max(0, burn_take + child_take - parent_emission_original)`

### Concrete Witness (Python-verified)
```python
parent_emission  = 1_000_000
burn_rate        = 0.85        # CKBurn = 0.85 * u64::MAX
child_rate       = 1.0         # child_take_proportion = MaxChildkeyTake/u16::MAX (capped at ~18% normally)

burn_take  = int(burn_rate  * parent_emission)  # 850_000
child_take = int(child_rate * parent_emission)  # 1_000_000

after_burn  = max(0, parent_emission - burn_take)   # 150_000
after_child = max(0, after_burn      - child_take)  # 0       (saturated)

total_extracted = after_child + burn_take + child_take
# = 0 + 850_000 + 1_000_000 = 1_850_000
EXCESS = total_extracted - parent_emission  # 850_000 alpha created from nothing
```

For `burn_rate + child_rate = 1.08` (e.g., CKBurn=90%, child_take=18%):
```python
parent_emission = 1_000_000
burn_take  = 900_000
child_take = 180_000
after_burn  = 100_000
after_child = max(0, 100_000 - 180_000) = 0
total_extracted = 0 + 900_000 + 180_000 = 1_080_000
EXCESS = 80_000  # 8% of parent_emission created from nothing
```

### Why Real (Not Known, Not Intended, In Scope)
1. **Why real:** Sequential `saturating_sub` with both operands computed from the ORIGINAL value. The invariant `a = a - b - c` requires `a - b - c = a - (b + c)`, which holds in real arithmetic but breaks under saturating subtraction when `b + c > a`.

2. **Why not known:** Distinct from the previously-known childkey burn bug (#2274) which involved `burn_subnet_alpha` not reducing `SubnetAlphaOut`. This is a different path: value is created ex nihilo in the *emission distribution* arithmetic, not in the *issuance tracking*.

3. **Why not intended:** No comment acknowledges this overflow behavior. The code comment says "Deduct childkey take from parent contribution" — the intent is to deduct both, not to create surplus. The excess appears as `child_take` given to the childkey that was not actually deducted from `parent_emission`.

4. **Why in scope:** Pure stateless arithmetic in a single function. No control-flow, no multi-transaction sequence needed. Activated by parameter values only.

### Current Exploitability
**LATENT** at current mainnet parameters:
- `CKBurn` defaults to `0` (stored in `StorageValue<_, u64>`, default `0`; comment in lib.rs says "18%" but is stale)
- `MaxChildkeyTake = 11_796 / 65_535 ≈ 18%`
- Current maximum: `0% + 18% = 18% < 100%` → no excess

**Activation condition:** Governance or sudo sets `CKBurn > (u64::MAX * 82%)`. There is no cap on `CKBurn` in the code (verified: `CKBurn` has no `ensure!` bound in any setter).

### Related Context
- The same structural pattern (both terms off original value) exists for multi-parent scenarios (each parent's `parent_emission` is independent so the bug is per-parent).
- `recycle_subnet_alpha` is called with `burn_take` — this correctly decreases `SubnetAlphaOut`. But when `parent_emission` saturates to 0, the full `burn_take` that was recycled was "funded" by nothing: `burn_take > parent_emission_after_child_deduction`.
- The excess value materializes as `child_take` credited to `total_child_take`, which is then added to `child_emission` at line ~993: `remaining_emission.saturating_add(total_child_take)`.

---

## Clean Checks Summary (all in this session)

| # | Function | File | Lines | Invariant | Result | Note |
|---|----------|------|-------|-----------|--------|------|
| 1 | emit_to_subnets split | run_coinbase.rs | 177-230 | conservation | CLEAN | alpha injection cap + excess TAO buyback by design |
| 2 | distribute_dividends_and_incentives delegate take | run_coinbase.rs | 580-718 | conservation | CLEAN | floor on take; payout+take <= dividend |
| 3 | SharePool alpha_take + pro-rata | share-pool/lib.rs | — | conservation | CLEAN | update_value_for_one vs all; Python-verified |
| 4 | ensure_total_proportions guard | set_children.rs | — | bound | CLEAN | u128 accumulator prevents overflow before comparison |
| 5 | swap_tao_for_alpha k-invariant | swap_step.rs | — | conservation | CLEAN | constant product non-decreasing; fees tracked in AMM |
| 6 | TotalStake vs SubnetTAO fee accounting | stake_utils.rs | 637-685 | tracking | OOS | control-flow/sequencing; intentional accounting design |
| 7 | registration burn decay | registration.rs | 466-510 | monotonicity | CLEAN | Q32 binary search; mul_by_q32 via u128; bounded |
| 8 | lock conviction decay | lock.rs | 356-408 | bound | CLEAN | 4-case formula; gamma >= 0 always; safe_div on fixed |
| 9 | inplace_pow_normalize zero-flow | subnet_emissions.rs | 196-237 | conservation | OOS | zero-flow = no emission by design; safe_div(0)=0 |
| 10 | epoch server+validator emission split | run_epoch.rs | 420-480 | conservation | CLEAN | normalized by common emission_sum; floor ensures <= rao_emission |
| 11 | recycle_alpha vs burn_alpha | recycle_alpha.rs | 20-125 | tracking | CLEAN | intentional design: burn=black hole, recycle=reduce issuance |

OOS = Out of Scope (control-flow/sequencing, not stateless arithmetic)

---

## Hunt Coverage at This Commit

Total checks logged (all sessions): **245** (see findings.jsonl)  
Stateless arithmetic PROPOSED findings from subtensor: **1** (Bug Class A above)  
Known/confirmed prior bugs re-verified as still live: **1** (finding #38 in findings.jsonl)  
Out-of-scope / intended / artifacts: multiple

---

*Generated by subtensor deep arithmetic hunt, 2026-06-06*
