# Arithmetic Audit Target Catalogue

**Total entries:** 100  
**Generated:** 2026-06-06  
**Primitive:** Stateless, bounded, closed-form arithmetic audited by SMT/model-checking (Kani, halmos, hevm, CBMC).  
**Lifters:** Kani = Substrate/Rust/Solana-Rust; halmos/hevm = EVM/Solidity/Vyper; CBMC = C/Go bridge; Move Prover = Aptos/Sui.

---

## Tier Structure

| Tier | Criterion | Count |
|------|-----------|-------|
| **Tier 1** | Do first — highest leverage (very high TVL + high encodability + our native lifter) | 22 |
| **Tier 2** | Strong candidates — high value, some friction (slightly harder encoding, or medium TVL) | 48 |
| **Tier 3** | Portfolio broadeners — niche, lower TVL, harder encoding, or closed source | 30 |

**Total value-at-risk represented (rough sum):** ~$170B+ (many protocols overlap; net unique exposure ~$80–100B, dominated by Lido, EigenLayer, Aave, Sky/MakerDAO, Uniswap, ERC4626 ecosystem)

---

## TOP 20 HIGHEST-LEVERAGE TARGETS

These are the entries where value × encodability × churn × source-availability is maximised. Do these first.

### 1. Subtensor / Bittensor — epoch math & Yuma consensus `T002`
**Score: 97** | Substrate/Rust | Staking/emission | Kani  
Direct relevance to Cathedral SN39. The Yuma consensus math lives in `pallets/subtensor/src/epoch/math.rs` and `run_epoch.rs` — I32F32 substrate_fixed throughout, stake-weighted matrix operations, emission division, bond updates. Pure Rust, no heap, bounded matrix dimensions. This is our own backyard and a perfect proof of concept. Kani harness on `epoch_math` is the single highest-leverage first commit we can make.  
Repo: https://github.com/opentensor/subtensor | Path: `pallets/subtensor/src/epoch/`

### 2. Aave v3 — interest rate model + liquidation health factor `T014`
**Score: 96** | EVM/Solidity | Lending | halmos/hevm  
$19B TVL. Health factor = `sum(collateral × liquidationThreshold) / totalDebt` — pure arithmetic. Interest index update in `MathUtils.sol` uses RAY-scaled arithmetic with piecewise-linear rates. Two invariants: health factor monotone decreasing under debt growth; liquidityIndex monotone increasing. High-value, frozen-ish math, clear halmos targets.  
Repo: https://github.com/aave/aave-v3-core | Path: `contracts/protocol/libraries/math/`

### 3. HydraDX / Hydration Omnipool pallet `T001`
**Score: 98** | Substrate/Rust | AMM | Kani  
The richest Substrate AMM math in production. `pallet-omnipool` uses FixedU128 throughout: hub-asset (LRNA) accounting, trade fee on sell/buy, add/remove liquidity share minting. The pool invariant (product of pool weights) and share conservation are directly encodable. C4 audit repo has the full pallet for reference. Active chain.  
Repo: https://github.com/galacticcouncil/hydration-node | Path: `pallets/omnipool/src/`

### 4. Uniswap v3 — FullMath + SqrtPriceMath + TickMath `T012`
**Score: 93** | EVM/Solidity | AMM | halmos/hevm  
$4B TVL, frozen library that all v3 forks inherit. `FullMath.mulDiv` is the canonical safe 256-bit multiply — proving no-overflow for all inputs is a single halmos test. `SqrtPriceMath` token delta calculations and `TickMath` bit-shift approximations are bounded. Community halmos work already exists as a starting point.  
Repo: https://github.com/Uniswap/v3-core | Path: `contracts/libraries/`

### 5. PRBMath — Solidity fixed-point library `T007`
**Score: 93** | EVM/Solidity | Fixed-point-lib | halmos/hevm  
Single library covering $5B+ downstream protocols. `mulDiv`, `avg`, `ceil`, `floor` are all perfectly encodable. `exp`/`ln` hit the transcendental cliff — skip those. The leverage is extraordinary: prove arithmetic invariants once, apply to every protocol using PRBMath. Active maintained codebase.  
Repo: https://github.com/PaulRBerg/prb-math | Path: `src/`

