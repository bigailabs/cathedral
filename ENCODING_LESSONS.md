# What our own found-and-fixed bugs teach us about the encoding

**Date:** 2026-06-07
**Source data:** the 6 historical subtensor bugs we backtested (backtest/REPORT.md) +
the 1 novel bug we found (childkey, forward-hunt/CANDIDATES.md) + the 1 reportable
new find in less-audited code (Bifrost vToken round-to-zero).

## The key realization

We were hunting with **generic invariants applied blindly to whole functions**. That is
LOW yield on audited code, because the generic invariants (conservation, bound,
monotonicity, round-trip) are *the first things every auditor checks*. 28 protocols,
293 checks, 1 novel bug — because most of that surface was already swept for exactly
these properties.

But the bugs we DO catch are not random. They all carry a concrete **code-level
signature**: a specific dangerous primitive operation combined with multi-quantity
accounting. We should hunt the **signature**, not the function.

## The signature taxonomy (every bug we've caught maps to one)

| # | Signature (grep-able) | Bugs that match | Specific invariant to assert |
|---|---|---|---|
| S1 | **Saturating arithmetic masking an accounting break** — `saturating_sub/_mul/_add` whose clamp-to-0 (or to MAX) is then used as if exact | childkey, PR#808 | the clamped result must still preserve the ledger identity; `total distributed ≤ total minted` |
| S2 | **Multiple takes/deductions computed off the SAME base** — two+ `rate.mul(base)` where Σrates can exceed 100% | childkey | `Σ takes ≤ base`; child/validator credit ≤ what was actually deducted |
| S3 | **Truncating division / round-to-zero** — floor `x/y` whose result can hit 0, or unit conversion `*1e9 // 1e9` | EVM↔sub conversion, Bifrost mint | round-trip identity; non-zero output when input > 0 (no silent zero-credit) |
| S4 | **Order-of-ops in bounded fixed-point** — `a * b / c` in I64F64/U96F32 where `a*b` can exceed the type max | PR#808 | safe-order (`a/c*b`) == buggy-order; intermediate ≤ type max |
| S5 | **Missing cap / missing normalization / no-op** — a computed reward not bounded by a budget; weights not row-normalized; a decrement that's a TODO | PR#1918, PR#415, #2274 | output ≤ budget; equal input ⇒ equal influence; double-entry delta equality |
| S6 | **Scaling factor applied to the wrong quantity** — an emission/halving factor multiplied into something it shouldn't scale | #2291 | monotonicity over time/input |

## The single most productive vein

**Reward/emission/stake DISTRIBUTION math with multiple percentage takes** (S1+S2 together).
That is exactly where childkey lived, and the Bifrost round-to-zero (S3) is the same
family. Subtensor's coinbase has MANY such split points — validator take, nominator
split, parent/child dividends, burn, root vs subnet weighting. We found ONE. We never
systematically enumerated the rest.

## The encoding shift ("try again" plan)

1. **Signature-directed, not function-blind.** Syntactically grep the target for S1–S6
   primitives FIRST, then encode the *specific* invariant at each flagged site. Skip the
   90% of code where generic invariants are vacuously clean.
2. **Enumerate every split/take/deduction site** in subtensor coinbase + staking pallets
   and run the S1/S2 conservation check on each (the childkey template: Σtakes ≤ base,
   total distributed ≤ minted, with the actual saturating arithmetic transcribed).
3. **Focus the beachhead.** Yield is in under-audited bespoke economic code (subtensor +
   subnet pallets), not Uniswap forks. Point the sharpened tool there.

## Why this should work better

The childkey bug was invisible to "assert conservation on the function" until you
transcribe the *actual saturating_sub chain* and ask "can Σtakes exceed base?". The
signature tells you (a) where to look and (b) which invariant has teeth there. We are
no longer asking a vague question of every function; we are asking the exact question
that the dangerous primitive can answer "yes" to.
