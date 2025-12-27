//! # Hotkey Type
//!
//! Bittensor hotkey identifier in SS58 format with validation.

use serde::{Deserialize, Serialize};
use sp_core::crypto::Ss58Codec;
use std::fmt;
use std::str::FromStr;

use crate::AccountId;

/// Bittensor hotkey identifier in SS58 format
///
/// A hotkey is a public key identifier used for signing transactions and
/// authenticating on the Bittensor network. It must be a valid SS58 address.
///
/// # Example
///
/// ```
/// use bittensor::types::Hotkey;
///
/// let hotkey = Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string());
/// assert!(hotkey.is_ok());
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Hotkey(String);

impl Hotkey {
    /// Create a new Hotkey from an SS58-formatted string
    ///
    /// # Arguments
    ///
    /// * `hotkey` - SS58 formatted address string
    ///
    /// # Returns
    ///
    /// * `Ok(Hotkey)` if the string is a valid SS58 address
    /// * `Err(String)` with a description if validation fails
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Hotkey;
    ///
    /// // Valid SS58 address
    /// let valid = Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string());
    /// assert!(valid.is_ok());
    ///
    /// // Invalid address
    /// let invalid = Hotkey::new("invalid".to_string());
    /// assert!(invalid.is_err());
    /// ```
    pub fn new(hotkey: String) -> Result<Self, String> {
        if hotkey.is_empty() {
            return Err("Hotkey cannot be empty".to_string());
        }

        // Length check for SS58 addresses (typical range is 47-48 characters)
        if hotkey.len() < 47 || hotkey.len() > 48 {
            return Err(format!(
                "Invalid hotkey length: expected 47-48 characters, got {}",
                hotkey.len()
            ));
        }

        // Validate SS58 format using sp-core
        match sp_core::sr25519::Public::from_ss58check(&hotkey) {
            Ok(_) => Ok(Hotkey(hotkey)),
            Err(_) => {
                // Try with AccountId32 format for broader compatibility
                match sp_core::crypto::AccountId32::from_ss58check(&hotkey) {
                    Ok(_) => Ok(Hotkey(hotkey)),
                    Err(_) => Err(format!(
                        "Invalid SS58 format: checksum validation failed for {hotkey}"
                    )),
                }
            }
        }
    }

    /// Create a Hotkey without validation (for internal use)
    ///
    /// # Safety
    ///
    /// This should only be used when you know the hotkey is valid,
    /// such as when converting from an AccountId.
    pub(crate) fn new_unchecked(hotkey: String) -> Self {
        Hotkey(hotkey)
    }

    /// Get the inner string representation
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Hotkey;
    ///
    /// let hotkey = Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
    /// assert_eq!(hotkey.as_str(), "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY");
    /// ```
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Convert to owned String
    pub fn into_string(self) -> String {
        self.0
    }

    /// Convert Hotkey to AccountId
    ///
    /// # Returns
    ///
    /// * `Ok(AccountId)` if conversion succeeds
    /// * `Err(String)` if conversion fails
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Hotkey;
    ///
    /// let hotkey = Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
    /// let account_id = hotkey.to_account_id();
    /// assert!(account_id.is_ok());
    /// ```
    pub fn to_account_id(&self) -> Result<AccountId, String> {
        AccountId::from_str(&self.0)
            .map_err(|e| format!("Failed to parse hotkey as AccountId: {e}"))
    }

    /// Create Hotkey from AccountId
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Hotkey;
    /// use std::str::FromStr;
    ///
    /// let hotkey = Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
    /// let account_id = hotkey.to_account_id().unwrap();
    /// let roundtrip = Hotkey::from_account_id(&account_id);
    /// assert_eq!(hotkey, roundtrip);
    /// ```
    pub fn from_account_id(account_id: &AccountId) -> Self {
        Hotkey(account_id.to_string())
    }
}

impl fmt::Display for Hotkey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl FromStr for Hotkey {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Self::new(s.to_string())
    }
}

impl AsRef<str> for Hotkey {
    fn as_ref(&self) -> &str {
        &self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Known valid SS58 addresses for testing
    const VALID_ADDRESSES: &[&str] = &[
        "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
        "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy",
        "5HGjWAeFDfFCWPsjFQdVV2Msvz2XtMktvgocEZcCj68kUMaw",
    ];

    #[test]
    fn test_valid_hotkeys() {
        for addr in VALID_ADDRESSES {
            let result = Hotkey::new(addr.to_string());
            assert!(
                result.is_ok(),
                "Failed to create hotkey from valid address: {addr}"
            );
            assert_eq!(result.unwrap().as_str(), *addr);
        }
    }

    #[test]
    fn test_empty_hotkey() {
        let result = Hotkey::new(String::new());
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("cannot be empty"));
    }

    #[test]
    fn test_short_hotkey() {
        let result = Hotkey::new("short".to_string());
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("Invalid hotkey length"));
    }

    #[test]
    fn test_invalid_checksum() {
        let result = Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQZ".to_string());
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("checksum validation failed"));
    }

    #[test]
    fn test_from_str() {
        let hotkey: Result<Hotkey, _> = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".parse();
        assert!(hotkey.is_ok());
    }

    #[test]
    fn test_display() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        assert_eq!(
            format!("{}", hotkey),
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        );
    }

    #[test]
    fn test_account_id_conversion() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        let account_id = hotkey.to_account_id().unwrap();
        let roundtrip = Hotkey::from_account_id(&account_id);
        assert_eq!(hotkey, roundtrip);
    }

    #[test]
    fn test_serialization() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        let json = serde_json::to_string(&hotkey).unwrap();
        let deserialized: Hotkey = serde_json::from_str(&json).unwrap();
        assert_eq!(hotkey, deserialized);
    }

    #[test]
    fn test_as_ref() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        let s: &str = hotkey.as_ref();
        assert_eq!(s, "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY");
    }
}
