# Kani Auto-Lift Derisk — VERDICT

## VERDICT: GREEN (with operational caveats)

Kani reasons about real `substrate-fixed` `I64F64` arithmetic end-to-end. On PR #808
it found a concrete counterexample for the buggy multiply-first ordering and
proved the fixed ratio-first ordering — checking the unmodified substrate-fixed
crate (its hand-rolled 128-bit wide_div/arith included). No hand-modelling.

Caveats: must extract pure fns to a standalone crate; must declare genuine operand
preconditions (< 2^63 to fit I64F64); solve cost ~0.5M vars / 1.45M clauses per
I64F64 mul+div (fixed proof 256s, buggy CEX 4s).

## M1 — plain-int EVM<->Substrate round-trip — PASS
into_substrate(e)=(e/1e9) as u64 ; into_evm(x)=x as u128 * 1e9.
Assert round-trip over any e, assume(e<=1e18). VERIFICATION FAILED (lossy).
Concrete CEX: e=998617697000000001 (sub-GWEI remainder). <0.1s.
  cargo kani --harness round_trip_evm [-Z concrete-playback --concrete-playback=print]

## M2 — DECIDER: fixed-point I64F64 (PR #808) — PASS
Dep: substrate-fixed = "0.5.9" (subtensor's actual crate).
BUGGY:  from_num(emission)*from_num(stake)/from_num(total_stake)
FIXED:  from_num(stake)/from_num(total_stake)*from_num(emission)
Invariant: single nominator (stake==total_stake) gets full emission.

2a. Reasons about I64F64? YES. CBMC lowered from_num, Mul/Div, wide_div::DivHalf,
arith::FallbackHelper, checked to_num/FromFixed to bitvectors. Symex 0.13s,
~8089 GOTO steps. No intrinsic/unbounded-loop wall in substrate-fixed 0.5.9.

2b. BUGGY finds the bug. Bounds emission>=2^31, stake>=2^33 -> VERIFICATION
FAILED 4s, "Failed Checks: overflow" (I64F64 product blows the 2^63 ceiling).
CEX: emission=768614335688736771 stake=8589934592.

2c. FIXED proven. Bounds emission<2^40, stake<2^62 -> VERIFICATION SUCCESSFUL,
1 verified, 0 failures, 256s. NOTE: with stake left full-range the harness
"failed" on substrate-fixed's OWN internal panic (traits.rs:2054, from_num panics
for operand>=2^63), not on our invariant. Constraining stake to representable
range -> clean proof. Key operational lesson: declare the real operand domain.
  cargo kani --harness buggy_single_nominator_gets_full_emission
  cargo kani --harness fixed_single_nominator_gets_full_emission

## Friction inventory
- Install: `cargo install --locked kani-verifier ; cargo kani setup`. ~1min, no pain.
  Pins nightly-2025-11-21 + bundled CBMC 6.8.0 (no conflict with system cbmc 5.12).
- Fixed-point: substrate-fixed 0.5.9 compiles+verifies clean. Modern `fixed 1.31`
  does NOT compile under Kani nightly (unstable const unchecked_shr). Pin 0.5.9.
- Extraction mandatory: cannot `cargo kani` the full node. Paste pure fn into a tiny
  crate depending only on substrate-fixed. Body byte-identical to chain code.
- Library-internal panics: Kani checks substrate-fixed's own debug_assert/overflow
  checks. With overflow-checks=true an unconstrained u64 into from_num trips the
  lib's "doesn't fit" assertion at >=2^63. Must assume operand<2^63 (RAO realistically
  <~2^50). Auto-lift tool must emit type-derived range assumes per operand.
- Solver: no --unwind needed (loop-free arith). Bit-width drives cost: I64F64 mul+div
  ~0.5M vars / 1.45M clauses. buggy CEX 4s, fixed proof 256s. Fully-unbounded buggy +
  concrete-playback did NOT finish in 900s -> bound operands to real domain; get the
  verdict first, replay the CEX separately.

## Path to encode subtensor's money-math surface
1. Extractor: per money-math fn (emission split, stake delta, bonds, EMA, price
   conversion), paste body verbatim into a generated per-fn crate on substrate-fixed 0.5.9.
2. Invariant + precondition templates: generic invariants (conservation, monotonicity,
   100%-stake->100%-payout, no-saturation) as reusable skeletons; auto-emit kani::assume
   range bounds from operand type + I64F64 range.
3. Run loop: `cargo kani` per (fn x invariant); on FAILURE `-Z concrete-playback` for the
   exact bad input + a regression test; on SUCCESS record the proven bound.
4. Cost control: bound operands to real domains (correct + tractable); minutes per
   fixed-point proof; parallelize across fns.
5. Keep z3 hand-models as a differential oracle (disagreement = drift caught).

## Fallback (acceptable, with cost)
If a fn defeats Kani (unbounded storage loops, FFI, host fns): port the arithmetic to
C and use the generator's proven CBMC-on-C path. Works but loses "checks the actual
Rust" (transcription drift, now in two languages). Reserve for genuinely Kani-hostile
fns; do not default to it. Faithful z3 hand-models = same fallback, same cost.

## Artifacts
derisk/m1-evm-convert/, derisk/m2-fixed-emission/ ; Stitch ~/kani-derisk/.
Both crates set overflow-checks=true so Kani treats overflow as a checked property.
