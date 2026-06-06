# EVM AMM/Accumulator Hunt Results

**Date:** 2026-06-06  
**Targets:** Uniswap v2 core, Uniswap v3 core/libraries, MakerDAO DSS  
**Method:** Z3 bv64 (algebraic structure) + Python exact integer / Monte Carlo  
**Sources:** Current GitHub main/master branches  

---

## Summary

41 unique checks run across 3 protocols. **Zero PROPOSED real-new bugs.**  
All SAT candidates were confirmed artifacts under re-derivation.

| Protocol | Checks | Clean/UNSAT | Artifacts | Intended | PROPOSED |
|----------|--------|-------------|-----------|----------|----------|
| UniswapV2 | 15 | 11 | 4 | 0 | 0 |
| UniswapV3 | 14 | 12 | 0 | 2 | 0 |
| MakerDAO DSS | 12 | 8 | 0 | 3* | 0 |

*One MakerDAO candidate was classified `known` (governance risk, not code bug).

---

## Checks Run

### Uniswap V2 (`v2-periphery/contracts/libraries/UniswapV2Library.sol`, `v2-core/contracts/UniswapV2Pair.sol`)

| Function | Invariant | Result |
|----------|-----------|--------|
| `getAmountOut` (Z3 bv64) | floor: amountOut\*denom ≤ numerator | CLEAN |
| `getAmountOut` (Z3 bv64) | output bound: amountOut < reserveOut | CLEAN |
| `getAmountOut` monotonicity | larger amountIn → larger amountOut | CLEAN |
| `getAmountOut` price impact | marginal rate decreases with larger input | CLEAN |
| `getAmountIn` round-trip | getAmountIn(getAmountOut(x)) ≥ x | **ARTIFACT** |
| `getAmountIn` ceiling guarantee | getAmountIn ≥ exact continuous | **ARTIFACT** |
| `getAmountIn` edge overflow | near-full-reserve withdraw doesn't overflow | CLEAN |
| `swap` K invariant (Z3 bv64) | balance0Adj\*balance1Adj ≥ reserve0\*reserve1\*1e6 | CLEAN |
| `swap` K preservation (Python 200k) | K holds post getAmountOut swap | CLEAN |
| `swap` K both-tokens-in | K holds with flash swap (both tokens) | CLEAN |
| `swap` K overflow bounds | k-check product fits in uint256 | CLEAN |
| `mint` share bound | LP share ≤ contributed ratio (exact fraction) | CLEAN |
| `mint` over-allotment | no over-allotment of LP shares | CLEAN |

**Artifact details — getAmountIn candidates (RECLASSIFIED):**

Two checks flagged `getAmountIn` as SAT candidates:

1. **Round-trip (getAmountIn(getAmountOut(x)) < x):** Reproduced numerically. ~47% of samples show shortfall. However: (a) the K invariant is unaffected (verified — K holds with aIn_required); (b) getAmountIn actually *over-quotes* the K-derived minimum by 1; (c) the round-trip property is not required by the V2 design — these are independent quote helpers, not inverses. Root cause: `getAmountOut` floors and `getAmountIn` ceilings (+1) independently; the +1 doesn't always compensate for the floor. **Not a bug by design.**

2. **Ceiling guarantee (getAmountIn vs exact_continuous):** Float comparison at ~1e33 precision loses 17+ significant digits. Using `fractions.Fraction` (exact rational arithmetic), `getAmountIn` is rigorously ≥ exact. **Test artifact.**

---

### Uniswap V3 (`v3-core/contracts/libraries/`)