### 6. Morpho Blue — MathLib + LIF liquidation formula `T015`
**Score: 93** | EVM/Solidity | Lending | halmos/hevm  
$4.9B TVL in only 650 lines of Solidity. `MathLib.sol` has `wMulDown`, `wMulUp`, `mulDivDown`, `mulDivUp` — the complete fixed-point multiply library. The Liquidation Incentive Factor formula `LIF = min(M, 1/(β×LLTV+(1-β)))` is closed-form and bounded. The Morpho team has expressed interest in formal verification.  
Repo: https://github.com/morpho-org/morpho-blue | Path: `src/`

### 7. ERC4626 (OpenZeppelin) — share inflation + deposit/redeem `T081`
**Score: 89** | EVM/Solidity | Staking/emission | halmos/hevm  
The universal yield vault standard underlies $10B+ across hundreds of protocols. Two killer invariants: (1) `convertToShares(convertToAssets(shares)) <= shares` (round-trip bound); (2) deposit-then-redeem gets back ≤ deposited. Both are pure `mulDiv` arithmetic. Proving these once covers Yearn, Pendle SY, Sommelier, and every ERC4626-compliant vault.  
Repo: https://github.com/OpenZeppelin/openzeppelin-contracts | Path: `contracts/token/ERC20/extensions/ERC4626.sol`

### 8. Polkadot pallet-staking — NPoS era reward payout `T042`
**Score: 90** | Substrate/Rust | Staking/emission | Kani  
$8B DOT staked, foundation of all Substrate chains. Era reward distribution uses `Perbill` arithmetic — each validator's payout is proportional to era points, then split among nominators by stake fraction. Conservation invariant: sum of all payouts = total era reward. Kani has been explored on this pallet in the Polkadot forum thread.  
Repo: https://github.com/paritytech/polkadot-sdk | Path: `substrate/frame/staking/src/`

### 9. ds-math WAD/RAY — DappHub arithmetic foundation `T010`
**Score: 89** | EVM/Solidity | Fixed-point-lib | halmos/hevm  
Frozen, ~100-line library that is the arithmetic foundation of MakerDAO/Sky DSS and many classic DeFi protocols. `rpow(x, n, b)` is bounded iterative exponentiation — proving no intermediate overflow for all valid inputs is a clean SMT target. WAD/RAY multiply/divide conservation is trivial. Audit once, cover all DSS forks forever.  
Repo: https://github.com/dapphub/ds-math | Path: `src/math.sol`

### 10. Uniswap v2 core — constant-product invariant `T011`
**Score: 90** | EVM/Solidity | AMM | halmos/hevm  
$2B direct TVL + foundation for hundreds of forks. The `xy=k` post-swap check, LP share mint/burn (geometric mean + proportional), and integer `sqrt` are all perfectly bounded integer arithmetic. Proving the product invariant and share conservation covers every Uniswap v2 fork in existence.  
Repo: https://github.com/Uniswap/v2-core | Path: `contracts/`

### 11. MakerDAO DSS — Jug drip + Vat rate accumulator `T024`, `T073`
**Score: 90/87** | EVM/Solidity | Stablecoin | halmos/hevm  
$8B DSS ecosystem. Two targets: `jug.sol` drip uses `rpow(duty, dt, RAY)` — temporal rate accumulation; `vat.sol` tracks `ilk.Art × ilk.rate` debt: proving `Art × rate ≤ line` (debt ceiling) and fold() conservation. Both are pure RAY arithmetic. These are among the oldest production DeFi arithmetic targets.  
Repo: https://github.com/makerdao/dss | Path: `src/`

### 12. FRAME pallet-balances — total issuance conservation `T043`
**Score: 88** | Substrate/Rust | L1 protocol-economics | Kani  
Every Substrate chain uses pallet-balances. The canonical invariant: `sum(all account balances) = TotalIssuance` must hold after every transfer, deposit, and withdrawal. This is the most fundamental arithmetic correctness property in the entire Substrate ecosystem. A Kani proof here covers all downstream chains.  
Repo: https://github.com/paritytech/polkadot-sdk | Path: `substrate/frame/balances/src/`

### 13. Spark Protocol — Aave v3 fork `T040`
**Score: 88** | EVM/Solidity | Lending | halmos/hevm  
$6.8B TVL on an Aave v3 fork. Any harness written for Aave v3 (T014) applies directly. This is a free multiplier on the same investment of work. Two separate audit engagements at marginal cost.  
Repo: https://github.com/marsfoundation/sparklend | Path: `src/`

