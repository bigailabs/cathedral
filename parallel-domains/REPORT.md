# Parallel Domains for Bounded Arithmetic Invariant Checking
## Beyond Smart Contracts — TAM Expansion Research
*Generated 2026-06-06*

---

## Our Primitive — Fit Test Recap

We find inputs that violate a **stateless, bounded, closed-form arithmetic invariant** (integer/fixed-point: conservation, monotonicity, bounds, round-trip identity, no-overflow/precision-loss) by exhaustive SAT/SMT search, returning a self-verifying counterexample. Sound, bounded-incomplete.

A domain fits if:
- (a) Self-contained numeric computation (formula on inputs, not long stateful process)
- (b) Bounded integer/fixed-point arithmetic
- (c) Checkable "should always hold" numeric rule
- (d) Wrong answer = real money / safety cost
- (e) Code is accessible and changes (renewable supply)

---

## TOP 3 STRONGEST NON-CRYPTO FITS

### 1. Embedded / Safety-Critical Fixed-Point Control (STRONG)

**Why it leads:** This domain has already proven that stateless fixed-point arithmetic bugs kill people and destroy hardware. The Patriot missile (28 dead, 1991), Ariane 5 (~$370-500M, 1996), and dozens of avionics/automotive incidents are all textbook bounded fixed-point arithmetic failures -- the exact class our primitive catches. Regulators (DO-178C/DO-333, ISO 26262) now *require* formal methods, creating pull that does not exist in other domains. Incumbents (AbsInt, TrustInSoft, Frama-C) exist but are expensive, slow, and human-services-heavy. A SAT-network that produces self-verifying counterexamples as deliverables maps directly to certification evidence artifacts.

**Specific invariants:**
- Time accumulator does not overflow (Patriot: 24-bit register, 100h uptime -> 0.34s drift -> 500m miss)
- Conversion between integer representations is lossless over full input range (Ariane 5: horizontal velocity > 32,767 -> 16-bit overflow -> $370M rocket)
- Fixed-point multiply/accumulate result stays in bounds given worst-case input combination
- No integer wrap in loop counters, timer increments, or index arithmetic
- Saturation arithmetic applied consistently (DSP: missed saturation in one code path while applied in another)

**Real bug evidence and dollar cost:**
- Patriot missile, 1991: 24-bit fixed-point truncation of 1/10 in binary. Accumulated error over 100 hours = 0.34s. 28 soldiers killed at Dhahran barracks.
- Ariane 5, 1996: Reused Ariane 4 code. 64-bit float horizontal velocity converted to 16-bit signed integer. First launch self-destructs 37 seconds in. Cost: >$370M (rocket + payload).
- DO-178C/DO-333 formal supplement mandates formal coverage; ISO 26262 ASIL-D requires formal verification for highest-safety automotive code. This is regulatory demand, not optional.

**Code sourcing:**
- Open-source RISC-V cores (YosysHQ riscv-formal framework, verified against ISA arithmetic spec)
- Automotive AUTOSAR reference implementations (publicly available C code)
- Open-source avionics test suites (NASA/JPL open-source code, Frama-C test cases)
- MISRA-C compliant embedded C from customers -- CBMC already ingests ANSI-C directly

**Lifting tool:**
- CBMC (ANSI-C -> SAT/SMT, used by Bosch, GE, AWS; won ETAPS 2025 Test of Time Award)
- Frama-C / WP plugin (C -> Why3 -> Z3/CVC5)
- TrustInSoft Analyzer (commercial, expensive -- our attack surface)

**Incumbents / moat assessment:**
- Synopsys/Cadence own the RTL-level hardware space; AbsInt/TrustInSoft own DO-178C services
- BUT: these are expensive consulting-first models; no crowdsourced compute marketplace exists
- RISC-V democratization is creating a new class of chip designers who cannot afford Synopsys -- real gap
- Our angle: self-verifying counterexample as cheap audit artifact, not full certification suite

