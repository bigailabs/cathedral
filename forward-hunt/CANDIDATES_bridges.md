# SHARP Bridge Hunt — Candidates Receipt
**Date:** 2026-06-06  
**Method:** SHARP (spec-differential + generic invariants)  
**Tools:** z3 Integer arithmetic + manual Python verification  
**Protocols audited:** Stargate v2 / LayerZero OFT, Wormhole NTT TrimmedAmount, Polkadot XCM FixedRateOfFungible  
**Source commit:** HEAD main/master fetched 2026-06-06  

---

## CHECK MATRIX

| # | Protocol | File | Invariant | z3 | Manual | Status |
|---|----------|------|-----------|-----|--------|--------|
| 1 | Stargate OFT | OFTCore.sol | round_trip removeDust→toSD→toLD | UNSAT | CLEAN 10k | CLEAN |
| 2 | Stargate OFT | OFTCore.sol | conservation: toLD(toSD(x)) ≤ x | UNSAT | CLEAN 10k | CLEAN |
| 3 | Stargate OFT | OFTCore.sol | **toSD uint64 silent overflow** (default rate 10^12) | UNSAT | CLEAN (threshold above practical supply) | CLEAN_HIGH_THRESHOLD |
| 4 | Stargate OFT | OFTCore.sol | **toSD uint64 silent overflow** (rate=100, 8dec/6sd) | SAT | SAT | CANDIDATE→PROPOSED |
| 5 | Stargate OFT | OFTCore.sol | **toSD uint64 silent overflow** (rate=1, localDec==sharedDec) | SAT | SAT | PROPOSED |
| 6 | Stargate OFT | OFTCore.sol | monotonicity toSD | UNSAT | — | CLEAN |
| 7 | Wormhole NTT | TrimmedAmount.sol | conservation trim→untrim (9 configs) | UNSAT | CLEAN 5k | CLEAN |
| 8 | Wormhole NTT | TrimmedAmount.sol | receive-path double-trim identity | UNSAT | — | CLEAN |
| 9 | Wormhole NTT | NttManager.sol | TransferAmountHasDust underflow (8 configs) | UNSAT | — | CLEAN |
| 10 | Wormhole NTT | TransceiverStructs.sol | cross-chain encoding byte order | — | Manual: codec layer is correct | CLEAN |
| 11 | Wormhole NTT | NttManager.sol | send→receive value conservation 5k (8 configs) | — | CLEAN 5k | CLEAN |
| 12 | Polkadot XCM | weight.rs | fee monotone in weight | UNSAT | CLEAN 10k | CLEAN |
| 13 | Polkadot XCM | weight.rs | refund ≤ charge | UNSAT | CLEAN 10k | CLEAN |
| 14 | Polkadot XCM | weight.rs | split ≤ combined (floor property) | UNSAT | Verified | CLEAN_KNOWN |
| 15 | Polkadot XCM | weight.rs | zero-fee dust weight | SAT | Verified | CLEAN_KNOWN (by design) |

---

## PROPOSED FINDING (1 total)

### OFT-1: `_toSD()` Silent uint64 Truncation — Spec/Impl Gap

**Status:** PROPOSED (not VERIFIED — human must verify against deployed contracts)  
**Protocol:** Stargate v2 / LayerZero OFT  
**File:** `OFTCore.sol`  
**Source URL:** https://raw.githubusercontent.com/LayerZero-Labs/LayerZero-v2/main/packages/layerzero-v2/evm/oapp/contracts/oft/OFTCore.sol  
**Lines:** `_toSD()` and `sharedDecimals()` comment  

**Intended formula (SPEC):**
The comment on `sharedDecimals()` says:
> "Sets an implicit cap on the amount of tokens, over uint64.max() will need **some sort of outbound cap / totalSupply cap**"
> "Defaults to 6 decimal places to provide up to 18,446,744,073,709.551615 units (max uint64)."

**Actual implementation:**
```solidity
function _toSD(uint256 _amountLD) internal view virtual returns (uint64 amountSD) {
    return uint64(_amountLD / decimalConversionRate);  // BARE CAST — no SafeCast, no require
}

// quoteOFT sets:
uint256 maxAmountLD = type(uint64).max; // Unused in the default implementation.
```