### 14. Lido — stETH share rate `T018`
**Score: 92** | EVM/Solidity | Staking/emission | halmos/hevm  
$10B+ TVL, second largest DeFi protocol. The share mechanism: `balanceOf(account) = shares[account] × totalPooledEther / totalShares`. Two invariants: (1) share→ETH→share round-trip identity; (2) totalShares conservation across rebases. Pure arithmetic, high value.  
Repo: https://github.com/lidofinance/core | Path: `contracts/`

### 15. Orca Whirlpools — Rust CLMM math `T030`
**Score: 87** | Solana/Rust | AMM | Kani  
$800M TVL Solana CLMM. Pure Rust math module: `tick_math.rs`, `token_math.rs`, `swap_math.rs`. Q64.64 integer arithmetic, tick↔sqrt-price conversion using bit-shift approximations (bounded). The round-trip `tick → sqrtPrice → tick` identity is exact across the full tick domain — a clean Kani proof target.  
Repo: https://github.com/orca-so/whirlpools | Path: `programs/whirlpool/src/math/`

### 16. Acala Honzon CDP — cdp_engine interest + liquidation `T003`
**Score: 92** | Substrate/Rust | Stablecoin | Kani  
Largest Substrate-native DeFi hub. Debit exchange rate accumulates compound interest using FixedU128 arithmetic. Liquidation threshold comparison is a ratio check. The debit→aUSD conversion must be monotone. Perfectly encodable with Kani.  
Repo: https://github.com/AcalaNetwork/Acala | Path: `modules/cdp-engine/src/`

### 17. EigenLayer StrategyBase — share↔underlying conversion `T019`
**Score: 91** | EVM/Solidity | Restaking | halmos/hevm  
$15B restaked. Deposit/withdraw share conversion is `mulDiv(amount, totalShares, totalAssets)` — the same pattern as ERC4626. The share round-trip invariant and monotone share price are clean halmos targets. Active codebase means renewable supply.  
Repo: https://github.com/Layr-Labs/eigenlayer-contracts | Path: `src/contracts/strategies/`

### 18. Solady FixedPointMathLib `T008`
**Score: 91** | EVM/Solidity | Fixed-point-lib | halmos/hevm  
Most-used gas-optimized EVM math library post-2022. `fullMulDiv`, `mulWad`, `divWad`, `mulWadUp`, `divWadUp` are all clean arithmetic targets. Active churn = renewable audit supply. Assembly blocks need hevm (bytecode-level) rather than halmos. Covers all Solady-dependent protocols.  
Repo: https://github.com/Vectorized/solady | Path: `src/utils/FixedPointMathLib.sol`

### 19. Polkadot XCM — asset amount conservation on teleport `T064`
**Score: 84** | Substrate/Rust | Bridge | Kani  
All cross-chain value transfer on Polkadot flows through XCM. The teleport invariant: assets burned on source = assets minted on destination (within fee). The asset arithmetic in `xcm-executor/src/assets.rs` uses `u128` saturating arithmetic. Conservation on teleport is exactly our primitive. High ecosystem leverage.  
Repo: https://github.com/paritytech/polkadot-sdk | Path: `polkadot/xcm/`

### 20. Interlay interBTC — vault collateral ratio `T005`
**Score: 88** | Substrate/Rust | Bridge | Kani  
$50M BTC-backed on Substrate. Vault collateral ratio = `collateral / issuedTokens × price`. Three threshold comparisons: secure, premium, liquidation. All pure `UnsignedFixedPoint` arithmetic. The invariant: a non-liquidated vault must have `ratio ≥ liquidationThreshold` at all times. Clean Kani proof.  
Repo: https://github.com/interlay/interbtc | Path: `crates/vault-registry/src/`

---

## TIER 1 — DO FIRST (22 targets)

*Sorted by priority score. All are high encodability + high TVL + native lifter available.*