**Fit verdict: STRONG**
The invariants are exactly bounded integer/fixed-point arithmetic. Bugs are documented, costs are catastrophic, regulatory pull exists, CBMC already does the lifting, and the market is underserved for smaller players. Closest to a direct port of the crypto audit model into a domain with real body count.

---

### 2. Traditional Finance -- Settlement / Ledger / FX Arithmetic (STRONG)

**Why it ranks #2:** Financial arithmetic runs on bounded integer and fixed-point representations (basis points, int128 balances, fixed-decimal rounding modes). Every dollar that moves through a clearing engine runs through arithmetic that must satisfy conservation invariants. The Vancouver Stock Exchange (1982) lost half its index value over 22 months from a floor() vs round() one-character bug, accumulating ~25 points/month of phantom loss. Payroll rounding bugs generate $600K+ DOL settlements. Knight Capital lost $440M in 45 minutes. The domain is heavily regulated, changes frequently (new instruments, new fee schedules), and the code is accessible to the operator.

**Specific invariants:**
- debits == credits (double-entry ledger conservation) -- must hold per transaction atomically
- Sum of all account balances == total issued supply at any snapshot
- FX conversion round-trip: floor(X * rate_A_to_B * rate_B_to_A) should equal X within tolerance
- Fee calculation: fee = floor(amount * basis_points / 10000) never exceeds amount, never rounds to zero on nonzero amounts
- Interest accrual: no day-count convention arithmetic overflows 64-bit accumulator over instrument lifetime
- Settlement netting: net position per counterparty stays within signed int64 range given max trade sizes
- Tax/withholding: rounding to 2 decimal places must not accumulate systematic bias across N transactions (cf. Vancouver exchange)
- Payroll: total pay = sum of component calculations; rounding at each step must not diverge from rounding at final step

**Real bug evidence and dollar cost:**
- Vancouver Stock Exchange, 1982-1983: Systematic truncation (floor not round) on every index update. 3,000 updates/day x 22 months = index fell from 1000 to 524 when true value was 1009. Effectively made the entire exchange's benchmark untrustworthy for nearly 2 years.
- Knight Capital, 2012: $440M lost in 45 minutes. The arithmetic of trade execution was never formally bounded.
- DOL enforcement, payroll rounding: Federal contractor forced to pay $600K in back wages/damages for overtime calculations that consistently rounded against employees (419 workers affected).
- TurboTax/state tax rounding: documented cases of rounding at intermediate steps rather than final step causing penalty triggers (Minnesota M15 form).
- Hedge fund: floating point rounding error in trading algorithm lurking for years -- firm acquired at fraction of value.

**Code sourcing:**
- Open-source clearing/settlement reference implementations
- Customer-supplied C/Java/Python arithmetic kernels (fee engines, interest calculators)
- Regulatory filings often include formula specifications encodable as SMT assertions
- FpML (Financial Products Markup Language) -- machine-readable instrument specifications

**Lifting tool:**
- Z3/CVC5 directly on Python/Java arithmetic expressions (no RTL step needed)
- CBMC for C/C++ settlement engine code

**Incumbents / moat assessment:**
- No incumbent owns formal arithmetic verification for traditional finance -- this market is essentially unserved
- Certora/Halmos/Echidna are crypto-native; none target traditional finance
- Big4 audit firms do not use formal methods -- they use spreadsheet samples and statistical sampling
- Regulatory tailwind: SEC/CFTC increasingly requiring software audits of financial algorithms; Basel IV model validation rules
- The "no incumbent" finding is the key signal here

**Fit verdict: STRONG**
The arithmetic is exactly bounded fixed-point (basis points, integer cents/pence). Bugs are documented with real dollar costs. No formal verification tool market exists for this domain. Changes frequently (renewable supply). Self-verifying counterexamples are immediately useful as audit evidence. The closest to a near-term second market outside crypto.