**The SHARP gap:**
- Spec says "will need outbound cap" (advisory)
- Impl: cap in `quoteOFT` is explicitly marked **"Unused in the default implementation"**
- `_toSD` uses `uint64(...)` not `SafeCast.toUint64(...)` — silent truncation, not a revert
- Constructor only checks `localDecimals >= sharedDecimals` — allows `localDecimals == sharedDecimals` (rate=1)

**Invariant violated:**
`_toSD(x) * decimalConversionRate == _removeDust(x)` fails when `x / rate > uint64_max`

**Concrete witness (z3 + Python verified):**
- Config: `localDecimals=18`, `sharedDecimals=18` (valid: constructor only rejects `localDec < sharedDec`), `rate=1`
- Trigger: `amountLD = 18,446,744,073,709,551,616` base units = **18.446... tokens**
- `_toSD(amountLD)` = `uint64(18,446,744,073,709,551,616)` = **0** (wraps to zero)
- Tokens sent: 18.446... | Tokens received: **0** | Loss: **100%**

- Config: `localDecimals=6`, `sharedDecimals=6`, `rate=1`
- Trigger: `amountLD = 18,446,744,073,709,551,616` = **18,446,744,073,709** tokens
- Same truncation to 0.

**Why real:**
1. Arithmetic is faithful to source character-for-character
2. `uint64(x)` in Solidity is documented to truncate (not revert) for `x > 2^64-1`
3. The spec comment acknowledges the constraint but does NOT encode it as a guard
4. The only enforcement path (`quoteOFT` maxAmountLD) is explicitly marked "Unused"
5. Plain Python confirms: `18446744073709551616 & (2**64-1) == 0`

**Why it might be "intended":**
- The spec comment does warn about the cap requirement
- Deployers are expected to override `sharedDecimals()` or add their own cap
- `localDec == sharedDec` is an unusual configuration
- Standard default (sharedDec=6, localDec=18) is SAFE (threshold ~1.8×10^31 base units)

**Why PROPOSED (not dismissed as known-intended):**
- The cap is advisory-only, not enforced in code
- The quoteOFT enforcement point is explicitly disabled ("Unused")
- `_toSD` uses a bare `uint64()` cast, not `SafeCast.toUint64()` which would revert
- The SPEC says "will need cap" but the IMPL provides no cap = spec-impl divergence
- Any deployment with `localDec==sharedDec` and amounts above the threshold silently loses tokens

**Impact scope:**
- Standard OFT deployments with default sharedDec=6 and localDec=18: NOT affected (threshold too high)
- OFT deployments with `sharedDecimals()` overridden to equal `localDecimals`: affected above `uint64_max` base units
- Example: 18dec token with sharedDec=18 overridden → affected above ~18.4 tokens per transfer

---

## ALL-CLEAN PROTOCOLS

### Wormhole NTT TrimmedAmount
- `trim(amt, fromDec, toDec)` → `actualToDecimals = min(min(8, fromDec), toDec)` — correctly uses triple-min
- `untrim(trimmedAmt, toDec)` → exact inverse within precision losses
- Receive-side double-trim `(amount.untrim(toDecimals)).trim(toDecimals, toDecimals)` is a mathematical identity because wire `decimals ≤ 8` always
- `TransferAmountHasDust` revert: `trim().untrim(fromDec) ≤ original` always holds (no underflow possible)
- `TransceiverStructs.sol` codec correctly reads `[decimals][amount]` in consistent byte order with Rust
- 5k random samples across 8 decimal configurations: zero violations

### Polkadot XCM FixedRateOfFungible  
- Fee formula `(ups*rt/WRTS) + (upb*ps/WPSPM)` is monotone in `(rt, ps)` — z3 proved
- `refund(w_r) ≤ charge(w_t)` for all `w_r ≤ w_t` — z3 proved + 10k samples
- Split-vs-combined: `floor((a+b)/n) ≥ floor(a/n)+floor(b/n)` — correct floor property
- Zero-fee dust weights: by design (known quantization artifact, not a decimal conversion bug)

---

## FINDINGS LOG

All 82 check results appended to `/home/fred/code/cathedral-scaffold/hunt-board/findings.jsonl`

Key count: 1 PROPOSED, 8 CANDIDATE (all same underlying finding), 60+ CLEAN, 4 CLEAN_HIGH_THRESHOLD, 2 CLEAN_KNOWN