| ID | Name | Ecosystem | Category | TVL | Lifter | Score |
|----|------|-----------|----------|-----|--------|-------|
| T003 | Acala CDP / Honzon | Substrate/Rust | Stablecoin | $80M | Kani | 92 |
| T001 | HydraDX Omnipool | Substrate/Rust | AMM | $150M | Kani | 98 |
| T002 | Subtensor Yuma consensus | Substrate/Rust | Emission | $500M | Kani | 97 |
| T014 | Aave v3 interest + liquidation | EVM/Solidity | Lending | $19B | halmos/hevm | 96 |
| T007 | PRBMath | EVM/Solidity | Fixed-point-lib | $5B dep. | halmos/hevm | 93 |
| T015 | Morpho Blue | EVM/Solidity | Lending | $4.9B | halmos/hevm | 93 |
| T012 | Uniswap v3 FullMath/SqrtPrice | EVM/Solidity | AMM | $4B | halmos/hevm | 93 |
| T018 | Lido share rate | EVM/Solidity | Liquid staking | $10B | halmos/hevm | 92 |
| T019 | EigenLayer StrategyBase | EVM/Solidity | Restaking | $15B | halmos/hevm | 91 |
| T008 | Solady FixedPointMathLib | EVM/Solidity | Fixed-point-lib | $3B dep. | halmos/hevm | 91 |
| T011 | Uniswap v2 core | EVM/Solidity | AMM | $2B | halmos/hevm | 90 |
| T042 | pallet-staking NPoS | Substrate/Rust | Emission | $8B | Kani | 90 |
| T024 | MakerDAO DSS Jug/Vat | EVM/Solidity | Stablecoin | $8B | halmos/hevm | 90 |
| T010 | ds-math WAD/RAY | EVM/Solidity | Fixed-point-lib | $10B dep. | halmos/hevm | 89 |
| T040 | Spark Protocol (Aave fork) | EVM/Solidity | Lending | $6.8B | halmos/hevm | 88 |
| T081 | ERC4626 OpenZeppelin | EVM/Solidity | Yield vault | $10B dep. | halmos/hevm | 89 |
| T013 | Uniswap v4 SwapMath | EVM/Solidity | AMM | $1B | halmos/hevm | 88 |
| T043 | pallet-balances issuance | Substrate/Rust | L1 economics | $10B dep. | Kani | 88 |
| T005 | Interlay vault collateral | Substrate/Rust | Bridge | $50M | Kani | 88 |
| T006 | Moonbeam parachain-staking | Substrate/Rust | Emission | $200M | Kani | 88 |
| T073 | MakerDAO vat debt ceiling | EVM/Solidity | Stablecoin | $8B | halmos/hevm | 87 |
| T030 | Orca Whirlpools CLMM math | Solana/Rust | AMM | $800M | Kani | 87 |
| T004 | Acala pallet-dex AMM | Substrate/Rust | AMM | $30M | Kani | 85 |
| T031 | Raydium CLMM Rust math | Solana/Rust | AMM | $1.5B | Kani | 85 |
| T044 | pallet-transaction-payment | Substrate/Rust | L1 economics | $10B dep. | Kani | 85 |
| T046 | Aave aToken rebasing | EVM/Solidity | Lending | $19B | halmos/hevm | 86 |
| T070 | Parallel Finance pallet-loans | Substrate/Rust | Lending | $150M | Kani | 83 |

**Why pallet-loans (T070) is especially notable:** It is Compound v2 math re-implemented in Substrate/Rust — our Kani beachhead + the canonical lending invariant in one package. The borrow index monotonicity and exchange rate conservation proofs transfer directly to EVM targets with a language switch.

---

## TIER 2 — STRONG CANDIDATES (48 targets)

*High value with one mitigating factor: heavier encoding (Newton iteration, some assembly), medium TVL, or mildly unfamiliar toolchain.*

