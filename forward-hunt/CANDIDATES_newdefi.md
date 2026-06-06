# SHARP Hunt — New DeFi Protocols
**Date:** 2026-06-06  
**Method:** Differential (spec vs impl) + Generic invariants (z3 + Python)  
**Protocols:** Sablier v2 Lockup, Pendle v2 MarketMathCore, Euler v2 EVK, Fluid Protocol (skipped)

---

## Executive Summary

**Zero PROPOSED findings. Zero CANDIDATE findings.** All arithmetic invariants hold across all three auditable protocols. Every apparent lead was either algebraically impossible, intentionally documented rounding, or a control-flow revert (not silent arithmetic).

This is an honest zero. The method works — it found a real bug in the prior session. These protocols are cleaner.

---

## Protocol Checks

### Sablier v2 LockupLinear

**Source:** `sablier-labs/evm-monorepo` — `lockup/src/libraries/LockupMath.sol:calculateStreamedAmountLL`

#### Formula (from NatSpec)
```
x = floor((blockTs - cliffTime) / granularity) * granularity / streamableTotalDuration
streamedPortion = x * streamableAmount
streamedAmount  = unlockAmountsSum + streamedPortion
```

#### Impl (character-for-character from Solidity)
```solidity
elapsedTimeInGranularityUnits = (blockTimestamp - cliffTime) / granularity   // floor div
streamedPortion = elapsedTimeInGranularityUnits * streamableAmount * granularity / streamableTotalDuration
streamedAmount  = unlockAmountsSum + uint128(streamedPortion)
```

#### Check: BOUND — streamedPortion < streamableAmount
**Status: CLEAN — algebraically proven.**

Proof:
- `elapsed = bt - ct < et - ct = streamableRange` (strictly, since `bt < et`)
- `elapsedInG * g <= elapsed < streamableRange`
- Therefore: `elapsedInG * g * streamable < streamableRange * streamable`
- `floor(elapsedInG * g * streamable / streamableRange) <= streamable - 1 < streamable` ✓

The code comment's claim is correct. The uint128 cast is safe.

**Verification:** 2M random trials, 0 violations.

#### Check: CONSERVATION — streamedAmount <= depositedAmount
**Status: CLEAN — follows directly from BOUND.**
`streamedAmount = unlockSum + portion < unlockSum + streamable = deposit` ✓

#### Check: MONOTONICITY — streamed(t2) >= streamed(t1) when t2 > t1
**Status: CLEAN — proven.**
`elapsed2 >= elapsed1 => floor(elapsed2/g) >= floor(elapsed1/g) => portion2 >= portion1` ✓

**Verification:** 1M random monotonicity trials, 0 violations.

#### Check: DIFFERENTIAL — spec formula ordering vs impl ordering
**Status: CLEAN.**
- Spec: `floor(elapsedInG * g * streamable / dur)` — single final division
- Impl: `floor(elapsedInG * streamable * g / dur)` — same numerator (multiplication commutes), same single division
- Identical results by integer arithmetic commutativity.

**Note:** Z3 bitvector queries timed out on all Sablier checks due to nonlinear arithmetic (UDiv chains with 64-bit BVs). The algebraic proofs and Python random testing are the receipts.

---

### Sablier v2 LockupDynamic (LD)

**Source:** `lockup/src/libraries/LockupMath.sol:calculateStreamedAmountLD`

#### Formula (NatSpec)
```
f(x) = x^exp * csa + Σ(esa)
x = elapsedTime / segmentDuration  (SD59x18 fixed point)
```

#### Check: BOUND — segmentStreamed <= segmentAmount
**Status: CLEAN.**
- `x ∈ [0, 1)` (elapsed < segmentDuration since bt < et)
- `x^exp ∈ [0, 1]` for any positive exponent
- `segmentStreamedAmount ≤ currentSegmentAmount`
- Guard present: `if segmentStreamedAmount > currentSegmentAmount { return max(previousAmounts, withdrawnAmount) }`

#### Near-miss: SD59x18 pow revert
When `elapsedTimePercentage` is very close to 0 and exponent is very large, `pow(x, exp)` calls `exp(ln(x) * exp)`, which can hit `MIN_NATURAL_EXPONENT = -41e18` and revert. This is a **control-flow** revert (not silent arithmetic), and is PRBMath's designed behavior. Out of scope for SHARP.

---

### Pendle v2 MarketMathCore

**Source:** `pendle-finance/pendle-core-v2-public` — `contracts/core/Market/MarketMathCore.sol`

#### Check: addLiquidityCore — non-binding token slippage
**Status: CLEAN — algebraically proven.**

Initial hypothesis: when SY is binding, `ptUsed = rawDivUp(totalPt * lpBySy, totalLp)` could exceed `ptDesired`.

