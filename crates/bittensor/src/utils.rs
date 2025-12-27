//! # Bittensor Utilities
//!
//! Helper functions for common Bittensor operations including:
//! - Weight normalization and payload creation
//! - Cryptographic signature operations
//! - Unit conversions (TAO/RAO)

use crate::error::BittensorError;
use crate::types::Hotkey;
use crate::AccountId;
use std::str::FromStr;
use subxt::ext::sp_core::{sr25519, Pair};

// Weight-related types

/// Represents a normalized weight for a neuron
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NormalizedWeight {
    /// The neuron's UID
    pub uid: u16,
    /// The normalized weight value (0 to u16::MAX)
    pub weight: u16,
}

/// Normalize weights to sum to u16::MAX
///
/// # Arguments
///
/// * `weights` - Vector of (uid, weight) pairs
///
/// # Returns
///
/// Vector of `NormalizedWeight` where weights sum to approximately u16::MAX
///
/// # Example
///
/// ```
/// use bittensor::utils::{normalize_weights, NormalizedWeight};
///
/// let weights = vec![(0, 100), (1, 100)];
/// let normalized = normalize_weights(&weights);
/// assert_eq!(normalized.len(), 2);
/// ```
pub fn normalize_weights(weights: &[(u16, u16)]) -> Vec<NormalizedWeight> {
    if weights.is_empty() {
        return vec![];
    }

    let total: u64 = weights.iter().map(|(_, w)| *w as u64).sum();
    if total == 0 {
        return weights
            .iter()
            .map(|(uid, _)| NormalizedWeight {
                uid: *uid,
                weight: 0,
            })
            .collect();
    }

    let target = u16::MAX as u64;
    weights
        .iter()
        .map(|(uid, weight)| {
            let normalized = ((*weight as u64 * target) / total) as u16;
            NormalizedWeight {
                uid: *uid,
                weight: normalized,
            }
        })
        .collect()
}

/// Create a set_weights payload for submission to the chain
///
/// # Arguments
///
/// * `netuid` - The subnet UID
/// * `weights` - Vector of normalized weights
/// * `version_key` - Version key for the weights
///
/// # Returns
///
/// A payload that can be submitted to the chain
pub fn set_weights_payload(
    netuid: u16,
    weights: Vec<NormalizedWeight>,
    version_key: u64,
) -> impl subxt::tx::Payload {
    use crate::api::api;

    let (dests, values): (Vec<u16>, Vec<u16>) =
        weights.into_iter().map(|w| (w.uid, w.weight)).unzip();

    api::tx()
        .subtensor_module()
        .set_weights(netuid, dests, values, version_key)
}

/// Verify a Bittensor signature
///
/// # Arguments
///
/// * `hotkey` - The hotkey that supposedly signed the data
/// * `signature_hex` - Hex-encoded signature
/// * `data` - The data that was signed
///
/// # Returns
///
/// * `Ok(())` if the signature is valid
/// * `Err(BittensorError)` if verification fails
///
/// # Example
///
/// ```rust,no_run
/// use bittensor::types::Hotkey;
/// use bittensor::utils::verify_bittensor_signature;
///
/// let hotkey = Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
/// let result = verify_bittensor_signature(&hotkey, "abcd...", b"message");
/// ```
pub fn verify_bittensor_signature(
    hotkey: &Hotkey,
    signature_hex: &str,
    data: &[u8],
) -> Result<(), BittensorError> {
    if signature_hex.is_empty() {
        return Err(BittensorError::AuthError {
            message: "Empty signature".to_string(),
        });
    }

    if data.is_empty() {
        return Err(BittensorError::AuthError {
            message: "Empty data".to_string(),
        });
    }

    let signature_bytes = hex::decode(signature_hex).map_err(|e| BittensorError::AuthError {
        message: format!("Invalid hex signature format: {e}"),
    })?;

    let account_id =
        AccountId::from_str(hotkey.as_str()).map_err(|_| BittensorError::InvalidHotkey {
            hotkey: hotkey.as_str().to_string(),
        })?;

    if signature_bytes.len() != 64 {
        return Err(BittensorError::AuthError {
            message: format!(
                "Invalid signature length: expected 64 bytes, got {}",
                signature_bytes.len()
            ),
        });
    }

    let mut signature_array = [0u8; 64];
    signature_array.copy_from_slice(&signature_bytes);

    let signature = sr25519::Signature::from_raw(signature_array);

    use subxt::ext::sp_runtime::traits::Verify;

    let public_key = sr25519::Public::from_raw(account_id.0);
    let is_valid = signature.verify(data, &public_key);

    if is_valid {
        Ok(())
    } else {
        Err(BittensorError::AuthError {
            message: "Signature verification failed".to_string(),
        })
    }
}

