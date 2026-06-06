//! Milestone 1: EVM <-> Substrate balance conversion (plain integers).
//!
//! Mirrors the real subtensor pallet-evm conversion: substrate balances are
//! u64 in RAO (1e9 units), EVM balances are u128 in wei (1e18 / 1e9 scaled).
//! The real code divides by 1_000_000_000 going *into* substrate, which is
//! LOSSY: any sub-RAO remainder (e.g. 1 wei) is silently truncated.

pub const GWEI: u128 = 1_000_000_000;

/// EVM (u128, fine-grained) -> Substrate (u64, coarse). Lossy: truncates.
/// Mirrors subtensor's `into_substrate`.
pub fn into_substrate(e: u128) -> u64 {
    (e / GWEI) as u64
}

/// Substrate (u64) -> EVM (u128). Exact scale-up.
pub fn into_evm(x: u64) -> u128 {
    (x as u128) * GWEI
}

#[cfg(kani)]
mod verification {
    use super::*;

    /// Round-trip invariant: converting EVM->Substrate->EVM should be the
    /// identity. It is NOT, because into_substrate truncates. Kani should
    /// produce the minimal counterexample (e = 1 wei, or any non-multiple
    /// of GWEI).
    #[kani::proof]
    fn round_trip_evm() {
        let e: u128 = kani::any();
        // Bound to a realistic balance range to keep the model small.
        kani::assume(e <= 10u128.pow(18));
        let back = into_evm(into_substrate(e));
        assert!(back == e, "round-trip lost value: into_evm(into_substrate(e)) != e");
    }
}