| ID | Name | Ecosystem | Category | TVL | Lifter | Score |
|----|------|-----------|----------|-----|--------|-------|
| T009 | Solmate FixedPointMathLib | EVM/Solidity | Fixed-point-lib | $2B dep. | halmos/hevm | 87 |
| T033 | Kamino Finance lending | Solana/Rust | Lending | $1.1B | Kani | 87 |
| T016 | Compound v3 Comet | EVM/Solidity | Lending | $2.7B | halmos/hevm | 88 |
| T021 | Pendle v2 AMM math | EVM/Solidity | AMM | $3B | halmos/hevm | 86 |
| T041 | Spark Protocol fork (alt) | EVM/Solidity | Lending | $2.4B | halmos/hevm | 88 |
| T027 | Stargate OFT decimal conv. | EVM/Solidity | Bridge | $500M | halmos/hevm | 85 |
| T064 | Polkadot XCM asset amounts | Substrate/Rust | Bridge | $1B | Kani | 84 |
| T076 | Uniswap v3 LiquidityAmounts | EVM/Solidity | AMM | $4B | halmos/hevm | 84 |
| T037 | Euler v2 EVK shares | EVM/Solidity | Lending | $890M | halmos/hevm | 84 |
| T059 | Pyth price EMA math | Solana/Rust | Oracle | $100B dep. | Kani | 83 |
| T063 | substrate_fixed crate | Substrate/Rust | Fixed-point-lib | $500M dep. | Kani | 80 |
| T028 | Wormhole NTT decimal trim | EVM/Solidity | Bridge | $1B | halmos/hevm | 84 |
| T032 | Saber stable-swap-math | Solana/Rust | AMM | $50M | Kani | 83 |
| T062 | SCALE codec round-trip | Substrate/Rust | L1 economics | $50B dep. | Kani | 82 |
| T067 | Bifrost vToken rate | Substrate/Rust | Liquid staking | $200M | Kani | 82 |
| T094 | Meteora DLMM bin math | Solana/Rust | AMM | $500M | Kani | 81 |
| T038 | Fluid Protocol | EVM/Solidity | Lending | $1.6B | halmos/hevm | 82 |
| T052 | Liquity v1 ICR/TCR | EVM/Solidity | Stablecoin | $500M | halmos/hevm | 81 |
| T047 | Uniswap v2 LP share mint | EVM/Solidity | AMM | $2B | halmos/hevm | 84 |
| T088 | MasterChefV2 reward/share | EVM/Solidity | Emission | $200M | halmos/hevm | 78 |
| T075 | Compound v3 present value | EVM/Solidity | Lending | $2.7B | halmos/hevm | 82 |
| T050 | Compound v2 classic | EVM/Solidity | Lending | $5B fork dep. | halmos/hevm | 82 |
| T017 | Curve StableSwap get_D | EVM/Solidity | AMM | $2B | halmos/hevm | 85 |
| T089 | Convex cliff reduction | EVM/Solidity | Emission | $2B | halmos/hevm | 79 |
| T078 | FRAX collateral ratio | EVM/Solidity | Stablecoin | $1B | halmos/hevm | 78 |
| T025 | Sablier v2 Lockup linear | EVM/Solidity | Vesting | $500M | halmos/hevm | 83 |
| T065 | Polkadot NPoS election math | Substrate/Rust | Emission | $8B | Kani | 78 |
| T039 | Moonwell Compound fork | EVM/Solidity | Lending | $700M | halmos/hevm | 80 |
| T049 | Chainlink CCIP decimals | EVM/Solidity | Bridge | $5B | halmos/hevm | 82 |
| T066 | Snowbridge amount conv. | Substrate/Rust | Bridge | $100M | Kani | 78 |
| T020 | ABDKMath64x64 | EVM/Solidity | Fixed-point-lib | $1B dep. | halmos/hevm | 83 |
| T048 | Rocket Pool rETH rate | EVM/Solidity | Liquid staking | $2B | halmos/hevm | 80 |
| T022 | Balancer v3 WeightedMath | EVM/Solidity | AMM | $1.5B | halmos/hevm | 82 |
| T077 | Balancer v2 StableMath | EVM/Solidity | AMM | $1.5B | halmos/hevm | 80 |
| T056 | Drift Protocol vAMM | Solana/Rust | Perps | $500M | Kani | 82 |
| T023 | Frax ERC4626 + TWAMM | EVM/Solidity | Stablecoin | $2B | halmos/hevm | 84 |
| T069 | Astar dApp staking | Substrate/Rust | Emission | $300M | Kani | 78 |
| T080 | Pendle SYToken yield | EVM/Solidity | Emission | $3B | halmos/hevm | 80 |
| T090 | Yearn v3 vault shares | EVM/Solidity | Yield vault | $500M | halmos/hevm | 79 |
| T034 | Thala Move stablecoin | Move/Aptos | Stablecoin | $100M | Move Prover | 75 |
| T074 | Liquity v2 BOLD interest | EVM/Solidity | Stablecoin | $200M | halmos/hevm | 78 |
| T051 | Venus Protocol | EVM/Solidity | Lending | $2B | halmos/hevm | 78 |
| T035 | Cosmos SDK distribution | Cosmos/Go | Emission | $3B | CBMC/custom | 72 |
| T036 | Osmosis CL math | Cosmos/Go | AMM | $500M | CBMC/custom | 72 |
| T079 | Curve tricrypto-ng | EVM/Vyper | AMM | $500M | hevm | 76 |
| T060 | Switchboard oracle math | Solana/Rust | Oracle | $10B dep. | Kani | 80 |
| T026 | OpenZeppelin VestingWallet | EVM/Solidity | Vesting | $1B dep. | halmos/hevm | 78 |
| T095 | Solend/Marginfi lending | Solana/Rust | Lending | $300M | Kani | 78 |