/// Signature type used by Bittensor (sr25519)
pub type BittensorSignature = sr25519::Signature;

/// Sign a message with a keypair
///
/// # Arguments
///
/// * `keypair` - The sr25519 keypair to sign with
/// * `message` - The message bytes to sign
///
/// # Returns
///
/// The signature
pub fn sign_with_keypair(keypair: &sr25519::Pair, message: &[u8]) -> BittensorSignature {
    keypair.sign(message)
}

/// Sign a message and return hex-encoded signature
///
/// # Arguments
///
/// * `keypair` - The sr25519 keypair to sign with
/// * `message` - The message bytes to sign
///
/// # Returns
///
/// Hex-encoded signature string
pub fn sign_message_hex(keypair: &sr25519::Pair, message: &[u8]) -> String {
    let signature = sign_with_keypair(keypair, message);
    hex::encode(signature.0)
}

/// Create a signature using a subxt signer
///
/// # Arguments
///
/// * `signer` - The signer to use
/// * `data` - The data to sign
///
/// # Returns
///
/// Hex-encoded signature string
pub fn create_signature<T>(signer: &T, data: &[u8]) -> String
where
    T: subxt::tx::Signer<subxt::PolkadotConfig>,
{
    let signature = signer.sign(data);

    match signature {
        subxt::utils::MultiSignature::Sr25519(sig) => hex::encode(sig),
        _ => hex::encode([0u8; 64]),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_weights_empty() {
        let result = normalize_weights(&[]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_normalize_weights_zero_weights() {
        let weights = vec![(0, 0), (1, 0)];
        let result = normalize_weights(&weights);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].weight, 0);
        assert_eq!(result[1].weight, 0);
    }

    #[test]
    fn test_normalize_weights_equal() {
        let weights = vec![(0, 100), (1, 100)];
        let result = normalize_weights(&weights);
        assert_eq!(result.len(), 2);
        // Each should be approximately half of u16::MAX
        assert!(result[0].weight > 30000);
        assert!(result[1].weight > 30000);
    }

    #[test]
    fn test_normalize_weights_unequal() {
        let weights = vec![(0, 75), (1, 25)];
        let result = normalize_weights(&weights);
        assert_eq!(result.len(), 2);
        // First should be ~3x the second
        assert!(result[0].weight > result[1].weight * 2);
    }

    #[test]
    fn test_signature_verification_empty_signature() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        let result = verify_bittensor_signature(&hotkey, "", b"data");
        assert!(result.is_err());
    }

    #[test]
    fn test_signature_verification_empty_data() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        let result = verify_bittensor_signature(&hotkey, &"ab".repeat(64), b"");
        assert!(result.is_err());
    }

    #[test]
    fn test_signature_verification_invalid_hex() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        let result = verify_bittensor_signature(&hotkey, "not_hex!", b"data");
        assert!(result.is_err());
    }

    #[test]
    fn test_signature_verification_wrong_length() {
        let hotkey =
            Hotkey::new("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string()).unwrap();
        let result = verify_bittensor_signature(&hotkey, "abcd", b"data");
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("length"));
    }

    #[test]
    fn test_sign_and_verify() {
        use subxt::ext::sp_core::Pair;

        // Generate a keypair
        let (pair, _) = sr25519::Pair::generate();
        let message = b"test message";

        // Sign the message
        let signature = sign_with_keypair(&pair, message);

        // Verify using sp_runtime
        use subxt::ext::sp_runtime::traits::Verify;
        let public = pair.public();
        assert!(signature.verify(message.as_slice(), &public));
    }

    #[test]
    fn test_sign_message_hex() {
        use subxt::ext::sp_core::Pair;

        let (pair, _) = sr25519::Pair::generate();
        let message = b"test message";

        let hex_sig = sign_message_hex(&pair, message);

        // Hex signature should be 128 characters (64 bytes * 2)
        assert_eq!(hex_sig.len(), 128);

        // Should be valid hex
        assert!(hex::decode(&hex_sig).is_ok());
    }
}