**Proof this CANNOT happen:**
- SY-binding condition (integer): `floor(s*L/S) < floor(p*L/P)`
- Therefore: `floor(s*L/S) < p*L/P` (since `floor(p*L/P) ≤ p*L/P`)
- Therefore: `P * floor(s*L/S) < p*L`
- Since `P * floor(s*L/S)` is an integer: `P * floor(s*L/S) ≤ p*L - 1`
- `rawDivUp(P * floor(s*L/S), L) = floor((P*floor(s*L/S) + L - 1) / L)`
- `≤ floor((p*L - 1 + L - 1) / L) = floor(p + (L-2)/L) = p` (for L ≥ 2)
- For L=1: `floor(s*1/S) = 0` (since s < S in SY-binding), so ptUsed = 0 ≤ p ✓

Similarly proven for PT-binding: `syUsed ≤ syDesired` always.

**Verification:** Exhaustive integer search 1–49, 5M large random trials — 0 violations.

#### Check: calcTrade fee direction
**Status: CLEAN.**
- `feeRate = e^(lnFeeRateRoot * t / IMPLIED_RATE_TIME) ≥ 1` (exponent ≥ 0)
- Buy-PT: `preFeeAsset < 0`, `(1 - feeRate) ≤ 0`, `fee_amount = negative × non-positive ≥ 0`, user pays more. ✓
- Sell-PT: `fee_amount = (-(preFee × (1-feeRate)) / feeRate).neg() > 0`, user gets less. ✓

#### Check: _logProportion spec vs impl
**Status: CLEAN.**
- Spec: `ln(p / (1-p))`
- Impl: `ln(p.divDown(1e18 - p))`
- Exact match.

#### Check: reserve fee sign
**Status: CLEAN.**
Fee is always ≥ 0 in both directions (shown above). `netAssetToReserve = fee * reserveFeePercent / 100 ≥ 0`. ✓

---

### Euler v2 EVK

**Source:** `euler-xyz/euler-vault-kit` — `src/EVault/`

#### Check: Liquidation yield — double floor rounding
**Status: CLEAN.**

- Spec: `floor(liabilityValue * 1e18 * collateralBalance / (discountFactor * collateralValue))`
- Impl: `floor(floor(liabilityValue * 1e18 / discountFactor) * collateralBalance / collateralValue)`

**Analytical bound:** Let `k_true = lv * 1e18 / df` (exact), `k = floor(k_true)`. Then `k_true - k < 1`, so `(k_true - k) * cb / cv < 1`. Therefore `spec - impl ≤ 1`.

**Verification:** 1M random trials, max observed shortfall = 1 unit. **Standard single-floor rounding.**

#### Check: decreaseBorrow totalBorrows dust
**Status: CLEAN / INTENTIONAL.**
When `Owed` has fractional bits (value mod 2^SHIFT > 0), repaying `assets` leaves `(2^SHIFT - r)` fractional bits in `totalBorrows`. Max dust < 1 asset unit. Code comment explicitly: "The rounding is an additional cost to the user and is recorded both in user's account and in `totalBorrows`." Intended.

#### Check: Interest accumulator overflow protection
**Status: CLEAN.**
The check `(oldBorrows == oldBorrows * newAcc / newAcc)` correctly detects uint256 overflow. When TRUE (no overflow): `newBorrows = oldBorrows * newAcc / oldAcc` (correct). When FALSE (overflow): old borrows kept — conservative, safe.

#### Near-miss: getCurrentOwed silent truncation
`OwedLib.mulDiv` calls `TypesLib.toOwed()` which checks `> MAX_SANE_DEBT_AMOUNT` and **reverts** — not silent truncation. At 920% max APR over long periods, a max-borrow position could freeze. Control-flow risk, not silent arithmetic. Out of scope.

---

### Fluid Protocol (Instadapp)

**Status: SKIPPED — source unavailable.**
Main vault contract repo (`Instadapp/fluid-contracts-public`) does not expose the complete vault math. Cannot perform character-for-character source analysis.

---

## Method Notes

### What z3 contributed
- z3 bitvector SMT was used for Sablier LL checks with 32-bit and 64-bit BVs. All queries timed out (20s) due to nonlinear integer arithmetic (chains of UDiv with multiplication). This is a known limitation of bit-vector SMT for `div`-heavy formulas.
- Python random testing (1M–5M samples) and algebraic proofs substituted effectively.
- z3 was faster for pure linear checks (no div) — the Euler and Pendle Python loops didn't need it.

### Why honest zero is valid
1. These protocols are more mature and more audited than the initial targets.
2. Sablier LL math is simple integer arithmetic with a proven tight bound.
3. Pendle's addLiquidity rounding was the most promising lead — the algebraic kill shows the floor in the binding-side LP calculation acts as a natural ceiling for the non-binding token.
4. Euler's liquidation math uses deliberate rounding choices that are all documented.

### What wasn't checked (future work)
- Fluid Protocol (no source)
- Pendle yield token accrual (PYIndex.syToAsset / assetToSy edge cases with very large exchange rates)
- Euler IRM (Interest Rate Model) — the external call result is capped but the IRM contract itself wasn't analyzed
- Sablier LT (Tranched) clock precision (tranche timestamp == blockTimestamp boundary)