---

### 3. Hardware / RTL Arithmetic Verification (STRONG -- but incumbents own tier-1)

**Why it is strong technically but strategically constrained:**
This is the original home of SAT-based formal verification. The Pentium FDIV bug (1994, $475M recall -- equivalent to $891M in 2024 dollars) catalyzed an entire industry. Every major chip company (Intel, AMD, ARM, Qualcomm) now runs formal verification of arithmetic units internally. The tooling is mature: Intel's Forte, Synopsys Formality, Cadence JasperGold. SAT-based bounded model checking for RTL arithmetic is the textbook use case.

**Specific invariants:**
- IEEE 754 compliance: division, sqrt, FMA results within 0.5 ULP for all input bit patterns
- ALU result identity: A + B == B + A, (A + B) + C == A + (B + C) for all bit combinations
- No carry chain overflow in integer adders given max operand width
- Multiplier output width: W_a + W_b bits sufficient for W_a-bit x W_b-bit product
- Round-to-nearest-even: correct tie-breaking for all mantissa patterns

**Real bug evidence and dollar cost:**
- Pentium FDIV, 1994: 16 missing entries in PLA division table (scripting error). Recall cost: $475M ($891M in 2024 dollars). First-ever CPU recall.
- Post-FDIV, Intel hired "practically every researcher from around the world who worked in Model Checking or Automated Theorem Proving" -- the field's founding moment.
- Automated multiplier verification remains an active research problem: pure SAT scales poorly on large multipliers; recent work combines SAT + computer algebra.

**Code sourcing:**
- RISC-V formal framework (YosysHQ) -- open-source, actively maintained
- OpenSPARC, OpenRISC, CVA6 (open-source cores) -- real Verilog to run SAT against
- Customer RTL (RISC-V chip startups -- 70% of chip cost is verification per industry estimates)

**Lifting tool:**
- Yosys -> AIGER -> SAT (open-source pipeline)
- SymbiYosys (formal verification for open-source RTL)
- CBMC CREST extension for C-to-RTL verification

**Incumbents / moat assessment:**
- Synopsys (31% EDA market share), Cadence (30%), Siemens EDA (13%) own this market
- EDA market: $17.2B in 2024, growing at 10.5% CAGR
- Intel, AMD, Apple run internal teams; they do not outsource to crowdsourced compute
- RISC-V gap is real: smaller chip startups cannot afford Synopsys JasperGold; open-source tools exist but require expertise
- Crowdsourced angle is weakest here -- the bottleneck is specification writing + result interpretation, not raw SAT compute

**Fit verdict: STRONG technically, but OWNED by incumbents for tier-1 chips**
The primitive fits perfectly. The market is real and large. The opening is RISC-V startups and academic/open-source cores -- a real but smaller niche. Not a credible near-term second market unless Cathedral approaches it as a tool for the RISC-V long tail.

---

## FULL DOMAIN RANKINGS (4-11)

### 4. Payroll / Accounting / ERP Arithmetic (PLAUSIBLE)

**Invariants:**
- Total gross pay = sum(hours x rate) for all employees; per-employee rounding must not diverge from aggregate
- Debits == credits per journal entry (double-entry conservation)
- Tax withholding: round(income x rate / 100) never exceeds income
- Allocation/proration: sum of allocated amounts == total to allocate (rounding remainder must be assigned, not lost)
- Currency conversion: consistent rounding per ISO 4217 decimal-place rules

**Real bugs:**
- DOL payroll rounding: $600K settlement (419 workers, systematic floor-rounding of overtime)
- TurboTax Minnesota M15 rounding: incorrect intermediate rounding caused false underpayment penalty triggers

**Code sourcing:** Open-source accounting packages (GnuCash, ERPNext), open-source tax calculation libraries, customer engines

**Fit verdict: PLAUSIBLE** -- regulatory pull exists but enforcement is reactive; sales cycle longer than finance/safety

