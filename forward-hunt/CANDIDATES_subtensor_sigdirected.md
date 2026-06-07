# Subtensor Emission/Reward Distribution — Signature-Directed Hunt
**Date:** 2026-06-07
**Method:** Signature-directed (S1–S6 from ENCODING_LESSONS.md), NOT blind generic sweep
**Commit:** opentensor/subtensor@`1104f2aab5acdf69fe967a787c7ae1cc5fdf170c`
**Files in scope:**
- `pallets/subtensor/src/coinbase/run_coinbase.rs` (1036 lines)
- `pallets/subtensor/src/coinbase/root.rs` (639 lines)
- `pallets/subtensor/src/coinbase/subnet_emissions.rs` (366 lines)
- `pallets/subtensor/src/coinbase/block_step.rs` (root_proportion only)
- `pallets/subtensor/src/staking/stake_utils.rs` (1499 lines)
- `pallets/subtensor/src/staking/claim_root.rs` (467 lines)
- `pallets/subtensor/src/staking/helpers.rs` (481 lines)

---

## Distribution Site Checklist (enumerated before encoding)

| # | Site | Function | Lines | Signature(s) | Result | Class |
|---|------|----------|-------|-------------|--------|-------|
| 1 | owner_cut split | `emit_to_subnets` | rc:268–276 | S2 | clean | null |
| 2 | 3-way split (root/server/validator) | `emit_to_subnets` | rc:284–307 | S2 | clean | null |
| 2b | root_proportion bound | `block_step::root_proportion` | bs:67–76 | S5 | clean | intended |
| 3 | hotkey_take deduction (alpha_divs) | `distribute_dividends_and_incentives` | rc:665–683 | S3 | clean | intended |
| 4 | hotkey_take deduction (root_alpha) | `distribute_dividends_and_incentives` | rc:694–715 | S3 | clean | intended |
| 5 | root_prop dividend split | `calculate_dividend_distribution` | rc:479–502 | S2 | clean | null |
| 6 | re-normalization (root + alpha shares) | `calculate_dividend_distribution` | rc:511–541 | S3 | clean | intended |
| 7 | burn+child_take on same base | `get_parent_child_dividends_distribution` | rc:957–965 | **S1+S2** | **candidate** | **known** |
| 8 | remaining_emission saturation | `get_parent_child_dividends_distribution` | rc:954–993 | S1 | clean | null |
| 9 | lock_cost halving truncation | `get_lock_reduction_interval` | root.rs | S3/S6 | clean | intended |
| 10 | root claimable rate round-to-zero | `increase_root_claimable_for_hotkey_and_subnet` | cr:52–72 | S3 | clean | intended |
| 11 | add/remove root_claimed roundtrip | `add/remove_stake_adjust_root_claimed` | cr | S1 | clean | null |
| 12 | U96F32 dividend accumulation overflow | `calculate_dividends_and_incentives` | rc:428–441 | S4 | clean | null |
| 13 | zero-incentive branch | `distribute_emission` | rc:797–802 | S2 | clean | null |
| 14 | disabled subnet redistribution | `get_subnet_block_emissions` | se | S5 | clean | intended |
| 15 | share pool split (take vs nominator) | `distribute_dividends_and_incentives` | rc:663–688 | S2 | clean | null |
| 16 | owed underflow (claimable - claimed) | `root_claim_on_subnet` | cr | S1 | clean | null |

**Total sites checked: 17**
**Clean: 16**
**Candidates: 1 (known duplicate)**

---

## CANDIDATE 1 — KNOWN (duplicate of original childkey bug)

### Name
`burn_child_take_saturation_value_creation`

### Location
`pallets/subtensor/src/coinbase/run_coinbase.rs`, lines 957–965
`get_parent_child_dividends_distribution`

### Signatures
**S1** (saturating arithmetic masking accounting break) + **S2** (two takes off same base)

### Exact Code (character-for-character from source)
```rust
burn_take = burn_take_proportion.saturating_mul(parent_emission);   // B * E
child_take = child_take_proportion.saturating_mul(parent_emission); // C * E (same base E!)
parent_emission = parent_emission.saturating_sub(burn_take);         // → max(0, E - B*E)
parent_emission = parent_emission.saturating_sub(child_take);        // → max(0, (1-B)*E - C*E)
total_child_take = total_child_take.saturating_add(child_take);      // C*E ALWAYS accumulated
```

### Invariant Violated
`total_distributed + total_burned ≤ validating_emission` (no alpha from nothing)

### Witness
`burn_rate = u64::MAX (100%), child_take = u16::MAX (100%)`:
- burn_take = E, child_take = 0 (saturated from original C*E via fp), parent_emission → 0
- But `child_take` was computed on original E BEFORE saturation: C*E credited to child from nowhere
- `total_distributed + burned = V + C*E > V`

### Status
**KNOWN** / **DUPLICATE** — already documented in `forward-hunt/CANDIDATES.md` as the original novel find. Not re-claiming as new.

---

## Clean Site Notes (key findings)

**SITE 2 / root_proportion**: `root_proportion = tao_weight / (tao_weight + alpha_issuance)` — denominator always ≥ numerator → bounded in [0,1]. The 3-way split is algebraically sound.

**SITE 3/4 / hotkey_take double-floor**: `tou64!(alpha_take) + tou64!(alpha_divs_rem)` can lose 1 rao per hotkey per epoch due to two separate floor truncations. This is value-loss, not value-creation. Max deficit = 1 rao × n_hotkeys per epoch. Classified as intended rounding.

**SITE 9 / lock cost halving**: `get_lock_reduction_interval` multiplies stored interval by `block_emission / 1e9`. When block_emission < 1000 rao (extreme halving), the result truncates to 0. Then `safe_div` returns 0 → lock never decreases. This is a liveness/DoS concern at extreme halving depths, not a value creation bug. The min_lock floor provides a lower bound.

**SITE 10 / root claimable round-to-zero**: `increment = amount / total_root_stake` in I96F32 (32 frac bits). When `total_root_stake > amount * 2^32 ≈ 4.3 TAO per rao of emission`, increment rounds to 0. Practically unreachable at typical epoch emission scales (~millions of rao). Value is lost, not created.

**SITE 14 / all-disabled subnets**: When all subnets disabled, block emission is unallocated and falls through to `recycle_credit` in `inject_and_maybe_swap`. Not value creation.

---

## Honest Verdict

Zero new bugs found. One candidate (known duplicate). The other 16 sites are clean or exhibit intended rounding-to-dust behavior (value loss of ≤ 1 rao per site per epoch, not value creation).

The burn+child_take site (SITE 7) was the only site where S1+S2 together fire in a dangerous combination, and it is the previously documented bug. All other multi-take splits in this codebase either:
- Have algebraically sound complementary structure (a = total - b, ensuring Σ = total), OR
- Compute each take off a sequentially decremented base (first take deducted before second is computed), OR
- Use normalized proportions that sum to ≤ 1 by construction

The reachability note for the known bug: requires `CKBurn > 100% - child_take_rate` (i.e., both rates sum > 100%). With CKBurn currently 85% and typical child_take ~18%, the condition sum = 103% is reachable and was live.
