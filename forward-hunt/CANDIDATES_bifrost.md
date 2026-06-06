# Bifrost vToken Mint/Redeem Exchange-Rate Math — Hunt Results

**Scope:** vtoken-minting pallet — mint, redeem, rebond, token_pool/exchange-rate accounting  
**Repo:** https://github.com/bifrost-io/bifrost (bifrost-io/bifrost, branch: develop)  
**Commit:** f713bf556a7538bd4dbdaf05eda7c0e7f789fb12  
**Files:**
- `pallets/vtoken-minting/src/impls.rs` (1284 lines)
- `pallets/vtoken-minting/src/lib.rs` (1954 lines)

**Method:** Python transcription of core math + 16 invariant checks (generic + sharp differential)  
**Total checks:** 16  **Clean:** 13  **PROPOSED:** 3  **Verified:** 0

---

## PROPOSED Candidates

### P-1 — vout==0 not guarded before mint_into (HIGH)

**Status:** PROPOSED  
**File:** `pallets/vtoken-minting/src/impls.rs:425-436`  
**Source:** https://github.com/bifrost-io/bifrost/blob/develop/pallets/vtoken-minting/src/impls.rs#L425

**Invariant violated:** `vout > 0` before `mint_into`

**Root cause:**  
`get_v_currency_amount_by_currency_amount` can return 0 when `pool >> issuance` (formula: `floor(net_in * issuance / pool)`). There is no post-computation guard requiring `v_currency_amount > 0` before calling `mint_into`. In Substrate's fungibles implementation, `mint_into(who, 0)` is a no-op — it returns `Ok(())` without minting anything.

**Trigger scenario:**
1. Attacker mints 1 token on fresh pool: `pool = 1, issuance = 1`
2. SLP pallet injects large staking reward `D = 10^18` via `increase_token_pool`: `pool = 10^18 + 1, issuance = 1`
3. Victim mints `V = 10^18` tokens: `vout = floor(10^18 * 1 / (10^18 + 1)) = 0`
4. Victim's `10^18` tokens deposited to entrance_account, pool grows, victim receives 0 vtokens

**Python witness:**
```
pool_after_seed  = 1
issuance         = 1
reward_injection = 10^18  (via increase_token_pool / SLP staking reward)
pool_after       = 10^18 + 1
victim_mint      = 10^18
vout             = floor(10^18 / (10^18 + 1)) = 0
victim_loss      = 10^18 tokens
```

**Attack reachability:** Not fully permissionless. Requires SLP pallet to inject a large reward when pool has only 1 token (issuance=1). An attacker cannot control the SLP timing without being a validator/operator. However, this is a latent bootstrap hazard — if the pool is not seeded with a sufficiently large initial mint, the first staking reward batch could zero out early minters.

**MinimumMint does NOT protect:** `MinimumMint` guards `currency_amount >= min_amount`, not `vout >= 1`. A victim minting `10^18 > MinimumMint` can still receive 0 vtokens.

**Fix:** Add `ensure!(v_currency_amount > BalanceOf::<T>::zero(), Error::<T>::CalculationOverflow)?;` after computing `v_currency_amount` in `mint_without_transfer`.

---

### P-2 — pool==0 bootstrap branch does not check issuance==0 (MEDIUM)

**Status:** PROPOSED  
**File:** `pallets/vtoken-minting/src/impls.rs:1185-1196`  
**Source:** https://github.com/bifrost-io/bifrost/blob/develop/pallets/vtoken-minting/src/impls.rs#L1185

**Invariant violated:** `pool == 0` is only valid as bootstrap state if `issuance == 0`

**Root cause:**
```rust
if BalanceOf::<T>::zero().eq(&token_pool_amount) {
    Ok(currency_amount)  // 1:1 bootstrap rate
} else {
    Ok(multiply_by_rational_with_rounding(...))
}
```
This path returns `currency_amount` (1:1) whenever `token_pool == 0`, regardless of `VtokenIssuance`. If `issuance > 0` and `pool == 0` (broken state), new minters receive vtokens at 1:1 while existing holders' backing is zero — then after the mint, existing holders can now redeem a fraction of the new depositor's tokens.

**Python witness:**
```
pool      = 0
issuance  = 10^18  (existing holders)
net_in    = 10^15  (new minter)
vout      = 10^15  (1:1 bootstrap rate, despite issuance being huge)
new_pool  = 10^15
new_issu  = 10^18 + 10^15

Existing holder E = 10^15 vtokens recovers:
  floor(10^15 * 10^15 / (10^18 + 10^15)) = 999_000_999 tokens
  (expected ~10^15, received ~10^9: ~10^6x haircut)
```