---

### 5. DSP / Fixed-Point Signal Processing Kernels (PLAUSIBLE)

**Invariants:**
- Accumulator width sufficient for worst-case multiply-accumulate chain (FIR filter: sum(abs(coefficients)) x max_input must not exceed accumulator range)
- Saturation applied consistently across all code paths
- Q-format alignment preserved through multiply before downshift
- No overflow in intermediate results for all valid input combinations

**Real bugs:**
- MAME emulator (2024): explicit fix for incorrect fixed-point clip and saturation arithmetic in SHARC DSP emulation
- In safety-critical motor drives and flight control, DSP overflow can command wrong actuator positions

**Fit verdict: PLAUSIBLE** -- fits better as add-on to embedded safety vertical than standalone market

---

### 6. Game Economies (PLAUSIBLE -- weak code-access)

**Invariants:**
- Gold supply conservation: total gold in all accounts == total ever minted
- Item stack overflow: stack size x item count must not overflow int32/int64
- Auction: listed_amount + fee == total_deducted; buyer_paid == listed_amount

**Real bugs:**
- Diablo III (2013): 32-bit signed integer overflow in gold auction listing. 4.3B gold (exactly 2^32) created per exploit. Real Money Auction House used; Blizzard issued refunds.
- New World (Amazon, 2021): multiple duplication glitches forced economy shutdowns multiple times. HTML injection in chat triggered item duplication.
- EverQuest II: dupers reportedly extracted $70,000+ USD from Station Exchange.

**Fit verdict: PLAUSIBLE as demo; not a reliable commercial vertical** -- server-side code is proprietary; no compliance mandate

---

### 7. ML Quantization / Fixed-Point Neural Network Arithmetic (PLAUSIBLE -- emerging)

**Invariants:**
- Accumulator overflow freedom: sum(abs(weights_i x activations_i)) fits in N-bit accumulator for all neurons
- Quantization round-trip: dequantize(quantize(x)) ~= x within stated error bound, for all x in range
- Scale factor multiplication: scale x (int_val - zero_point) must not overflow float32/int32 intermediate

**Real bugs:**
- Academic measurement: overflows occur in ~11% of neurons in binary-weight networks with 8-bit accumulators
- Using 8-bit instead of 32-bit accumulator causes accuracy drops >40% in highly quantized networks
- Edge AI deployments on ARM Cortex-M and RISC-V microcontrollers: overflow is a real deployed correctness failure

**Fit verdict: PLAUSIBLE (early)** -- no commercial market yet; watch for FDA (medical AI) / ISO 26262 (automotive ML) regulatory trigger

---

### 8. Insurance / Actuarial Premium Arithmetic (STRETCH)

**Why it is a stretch:** Actuarial models involve stochastic simulation (mortality rates, claim distributions) which is explicitly outside our primitive's scope. The deterministic arithmetic kernel (premium formula evaluation) does fit, but it is a small fraction of what insurers care about. No documented public incidents with specific dollar costs found for arithmetic kernel bugs specifically. Regulatory review (state insurance filings) is statistical, not formal.

**Fit verdict: STRETCH**

---

### 9. Database / Spreadsheet Numeric Aggregates (STRETCH)

**Why it is a stretch:** Database engines already catch DECIMAL overflow at runtime (they throw exceptions). The opportunity is pre-deployment static verification, but database schemas change constantly. The cost of a failed query is low compared to finance/safety-critical domains. No clear incentive to formally pre-verify.

**Real bugs documented:** DB2 SQLCODE -802 on 50B-value decimal multiplication; Apache Calcite NUMERIC overflow (CALCITE-3866 JIRA).

**Fit verdict: STRETCH**

---

### 10. Cryptographic Protocol Integer Arithmetic (STRONG -- niche)