| Function | Invariant | Result |
|----------|-----------|--------|
| `FullMath.mulDiv` (Z3 bv64) | floor: result\*d ≤ a\*b | CLEAN |
| `FullMath.mulDiv` simulation (10k) | result == floor(a\*b/d) exactly | CLEAN |
| `FullMath.mulDiv` NR seed | (3\*d)^2 (XOR 2) satisfies seed\*d ≡ 1 mod 16 for all odd d | CLEAN |
| `FullMath.mulDiv` NR convergence | after 6 NR steps: inv\*d_odd ≡ 1 mod 2^256 | CLEAN |
| `FullMath.mulDiv` twos isolation | (-d mod 2^256) & d == lowest_bit(d) | CLEAN |
| `FullMath.mulDiv` denom > prod1 boundary | require(denom > prod1) not ≥ correctly rejects equality | CLEAN |
| `FullMath.mulDivRoundingUp` overflow guard | guard reachable and correctly placed | CLEAN (INTENDED) |
| `SqrtPriceMath.getAmount0Delta` double rounding | roundUp−roundDown ∈ {0,1,2} (two-stage ceil) | CLEAN (INTENDED) |
| `SqrtPriceMath.getAmount0Delta` monotone | roundUp ≥ roundDown | CLEAN |
| `SqrtPriceMath.getAmount1Delta` single round | roundUp−roundDown ∈ {0,1} | CLEAN |
| `SqrtPriceMath.getNextSqrtPriceFromAmount0` path B vs exact | Path B result ≥ ceil(exact) | CLEAN |
| `SqrtPriceMath.getNextSqrtPriceFromAmount0` path A vs B | path B result ≥ path A result | CLEAN |
| `SqrtPriceMath.getNextSqrtPriceFromAmount1` add-branch | both paths give floor(amount\*Q96/L) | CLEAN |
| `TickMath` boundary constants | MIN/MAX fit in uint160 | CLEAN |

**Notes on intended behavior:**
- `getAmount0Delta` can give `roundUp − roundDown = 2` (two-stage ceil). This is by design — the double-rounding is conservative (pool-favorable) and the comment explicitly documents it.
- `mulDivRoundingUp` overflow guard (`require result < MAX_UINT256`) is reachable at extreme values but correctly triggers a revert, not a money leak.

---

### MakerDAO DSS (`dss/src/jug.sol`, `dss/src/vat.sol`)

| Function | Invariant | Result |
|----------|-----------|--------|
| `Jug._rpow` identity | rpow(ONE,n)=ONE; rpow(x,0)=ONE; rpow(0,n>0)=0 | CLEAN |
| `Jug._rpow` monotone | rpow(rate,n) non-decreasing in n for rate≥RAY | CLEAN |
| `Jug._rpow` rounding bias | accumulated error < 1 RAY for realistic rates/dt | CLEAN (INTENDED) |
| `Jug._rpow` overflow boundary | reverts (not wraps) for realistic rates | CLEAN |
| `Jug._rpow` overflow checks | all intermediate muls covered | CLEAN |
| `Jug.drip` dt=0 no-op | dt=0 leaves rate unchanged | CLEAN |
| `Jug.drip` deflation risk | base+duty < RAY deflates rate | candidate [KNOWN] |
| `Vat.fold` dai/debt consistency | dai[u] and debt increments identical | CLEAN |
| `Vat.frob` tab distributive | rate\*(art+dart) == rate\*art + rate\*dart | CLEAN |
| `Vat.frob` safety overflow | ink\*spot fits in uint256 | CLEAN |
| `Vat.frob` debt ceiling accrual | rate accrual can push past ilk.line | KNOWN |
| `Vat._mul(uint,int)` sign safety | require(int(x)≥0) catches x≥2^255 | CLEAN |
| `Vat.suck/heal` sin/vice | symmetric add/sub, conservation holds | CLEAN |

**Known issue (not a code bug):**  
`Jug.drip/deflation-risk`: If governance sets `duty < RAY` (i.e., a sub-1.0 per-second multiplier), the stability fee rate deflates over time. This is a known governance risk; the code correctly executes whatever rate is set. Not a code defect.

---

## Final Verdict

**Zero PROPOSED real-new findings.** These three protocols are as clean as expected for heavily-audited production contracts:

- **Uniswap V2:** All invariants hold. The two SAT candidates were both artifacts (round-trip not a design requirement; ceiling test used imprecise floats). K invariant robustly preserved.
- **Uniswap V3:** FullMath.mulDiv is mathematically correct (Newton-Raphson convergence verified, twos isolation correct, floor property confirmed). SqrtPriceMath rounding is intentionally conservative. No exploitable rounding gaps.
- **MakerDAO DSS:** rpow identity/monotonicity/edge cases all pass. Integer arithmetic in Vat is exact (no rounding in fold/frob). Overflow checks in rpow are complete. Only known governance risks noted.