**How pool==0 with issuance>0 arises:**
- Admin calls `set_v_currency_issuance(positive_adjustment)` without touching pool
- SLP calls `decrease_token_pool(full_amount)` while vtokens still outstanding
- Admin calls `update_token_pool(Set, 0)` directly

**Fix:** Guard: `if pool == 0 { if issuance > 0 { return Err(NotSupportTokenType) } else { return Ok(currency_amount) } }`

---

### P-3 — Zero period-start snapshot blocks all operations (MEDIUM / Operational DoS)

**Status:** PROPOSED  
**File:** `pallets/vtoken-minting/src/lib.rs:1821-1855`  
**Source:** https://github.com/bifrost-io/bifrost/blob/develop/pallets/vtoken-minting/src/lib.rs#L1821

**Invariant violated:** Exchange rate check should not block operations when baseline snapshot is zero (uninitialized)

**Root cause:**
`ExchangeRateAtPeriodStart` uses `ValueQuery` with `Default` which resolves to `{token_pool: 0, vtoken_issuance: 0}`. When `ExchangeRateCheckEnabled` is set on a fresh pool, `update_exchange_rate_snapshot` stores `{0, 0}`. Any subsequent `mint`/`redeem`/`rebond` that calls `update_pool_and_issuance` then calls `check_exchange_rate_changes(vtoken, Some(new_pool>0), Some(new_issuance>0))`, which invokes `calculate_rate_change(start=0, start=0, current>0, current>0)`. Because any `start` value is zero, the function returns `None`, which maps to `Err(ExchangeRateCalculationFailed)`. Every operation is blocked until the next period reset provides non-zero snapshot values.

**Python witness:**
```
ExchangeRateCheckEnabled = true
ExchangeRateAtPeriodStart = {token_pool: 0, vtoken_issuance: 0}
First mint attempt: new_pool=1000, new_issuance=1000
calculate_rate_change(0, 0, 1000, 1000) => None
=> Err(ExchangeRateCalculationFailed)
=> mint fails
```

**Trigger path:** Admin enables check (`set_exchange_rate_check_switch(true)`) on a pool where `pool=0, issuance=0`. The `do_period_reset` stores `{0, 0}`. All mints fail for `period` blocks until the next auto-reset.

**Fix:** In `calculate_rate_change`, treat zero start-snapshot as "no change from baseline" (return `Permill::zero()`). Or in `check_exchange_rate_changes`, skip the check if `period_start_snapshot.token_pool == 0 && period_start_snapshot.vtoken_issuance == 0`.

---

## Clean Checks (summary)

| # | Area | Invariant | Result |
|---|------|-----------|--------|
| 1 | mint+redeem | Conservation: tok_out <= deposited | Clean — floor rounding on both legs |
| 2 | redeem | Pool underflow guard | Clean — checked_sub aborts (Python artifact) |
| 3 | mint | Monotonicity: more_in >= more_vout | Clean |
| 4 | mint+redeem | Round-trip: tok_out <= net_in | Clean |
| 5 | fee | Mint fee deduction order | Clean — mul_floor on fee is correct |
| 6 | mint | Sharp: vout = floor(net_in * issuance / pool) | Clean — multiply_by_rational matches spec |
| 7 | redeem | Sharp: token_out = floor(net_v * pool / issuance) | Clean |
| 8 | fee | Fee charging in correct order (gross before rate) | Clean |
| 9 | mint | VtokenIssuance tracks sum of vtokens minted | Clean |
| 10 | redeem | VtokenIssuance decreases by net_v burned | Clean |
| 11 | fee | Redeem fee in vtokens: tok_out <= net_v at 1:1 | Clean |
| 12 | staking | Rebond: pool+unlocking_total delta == -fee | Clean |
| 13 | staking | Rate-change floor rounding (<1 ppm underestimate) | Clean — intended/conservative |

---

## Notes on Out-of-Scope Items Observed

- `calculate_incentive_vtoken_amount`: uses `sqrt(bbBNC_percentage)` with `FixedU128` — potential precision loss but out of scope (incentive, not exchange-rate math)
- `apply_signed_delta` with `i128::MIN` is guarded via `checked_neg()` — no overflow
- `set_v_currency_issuance` root-only admin adjustment of issuance without pool adjustment is an intentional design choice (cross-chain burn reconciliation), not a bug
