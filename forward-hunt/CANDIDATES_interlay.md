# Interlay interBTC — Money Math Hunt Receipts

**Source commit:** c645294b3257d5a0e37b2613e55b90380100ea9f  
**Repo:** https://github.com/interlay/interbtc  
**Scope:** vault-registry, fee, oracle, redeem pallets — collateral / liquidation / fee math only  
**Date:** 2026-06-06  
**Checks run:** 19 (see `forward-hunt/interlay_hunt.py`)  
**Candidates:** 2 (1 PROPOSED real-new, 1 cosmetic known)  
**Honest zero real-new bugs?** No — 1 PROPOSED inconsistency found (not a silent fund loss).

---

## CANDIDATES

### CANDIDATE-1 — PROPOSED: Wrong currency in `get_issuable_tokens_from_vault` zero-path

**Status:** PROPOSED (real-new inconsistency, not confirmed as financial loss)  
**File:** `crates/vault-registry/src/lib.rs:1780-1784`  
**Source URL:** https://github.com/interlay/interbtc/blob/c645294b3257d5a0e37b2613e55b90380100ea9f/crates/vault-registry/src/lib.rs#L1780  
**Severity:** Medium  

**Finding:**

```rust
pub fn get_issuable_tokens_from_vault(vault_id: &DefaultVaultId<T>) -> Result<Amount<T>, DispatchError> {
    let vault = Self::get_active_rich_vault_from_id(vault_id)?;
    if vault.data.status != VaultStatus::Active(true) {
        Ok(Amount::new(0u32.into(), vault_id.currencies.collateral))  // ← COLLATERAL currency
    } else {
        vault.issuable_tokens()  // ← returns in WRAPPED currency
    }
}
```

When a vault is not accepting new issues, the function returns zero with `collateral` currency.  
When it is accepting, it returns the issuable amount in `wrapped` currency.

**Expected (from `RichVault::issuable_tokens` at `types.rs:441`):**
```rust
if self.is_banned() {
    return Ok(Amount::new(0u32.into(), self.wrapped_currency()));  // ← WRAPPED (correct)
}
```

The inline `issuable_tokens()` method correctly returns zero in `wrapped_currency`. The standalone  
`get_issuable_tokens_from_vault` uses `collateral` currency in its zero path.

**Impact:**  
- Any caller that does `amount.checked_add(&other_wrapped_amount)` on the result will get  
  `InvalidCurrency` error (not a silent fund loss, just an unexpected error).  
- `get_vaults_with_issuable_tokens` only calls `.is_zero()` so is unaffected.  
- External RPC callers (UI, indexers) may receive an amount with the wrong currency label,  
  potentially misleading displays or downstream logic.

**Witness:** Code read, character-for-character verified against commit c645294.  
**Triage:** Not a silent value-loss bug. No known exploit. `InvalidCurrency` errors would be the  
symptom. Classified PROPOSED pending team triage on actual caller impacts.

---

### CANDIDATE-2 — KNOWN/COSMETIC: `calculate_collateral` maps division-by-zero to `ArithmeticError::Underflow`

**Status:** cosmetic/known (no financial impact)  
**File:** `crates/vault-registry/src/lib.rs:1643-1644`  
**Source URL:** https://github.com/interlay/interbtc/blob/c645294b3257d5a0e37b2613e55b90380100ea9f/crates/vault-registry/src/lib.rs#L1643  
**Severity:** Info  

**Finding:**

```rust
let amount = collateral
    .checked_mul(numerator)
    .ok_or(ArithmeticError::Overflow)?
    .checked_div(denominator)
    .ok_or(ArithmeticError::Underflow)?   // ← should be DivisionByZero
    .try_into()
```

`U256::checked_div` returns `None` only when the divisor is zero. The error is mapped to  
`ArithmeticError::Underflow` rather than `ArithmeticError::DivisionByZero`.

**Impact:** No financial loss. The error is still terminal. Monitoring systems or error handlers  
inspecting the specific variant would misattribute the cause.

---

## CLEAN CHECKS (17)

| # | Function | Invariant | Result |
|---|----------|-----------|--------|
| 1 | calculate_collateral | zero/zero special case returns collateral | clean |
| 2 | calculate_collateral | denominator=0, n!=0 → error (not silent) | clean |
| 3 | calculate_collateral | n==d → result==collateral | clean |
| 4 | calculate_collateral | n<=d → result<=collateral (100k samples) | clean |
| 5 | liquidate() split | exc + for_redeem == liquidated_collateral (100k samples) | clean |
| 6 | liquidate() | uses liquidation_threshold not secure_threshold | clean |
| 7 | redeem_tokens_liquidation | to_be_backed denominator — intended design | clean |
| 8 | issuable_tokens | required_collateral(max_wrapped(col)) <= col (100k samples) | clean |
| 9 | checked_div | rounds DOWN — conservative for issuable calculation | clean |
| 10 | oracle median (even) | overflow requires physically impossible exchange rate | clean (artifact) |
| 11 | get_vault_max_premium_redeem | spec formula = impl within 1-unit truncation | clean |
| 12 | _request_redeem premium | premium_for_amount <= max_premium at threshold | clean |
| 13 | oracle cross-currency | two-leg error <= 1 satoshi in B — known/intended | clean |
| 15 | cancel_redeem punishment | includes transfer_fee_btc — intentional | clean |
| 16 | liquidate() split ratio | for_redeem/liq_col == to_be_redeemed/backed | clean |
| 17 | liquidate() subtraction | to_be_redeemed <= backed always (enforced at request) | clean |
| 18 | get_premium_redeem_vaults | unit tracking correct, truncation conservative | clean |

---

## METHOD NOTES

- Python arithmetic used as faithful replica of Rust integer ops (truncating floor div).  
- FixedU128 (18-decimal, inner=u128) modeled with DIV=10^18 throughout.  
- 100k random samples used for invariant checks (checks 4, 5, 8).  
- Exchange rate semantic: planck-of-collateral per satoshi-of-wrapped (interBTC convention).  
- No z3 SMT used (pure Python arithmetic sufficient for these integer-proportion checks).  
- Source verified character-for-character for all candidates.