---

## TIER 3 — PORTFOLIO BROADENERS (30 targets)

*Smaller TVL, harder encoding, niche ecosystem, or access friction. Good for demonstrating breadth but not primary resource allocation.*

| ID | Name | Ecosystem | Category | TVL | Lifter | Score | Blocker |
|----|------|-----------|----------|-----|--------|-------|---------|
| T053 | GMX v2 price impact | EVM/Solidity | Perps | $600M | halmos/hevm | 76 | sqrt/power in pricing |
| T029 | Across Protocol fee | EVM/Solidity | Bridge | $200M | halmos/hevm | 78 | Simple but small |
| T082 | Synthetix v3 debt shares | EVM/Solidity | Perps | $300M | halmos/hevm | 74 | Oracle dependency |
| T091 | Gearbox v3 health factor | EVM/Solidity | Lending | $150M | halmos/hevm | 75 | Array sum |
| T057 | Maple Finance amortisation | EVM/Solidity | Lending | $300M | halmos/hevm | 74 | Institutional niche |
| T058 | Centrifuge tranche NAV | EVM/Solidity | Lending | $300M | halmos/hevm | 72 | Oracle NAV OOS |
| T068 | Pendulum Spacewalk bridge | Substrate/Rust | Bridge | $20M | Kani | 70 | Small TVL |
| T071 | KILT parachain staking | Substrate/Rust | Emission | $50M | Kani | 72 | Small TVL |
| T072 | Mangata DEX AMM | Substrate/Rust | AMM | $30M | Kani | 74 | Small TVL |
| T097 | NEAR staking pool | NEAR/Rust | Emission | $500M | Kani | 74 | Ecosystem gap |
| T061 | ink! PSP22 token balance | Substrate/Rust | L1 economics | $100M dep. | Kani | 76 | Simple math |
| T083 | Sommelier vault fee | EVM/Solidity | Yield vault | $200M | halmos/hevm | 72 | Small |
| T045 | Sushi Trident TWAP | EVM/Solidity | AMM | $300M | halmos/hevm | 74 | Smaller scope |
| T085 | Maple v2 drawdown math | EVM/Solidity | Lending | $300M | halmos/hevm | 73 | Institutional |
| T088 | Convex cvxCRV minting | EVM/Solidity | Emission | $2B | halmos/hevm | 79 | Frozen |
| T092 | Tokemak v2 NAV/share | EVM/Solidity | Yield vault | $100M | halmos/hevm | 71 | Small |
| T054 | dYdX v4 funding rate | Cosmos/Go | Perps | $1B | CBMC/custom | 70 | Go toolchain |
| T086 | deBridge fee deduction | EVM/Solidity | Bridge | $200M | halmos/hevm | 70 | Small |
| T096 | Sei DEX order matching | Cosmos/Go | AMM | $200M | CBMC/custom | 65 | Go toolchain |
| T087 | Squid Router fee | EVM/Solidity | Bridge | $50M | halmos/hevm | 65 | Small |
| T098 | ICP ledger arithmetic | ICP/Rust | L1 economics | $300M | Kani | 68 | Ecosystem niche |
| T041 | JustLend Compound fork | EVM/Tron | Lending | $2.4B | halmos/hevm | 76 | Tron compatibility |
| T041 | Venus Compound fork | EVM/Solidity | Lending | $2B | halmos/hevm | 78 | BNB chain |
| T084 | Ribbon Black-Scholes approx | EVM/Solidity | Perps | $150M | halmos (partial) | 45 | Transcendental cliff |
| T099 | Tezos FA2 arithmetic | Tezos/Michelson | L1 economics | $100M | Mi-Cho-Coq | 48 | Different toolchain |
| T093 | Multichain (reference) | EVM/Solidity | Bridge | Defunct | halmos/hevm | 50 | Defunct |
| T055 | Hyperliquid clearing | EVM/custom L1 | Perps | $4B | hevm (bytecode) | 55 | Closed source |
| T100 | Polkadot-JS SCALE (JS) | JavaScript | L1 economics | Ref only | none | 20 | Not Kani-targetable |

