//! # Bittensor Types
//!
//! Core type definitions for the Bittensor SDK including:
//! - `Hotkey`: SS58-formatted Bittensor hotkey addresses
//! - `Balance`: TAO/RAO balance representation with arithmetic
//! - Identity types: `ValidatorUid`, `MinerUid`

mod account;
mod balance;
mod hotkey;

pub use account::*;
pub use balance::{rao_to_tao, tao_to_rao, Balance, RAO_PER_TAO};
pub use hotkey::Hotkey;

/// Bittensor validator unique identifier (u16)
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub struct ValidatorUid(pub u16);

impl ValidatorUid {
    /// Create a new ValidatorUid
    pub fn new(uid: u16) -> Self {
        Self(uid)
    }

    /// Get the inner u16 value
    pub fn as_u16(&self) -> u16 {
        self.0
    }
}

impl From<u16> for ValidatorUid {
    fn from(uid: u16) -> Self {
        Self(uid)
    }
}

impl From<ValidatorUid> for u16 {
    fn from(uid: ValidatorUid) -> u16 {
        uid.0
    }
}

impl std::fmt::Display for ValidatorUid {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Bittensor miner unique identifier (u16)
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub struct MinerUid(pub u16);

impl MinerUid {
    /// Create a new MinerUid
    pub fn new(uid: u16) -> Self {
        Self(uid)
    }

    /// Get the inner u16 value
    pub fn as_u16(&self) -> u16 {
        self.0
    }
}

impl From<u16> for MinerUid {
    fn from(uid: u16) -> Self {
        Self(uid)
    }
}

impl From<MinerUid> for u16 {
    fn from(uid: MinerUid) -> u16 {
        uid.0
    }
}

impl std::fmt::Display for MinerUid {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validator_uid() {
        let uid = ValidatorUid::new(42);
        assert_eq!(uid.as_u16(), 42);
        assert_eq!(uid.to_string(), "42");

        let uid_from: ValidatorUid = 100u16.into();
        assert_eq!(uid_from.as_u16(), 100);
    }

    #[test]
    fn test_miner_uid() {
        let uid = MinerUid::new(123);
        assert_eq!(uid.as_u16(), 123);
        assert_eq!(uid.to_string(), "123");

        let uid_from: MinerUid = 456u16.into();
        assert_eq!(uid_from.as_u16(), 456);
    }
}
