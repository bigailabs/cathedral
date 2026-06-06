# RESUME — message to a future agent (read this first)

You are continuing a working project. This repo (`wallscaler/cathedral-scaffold`, private)
is the **durable brain** — it lives on GitHub and is mirrored on the host **Stitch**.
A different machine ("command-center"/cc) was reset; nothing important lived only there.
`git clone` this repo on any machine + read this file = full context, zero loss.

## What this project is (one line)
A **verifiable finder of money-math bugs in blockchain/protocol arithmetic**: assert a
GENERIC invariant (conservation "value in==out", monotonicity, bound "share<=whole",
round-trip identity, no-overflow) over real on-chain arithmetic, and use SAT/SMT to find
an input that violates it. The witness **self-verifies** (anyone re-checks it in
microseconds), so it needs no trust and no sandbox. Beachhead: Bittensor's own subtensor
chain. Delivery: a decentralized "subnet" that publishes instances and pays for verified
counterexamples.

## What is PROVEN (status as of this handoff)
1. **Catches known bugs blind — 6/6.** Generic invariants caught 6 real historical subtensor
   bugs (PR#1918, #2274, #808, #2291, PR#415, EVM↔Substrate conversion) without being told
   where. See `backtest/` (backtest.py + REPORT.md).
2. **Auto-lift works — Kani GREEN.** Kani (Rust bounded model checker) reasons about the REAL
   unmodified `substrate-fixed` I64F64 crate — found #808 as a counterexample, proved the fix.
   Means we can check the chain's ACTUAL Rust, not just transcriptions. See `derisk/` (REPORT.md
   + m1-evm-convert/ + m2-fixed-emission/). Caveats: pin substrate-fixed 0.5.9; emit operand
   range-assumes or you trip library-internal panics; ~minutes/fn solve.
3. **One NEW bug found + verified.** `get_parent_child_dividends_distribution`
   (`run_coinbase.rs:~940-975`): burn_take and child_take are both computed off the same
   pre-deduction parent_emission; the two saturating_subs clamp at 0 but `total_child_take`
   is added unconditionally → when CKBurn + childkey_take > 100%, the child is credited alpha
   that was never deducted = **conservation/inflation bug** (alpha from nothing). Witness:
   V=100,E=80,burn=100%,child=18% → 14 rao/parent/epoch from nothing. **DORMANT**: triggers
   only when CKBurn (sudo-set, default 0, no on-chain cap) > ~82%. Verified against
   opentensor/subtensor@main character-by-character. Receipt: `forward-hunt/CANDIDATES.md`.
   **NOT yet disclosed to OpenTensor — that's Fred's call.**
4. **Score rate (honest):** first full subtensor sweep = 1 verified new bug / 37 invariant-checks
   (~25 functions). 30 clean (invariant provably holds in-band) — valuable negatives. n is small;
   more catches come from BREADTH (the corpus), not from re-scanning subtensor.
5. **Supply / breadth:** `targets/` = 100 auditable protocols catalogued (targets.jsonl +
   CATALOGUE.md), ~$80-100B net value-at-risk, 32 Kani-ready Substrate/Rust, 5 fixed-point libs
   covering ~$20B downstream. ~30k solvable instances/full-pass ≈ 3.6 days of full-fleet feed,
   renewable per release.
6. **Market:** competitors mapped — the exact intersection (exhaustive SMT + decentralized
   crowd-solve + continuous protocol-math monitoring + self-verifying witness) is UNCONTESTED;
   closest are Halmos (technique, no crowd/continuous) and Bitsec SN60 / BitAudit SN32 (crypto
   security subnets, but AI-guess + fuzzy LLM judge, NOT verifiable). Parallel domains
   (`parallel-domains/REPORT.md`): TradFi settlement/ledger is the clearest non-crypto 2nd market
   (no FV incumbent); also safety-critical fixed-point, RISC-V hardware.

## The engine (how it works + how to run it ON STITCH)
- z3 on Stitch: `~/experiments/evm-smt/z3venv/bin/python`. drat-trim: `~/tools/drat-trim/drat-trim`.
  Kani: `cargo kani` (pin substrate-fixed 0.5.9). CBMC on Stitch (the generator uses it).
- The hunt method = `forward-hunt/hunt.py` / `backtest/backtest.py` patterns: transcribe a
  function's arithmetic into z3, assert a generic invariant, SAT = candidate, then TRIAGE HARD.
- **DISCIPLINE (do not break):** for any SAT — (1) re-derive the witness in plain Python;
  (2) match the REAL source CHARACTER-FOR-CHARACTER (assume your model has a typo until proven —
  the #1 false-positive cause); (3) check KNOWN / INTENDED (deliberate rounding) / artifact
  (you missed a guard). A finding is **real-new** only if faithful + reproduces + unknown +
  unintended. Agents log real-new as `status:"PROPOSED"`; a HUMAN verifies against source and
  promotes to `status:"VERIFIED"`. The board's headline counts only VERIFIED. A false claim is
  worse than missing one.
- Findings stream: `hunt-board/findings.jsonl` (one JSON per check; log clean UNSATs too — the
  denominator is the score rate). Schema in dashboard.py.
- Dashboard: `python3 hunt-board/dashboard.py` → http://127.0.0.1:8100 (bugs / funnel / coverage
  / live supply pipeline from targets.jsonl).

## NEXT STEPS (in priority order)
1. **Mine the full corpus.** Work through `targets/targets.jsonl` (100 targets, Tier 1 first):
   pull current source, transcribe core arithmetic, assert generic invariants on z3 (Stitch),
   triage, append to findings.jsonl with FULL PROVENANCE (protocol, file, lines, source URL,
   commit/branch, witness). Prioritize the less-audited Substrate/Rust + long-tail (more yield);
   the heavily-audited (Uniswap/Aave/PRBMath) will mostly be clean = also valuable signal.
2. **Get to ≥3-5 verified new bugs across multiple protocols** — that's the bar to call the
   discovery side "green" (1 is a fluke). Each must be human-verified against source.
3. **Build the Kani auto-lift pipeline** (derisk path is documented in derisk/REPORT.md):
   per-fn extractor → invariant templates → range-assumes → cargo kani. This is the production
   scale-up that checks real Rust, not transcriptions.
4. **Decide disclosure** of the childkey bug to OpenTensor (private write-up first; Fred decides).
5. The eventual product = bug-hunting (value, finite per release) + a racing layer (volume,
   renewable) feeding the subnet's fast miners (~350 distinct solves/hr observed, supply-starved).

## File map
- `backtest/` — known-bug validation (6/6) · `derisk/` — Kani auto-lift proof (GREEN)
- `forward-hunt/` — the new-bug hunt + CANDIDATES.md receipts · `hunt-board/` — live dashboard + findings.jsonl
- `targets/` — the 100-protocol audit corpus (the supply pipeline) · `parallel-domains/` — TAM beyond crypto
- `scaffold/` — the original 3-lane subnet scaffold + RC gate (rc_verify.py) · `RELEASE_STATUS.md` — scaffold status

## Retention note (why this survives a machine reset)
The repo is on GitHub (private) and on Stitch. cc-local state (running agents, the live dashboard
process) does NOT survive a cc reset, but it's all reproducible from this repo. To resume: clone,
read this file, restart the dashboard, and continue from NEXT STEPS — the engine runs on Stitch.