**What it is:** Post-quantum cryptography (ML-KEM, ML-DSA per FIPS 203/204) uses modular integer arithmetic. NTTs involve fixed-size polynomial ring arithmetic where overflow or modular reduction bugs break security proofs.

**Invariants:**
- NTT butterfly: (a + b) mod q never produces negative result for unsigned implementation
- Modular reduction: result always in [0, q-1]
- Polynomial coefficient values stay in bounds through Barrett reduction

**Evidence:** Apple uses SAW (Software Analysis Workbench) + SAT/SMT to formally verify corecrypto ML-KEM and ML-DSA implementations against FIPS standards. This is precisely our primitive.

**Gap:** Apple, Google, Intel do this in-house. The open-source/startup space (libpqcrystals, OQS) is narrow.

**Fit verdict: STRONG (niche)**

---

### 11. Zero-Knowledge Proof Arithmetic Circuits (STRONG -- crypto-adjacent)

**What it is:** ZK circuits are arithmetic over finite fields. Circuit constraints being satisfiable for valid witnesses and not for invalid ones is exactly SAT.

**Invariants:**
- Range checks: field element in [0, 2^n - 1]
- Bit decomposition: sum of bits x powers of 2 equals original value
- Non-overflow: intermediate wire values in arithmetic circuits stay in field

**Evidence:** Zcash had a critical bug in its Groth16 circuit that would have allowed unlimited token minting if exploited.

**Fit verdict: STRONG (overlaps crypto -- use as adjacent expansion)**

---

## INCUMBENTS SUMMARY -- HONEST ASSESSMENT

| Domain | Incumbent | Strength | Opening |
|--------|-----------|----------|---------|
| Hardware RTL | Synopsys, Cadence, Siemens (~74% EDA market share) | Very strong, deep foundry certs | RISC-V long tail, open-source cores |
| Safety-critical embedded | AbsInt, TrustInSoft, Frama-C | Strong but human-services model | Self-service counterexample artifact |
| Traditional finance | None with formal methods | No incumbent | Full opening |
| Accounting/payroll | None | No incumbent | Full opening |
| Game economies | None | No incumbent | Code access is barrier |
| ML quantization | Academic only | No commercial player yet | Too early |
| Insurance actuarial | None (wrong scope) | N/A | Wrong fit |

EDA market total: $17.2B in 2024, 10.5% CAGR. Synopsys/Cadence/Siemens collectively ~74% share.

---

## REAL EXPANSION vs. STRETCH CALL

**Real expansion (credible second markets):**
1. Safety-critical embedded/avionics/automotive -- regulatory pull is real, incumbents are expensive and slow, CBMC already works on the code
2. Traditional finance arithmetic -- zero incumbents, documented costly bugs, heavily regulated, code is accessible to operators
3. RISC-V hardware arithmetic (long tail) -- incumbents do not serve this segment, open-source tooling exists, growing market

**Credible near-term second market beyond Bittensor/crypto:**
Traditional finance arithmetic. It has: no formal verification incumbents, documented costly bugs (Vancouver exchange, DOL payroll settlements), regulatory tailwind (SEC/CFTC algorithm audit requirements, Basel IV model validation), code accessibility, and a straightforward mapping of our invariant-checking primitive to the actual formulas financial systems run. The closest analogy to Certora for DeFi but for traditional clearing/settlement/payroll engines.

**Stretch domains (do not lead with these):**
- Insurance actuarial (stochastic, outside primitive scope)
- Database aggregates (runtime catches it anyway)
- Game economies (code access barrier)

---

## SOURCING AND LIFTING SUMMARY

| Domain | Lifting Tool | Code Source | Time to First Bug |
|--------|-------------|-------------|-------------------|
| Safety-critical embedded | CBMC -> SAT | MISRA-C customer code, AUTOSAR | 2-4 weeks |
| Traditional finance | Z3 Python / CBMC | Customer fee/settlement engines | 1-2 weeks |
| RISC-V hardware | SymbiYosys / Yosys | YosysHQ riscv-formal | 1-2 weeks |
| DSP kernels | CBMC | CMSIS-DSP, customer code | 2-4 weeks |
| ML quantization | QVIP / SMT | ONNX, TFLite specs | 4-8 weeks |