---

## ENCODING NOTES BY CATEGORY

### What encodes cleanly (HIGH priority)
- **Share↔asset conversions** (`mulDiv` with rounding) — ERC4626, stETH, rETH, EigenLayer, all lending protocols
- **Constant-product AMM** — `xy=k` post-swap check, LP share mint (geometric mean + proportional)
- **Interest index accumulation** — monotone increasing, bounded per block
- **Health factor / collateral ratio** — pure ratio arithmetic
- **Fixed-point multiply libraries** — WAD, RAY, Q64.64, Q96, FixedU128, Perbill
- **Emission distribution** — reward-per-share accumulator, era payout, round-trip sum
- **Decimal/bridge conversion** — scale up/down, trim, round-trip
- **Vesting schedules** — linear interpolation, cliff gate, monotone

### What hits the transcendental cliff (LOW — exclude or partial)
- `exp`, `ln`, `log2` in fixed-point (PRBMath, ABDKMath) — skip these functions
- Black-Scholes approximation (polynomial is bounded but complex — partial)
- Weighted pool `powDown`/`powUp` in Balancer (delegates to exp/ln)
- Continuous compounding exact (approximated by Taylor series — bounded terms only)

### Newton iteration (MEDIUM — bounded unroll)
- Curve `get_D`, `get_y` — 255 iteration bound; SMT can unroll to depth 10-20 and prove convergence for most inputs
- Saber/Meteora stable-swap — same pattern

### Go/Cosmos (MEDIUM — CBMC route)
- Cosmos SDK uses arbitrary-precision `Dec` type; conservation invariants remain encodable but need a Go→C harness
- Target the inner arithmetic functions, not the full transaction pipeline

---

## SOURCE AVAILABILITY SUMMARY

| Status | Count |
|--------|-------|
| Fully open source, public GitHub | 96 |
| Open source but hosted on GitLab | 1 (substrate_fixed) |
| Closed/proprietary source | 1 (Hyperliquid) |
| Defunct/archived | 1 (Multichain) |
| JS reference only | 1 (polkadot-js) |

**96 of 100 targets are fully open source.** For bytecode-only targets (Hyperliquid), hevm can operate at the EVM bytecode level if the deployed contracts are on-chain.

---

## CHURN CATEGORIES (SUPPLY SIGNAL)

| Churn | Count | Implication |
|-------|-------|-------------|
| Very active | 12 | New commits weekly; high renewable supply |
| Active | 38 | Regular releases; good renewal cadence |
| Steady | 30 | Quarterly updates; reliable but slower renewal |
| Frozen | 17 | No further changes; single audit, permanent signal |

Frozen targets (Uniswap v2, v3, ds-math, ABDKMath, Saber) offer permanent one-time value. Active targets (Aave v3, EigenLayer, Subtensor, Orca, Raydium, Uniswap v4) offer renewable supply as math changes over time.

---

## LIFTER DISTRIBUTION

| Tool | Count | Ecosystem |
|------|-------|-----------|
| Kani | 35 | Substrate/Rust, Solana/Rust, NEAR/Rust |
| halmos/hevm | 55 | EVM/Solidity, EVM/Vyper |
| CBMC or custom | 6 | Cosmos/Go, C bridge |
| Move Prover | 1 | Aptos/Move |
| Mi-Cho-Coq | 1 | Tezos |
| None | 2 | JS, closed |

**Our strongest leverage:** Kani for the Substrate/Rust beachhead (35 targets), halmos/hevm for EVM (55 targets). These cover $80–100B in unique exposed value.
