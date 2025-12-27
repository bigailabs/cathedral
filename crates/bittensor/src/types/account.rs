//! # Account ID Utilities
//!
//! Utilities for working with Substrate AccountId types in the Bittensor context.

use crate::error::BittensorError;
use crate::types::Hotkey;
use crate::AccountId;
use std::str::FromStr;

/// Convert a Hotkey to an AccountId
///
/// # Example
///
/// ```
/// use bittensor::types::{Hotkey, hotkey_to_account_id};
///
/// let hotkey = Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
/// let account_id = hotkey_to_account_id(&hotkey);
/// assert!(account_id.is_ok());
/// ```
pub fn hotkey_to_account_id(hotkey: &Hotkey) -> Result<AccountId, BittensorError> {
    AccountId::from_str(hotkey.as_str()).map_err(|_| BittensorError::InvalidHotkey {
        hotkey: hotkey.as_str().to_string(),
    })
}

/// Convert an AccountId to a Hotkey
///
/// # Example
///
/// ```
/// use bittensor::types::{Hotkey, hotkey_to_account_id, account_id_to_hotkey};
///
/// let hotkey = Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
/// let account_id = hotkey_to_account_id(&hotkey).unwrap();
/// let roundtrip = account_id_to_hotkey(&account_id).unwrap();
/// assert_eq!(hotkey, roundtrip);
/// ```
pub fn account_id_to_hotkey(account_id: &AccountId) -> Result<Hotkey, BittensorError> {
    // AccountId's Display implementation produces the SS58 address
    Ok(Hotkey::new_unchecked(account_id.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hotkey_to_account_id() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        let result = hotkey_to_account_id(&hotkey);
        assert!(result.is_ok());
    }

    #[test]
    fn test_account_id_to_hotkey() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        let account_id = hotkey_to_account_id(&hotkey).unwrap();
        let result = account_id_to_hotkey(&account_id).unwrap();
        assert_eq!(hotkey, result);
    }

    #[test]
    fn test_roundtrip() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        let account_id = hotkey_to_account_id(&hotkey).unwrap();
        let roundtrip = account_id_to_hotkey(&account_id).unwrap();
        assert_eq!(hotkey.as_str(), roundtrip.as_str());
    }
}
