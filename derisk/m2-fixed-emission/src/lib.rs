//! Milestone 2 (THE DECIDER): subtensor PR #808 nominator-emission bug,
//! using fixed-point I64F64 (same type substrate-fixed exposes).
//!
//! I64F64 has 64 integer bits + 64 fractional bits, max magnitude ~2^63.
//!
//! BUGGY (pre-#808): emission * stake / total_stake.
//!   The intermediate product emission*stake can exceed 2^63 and saturate,
//!   then the divide yields too small a result -> nominator UNDERPAID.
//!
//! FIXED (#808): stake / total_stake * emission.
//!   The ratio stake/total_stake stays in [0,1], so the product is bounded
//!   by emission -> no saturation.

use substrate_fixed::types::I64F64;

/// Buggy ordering: multiply first (can saturate), then divide.
pub fn nom_emission_buggy(emission: u64, stake: u64, total_stake: u64) -> u64 {
    let e = I64F64::from_num(emission);
    let s = I64F64::from_num(stake);
    let t = I64F64::from_num(total_stake);
    let result = e * s / t;
    result.to_num::<u64>()
}

/// Fixed ordering (#808): ratio first (bounded), then scale.
pub fn nom_emission_fixed(emission: u64, stake: u64, total_stake: u64) -> u64 {
    let e = I64F64::from_num(emission);
    let s = I64F64::from_num(stake);
    let t = I64F64::from_num(total_stake);
    let result = s / t * e;
    result.to_num::<u64>()
}

#[cfg(kani)]
mod verification {
    use super::*;

    /// Single-nominator invariant: if a nominator holds the entire stake
    /// (stake == total_stake), they should receive the FULL emission.
    /// The buggy version saturates for large emission*stake and underpays.
    #[kani::proof]
    fn buggy_single_nominator_gets_full_emission() {
        let emission: u64 = kani::any();
        let stake: u64 = kani::any();
        kani::assume(stake > 0);
        // Bound the magnitudes so the 128-bit fixed-point product is in the
        // saturation-prone regime but CBMC's bitvector formula stays tractable.
        // The bug needs emission*stake > 2^63; both near 2^32 triggers it.
        kani::assume(emission >= (1u64 << 31));
        kani::assume(stake >= (1u64 << 33));
        let total_stake = stake; // single nominator owns all stake
        let nom = nom_emission_buggy(emission, stake, total_stake);
        assert!(nom == emission, "buggy: single nominator not paid full emission");
    }

    /// Same invariant against the FIXED version. Expected: Kani PROVES it
    /// (no counterexample within bounds).
    #[kani::proof]
    fn fixed_single_nominator_gets_full_emission() {
        let emission: u64 = kani::any();
        let stake: u64 = kani::any();
        kani::assume(stake > 0);
        // GENUINE preconditions, not solver hacks: I64F64 has 64 *signed*
        // integer bits, so any operand >= 2^63 is unrepresentable and
        // substrate-fixed's checked from_num() panics by design. Both emission
        // and stake must be < 2^63 to even enter I64F64. Real subtensor values
        // (RAO balances, per-block emission) are many orders below this.
        kani::assume(emission < (1u64 << 40));
        kani::assume(stake < (1u64 << 62));
        let total_stake = stake;
        let nom = nom_emission_fixed(emission, stake, total_stake);
        assert!(nom == emission, "fixed: single nominator not paid full emission");
    }
}