---

## SOURCES

- [How Intel makes sure the FDIV bug never happens again](https://www.chiplog.io/p/how-intel-makes-sure-the-fdiv-bug)
- [Pentium FDIV Bug -- Wikipedia](https://en.wikipedia.org/wiki/Pentium_FDIV_bug)
- [Analysis of the 30-Year Pentium FDIV Bug and Intel's $475M Recall](https://www.guru3d.com/story/analysis-of-the-30year-pentium-fdiv-bug-and-intels-million-recall/)
- [Patriot Missile Failure -- University of Minnesota](https://www-users.cse.umn.edu/~arnold/disasters/patriot.html)
- [Truncation Error in the Persian Gulf War -- Stanford Nifty](http://nifty.stanford.edu/2003/pests/2002/lectures/07.1_FloatingPoint/Patriot.html)
- [Ariane-5 Disaster -- Integer Overflow](https://dhanvina.medium.com/ariane-5-disaster-integer-overflow-space-requirements-b96f4dda8bdb)
- [How one line of code brought down a 500M euro rocket launch](https://jam.dev/blog/famous-bugs-rocket-launch/)
- [Disasters due to rounding error -- UT Austin](https://web.ma.utexas.edu/users/arbogast/misc/disasters.html)
- [Vancouver Stock Exchange Rounding Error](https://www.thiscodeworks.com/the-vancouver-stock-exchange-s-rounding-error-that-dropped-the-market-value-by-half-historicalcode-numbers/5e1b5a8b8459eb00141e89f0)
- [Knight Capital $440M Software Error -- Henrico Dolfing](https://www.henricodolfing.ch/en/case-study-4-the-440-million-software-error-at-knight-capital/)
- [When Time Rounding Backfires: A $600K Payroll Mistake](https://www.resourcefulfinancepro.com/news/time-rounding-financial-risk/)
- [Diablo III Economy Broken by Integer Overflow Bug](https://www.gamedeveloper.com/programming/diablo-iii-economy-broken-by-an-integer-overflow-bug)
- [Amazon Games switches off New World economy after duping glitch](https://www.pcgamer.com/amazon-games-switches-off-new-worlds-entire-economy-after-players-discover-duping-glitch/)
- [CBMC: Software Verification from Bug Finding to Proofs -- ETAPS 2025](https://etaps.org/blog/040-totta-2025/)
- [CBMC: Bounded Model Checking for Software](https://www.cprover.org/cbmc/)
- [Formal Verification: Key to Regulatory Compliance ISO 26262 / DO-178C](https://www.trust-in-soft.com/resources/blogs/formal-verification-your-key-to-regulatory-compliance-iso-26262-do-178c)
- [EDA Market Size -- Precedence Research](https://www.precedenceresearch.com/electronic-design-automation-software-market)
- [RISC-V Formal Verification Framework -- YosysHQ](https://github.com/YosysHQ/riscv-formal)
- [Sound Mixed Fixed-Point Quantization of Neural Networks](https://dl.acm.org/doi/abs/10.1145/3609118)
- [WrapNet: Neural Net Inference with Ultra-Low-Resolution Arithmetic](https://arxiv.org/pdf/2007.13242)
- [Apple corecrypto formal verification (SAW + SAT/SMT)](https://github.com/apple/corecrypto/blob/2026-05/corecrypto_verify/technical_overview/formal-verification-for-apple-corecrypto.md)
- [AGORA: Crowdsourced Formal Verification Bug Bounty Protocol -- NDSS 2025](https://www.ndss-symposium.org/wp-content/uploads/2025-poster-44.pdf)
