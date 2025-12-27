//! # Keyfile Handling
//!
//! Utilities for loading and parsing Bittensor keyfiles.
//!
//! Bittensor keyfiles can be in several formats:
//! - JSON format with `secretPhrase` or `secretSeed` fields
//! - Plain text mnemonic phrase
//! - Encrypted keyfiles (for coldkeys)

use crate::error::BittensorError;
use serde::{Deserialize, Serialize};
use std::path::Path;
use subxt::ext::sp_core::{sr25519, Pair};
use thiserror::Error;

/// Errors that can occur when loading keyfiles
#[derive(Debug, Error)]
pub enum KeyfileError {
    /// File could not be read
    #[error("Failed to read keyfile: {0}")]
    ReadError(#[from] std::io::Error),

    /// JSON parsing failed
    #[error("Failed to parse keyfile JSON: {0}")]
    ParseError(String),

    /// Decryption failed
    #[error("Decryption failed: {0}")]
    DecryptionError(String),

    /// Invalid key format
    #[error("Invalid key format: {0}")]
    InvalidFormat(String),
}

impl From<KeyfileError> for BittensorError {
    fn from(err: KeyfileError) -> Self {
        BittensorError::WalletError {
            message: err.to_string(),
        }
    }
}

/// Parsed keyfile data
#[derive(Debug, Clone)]
pub struct KeyfileData {
    /// The secret seed or mnemonic phrase
    pub secret: String,
    /// Whether this is a mnemonic phrase
    pub is_mnemonic: bool,
    /// Original format of the keyfile
    pub format: KeyfileFormat,
}

/// Format of the keyfile
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KeyfileFormat {
    /// JSON with secretPhrase field
    JsonSecretPhrase,
    /// JSON with secretSeed field (hex)
    JsonSecretSeed,
    /// Plain text mnemonic
    PlainMnemonic,
    /// Plain text hex seed
    PlainHexSeed,
    /// Encrypted keyfile
    Encrypted,
}

/// JSON structure for Bittensor keyfiles
#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct JsonKeyfile {
    /// Mnemonic phrase
    secret_phrase: Option<String>,
    /// Hex-encoded seed
    secret_seed: Option<String>,
    /// Public key (SS58)
    public_key: Option<String>,
    /// Account ID
    account_id: Option<String>,
    /// SS58 address
    ss58_address: Option<String>,
}

impl KeyfileData {
    /// Convert the keyfile data to an sr25519 keypair
    pub fn to_keypair(&self) -> Result<sr25519::Pair, BittensorError> {
        if self.is_mnemonic {
            sr25519::Pair::from_string(&self.secret, None).map_err(|e| {
                BittensorError::WalletError {
                    message: format!("Invalid mnemonic phrase: {e:?}"),
                }
            })
        } else {
            // It's a hex seed
            let hex_str = self.secret.strip_prefix("0x").unwrap_or(&self.secret);
            let seed_bytes = hex::decode(hex_str).map_err(|e| BittensorError::WalletError {
                message: format!("Invalid hex seed: {e}"),
            })?;

            if seed_bytes.len() != 32 {
                return Err(BittensorError::WalletError {
                    message: format!("Seed must be 32 bytes, got {} bytes", seed_bytes.len()),
                });
            }

            let mut seed_array = [0u8; 32];
            seed_array.copy_from_slice(&seed_bytes);
            Ok(sr25519::Pair::from_seed(&seed_array))
        }
    }
}

/// Load a keyfile from disk
///
/// Supports both JSON and plain text formats.
///
/// # Arguments
///
/// * `path` - Path to the keyfile
///
/// # Returns
///
/// Parsed keyfile data
pub fn load_keyfile(path: &Path) -> Result<KeyfileData, KeyfileError> {
    let content = std::fs::read_to_string(path)?;
    parse_keyfile_content(&content)
}

/// Load an encrypted keyfile
///
/// Currently only supports nacl-encrypted keyfiles as used by the Python SDK.
///
/// # Arguments
///
/// * `path` - Path to the encrypted keyfile
/// * `password` - Password for decryption
///
/// # Returns
///
/// Parsed keyfile data after decryption
pub fn load_encrypted_keyfile(path: &Path, password: &str) -> Result<KeyfileData, KeyfileError> {
    let content = std::fs::read(path)?;
    decrypt_keyfile(&content, password)
}

/// Parse keyfile content
fn parse_keyfile_content(content: &str) -> Result<KeyfileData, KeyfileError> {
    let trimmed = content.trim();

    // Try to parse as JSON first
    if let Ok(json_keyfile) = serde_json::from_str::<JsonKeyfile>(trimmed) {
        // Check for secretPhrase (mnemonic)
        if let Some(phrase) = json_keyfile.secret_phrase {
            return Ok(KeyfileData {
                secret: phrase,
                is_mnemonic: true,
                format: KeyfileFormat::JsonSecretPhrase,
            });
        }

        // Check for secretSeed (hex)
        if let Some(seed) = json_keyfile.secret_seed {
            return Ok(KeyfileData {
                secret: seed,
                is_mnemonic: false,
                format: KeyfileFormat::JsonSecretSeed,
            });
        }

        return Err(KeyfileError::InvalidFormat(
            "JSON keyfile missing secretPhrase or secretSeed".to_string(),
        ));
    }

    // Not JSON - try to parse as plain text
    // Check if it's a hex seed (starts with 0x and is 66 chars)
    if trimmed.starts_with("0x") && trimmed.len() == 66 {
        return Ok(KeyfileData {
            secret: trimmed.to_string(),
            is_mnemonic: false,
            format: KeyfileFormat::PlainHexSeed,
        });
    }

    // Check if it looks like a hex seed without 0x prefix
    if trimmed.len() == 64 && trimmed.chars().all(|c| c.is_ascii_hexdigit()) {
        return Ok(KeyfileData {
            secret: format!("0x{}", trimmed),
            is_mnemonic: false,
            format: KeyfileFormat::PlainHexSeed,
        });
    }

    // Assume it's a mnemonic phrase
    let word_count = trimmed.split_whitespace().count();
    if word_count == 12 || word_count == 24 {
        return Ok(KeyfileData {
            secret: trimmed.to_string(),
            is_mnemonic: true,
            format: KeyfileFormat::PlainMnemonic,
        });
    }

    // Still treat as mnemonic but warn
    Ok(KeyfileData {
        secret: trimmed.to_string(),
        is_mnemonic: true,
        format: KeyfileFormat::PlainMnemonic,
    })
}

/// Decrypt an encrypted keyfile
///
/// Bittensor uses NaCl secretbox for encryption with:
/// - Salt: first 16 bytes
/// - Nonce: next 24 bytes
/// - Ciphertext: remaining bytes
fn decrypt_keyfile(data: &[u8], _password: &str) -> Result<KeyfileData, KeyfileError> {
    // Check minimum length: 16 (salt) + 24 (nonce) + 16 (auth tag) + 1 (min data)
    if data.len() < 57 {
        return Err(KeyfileError::DecryptionError(
            "Encrypted keyfile too short".to_string(),
        ));
    }

    // For now, we return an error indicating encrypted keyfiles need the Python SDK
    // TODO: Implement nacl decryption or use sodiumoxide crate
    Err(KeyfileError::DecryptionError(
        "Encrypted coldkey decryption not yet implemented. \
         Please use `btcli wallet regen_coldkey` to create an unencrypted coldkey, \
         or decrypt using the Python bittensor SDK."
            .to_string(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_json_mnemonic() {
        let content = r#"{"secretPhrase": "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"}"#;
        let result = parse_keyfile_content(content).unwrap();
        assert!(result.is_mnemonic);
        assert_eq!(result.format, KeyfileFormat::JsonSecretPhrase);
        assert!(result.secret.contains("abandon"));
    }

    #[test]
    fn test_parse_json_seed() {
        let content = r#"{"secretSeed": "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}"#;
        let result = parse_keyfile_content(content).unwrap();
        assert!(!result.is_mnemonic);
        assert_eq!(result.format, KeyfileFormat::JsonSecretSeed);
    }

    #[test]
    fn test_parse_plain_mnemonic() {
        let content = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
        let result = parse_keyfile_content(content).unwrap();
        assert!(result.is_mnemonic);
        assert_eq!(result.format, KeyfileFormat::PlainMnemonic);
    }

    #[test]
    fn test_parse_plain_hex_with_prefix() {
        let content = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        let result = parse_keyfile_content(content).unwrap();
        assert!(!result.is_mnemonic);
        assert_eq!(result.format, KeyfileFormat::PlainHexSeed);
    }

    #[test]
    fn test_parse_plain_hex_without_prefix() {
        let content = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        let result = parse_keyfile_content(content).unwrap();
        assert!(!result.is_mnemonic);
        assert_eq!(result.format, KeyfileFormat::PlainHexSeed);
        assert!(result.secret.starts_with("0x"));
    }

    #[test]
    fn test_to_keypair_from_mnemonic() {
        let data = KeyfileData {
            secret: "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about".to_string(),
            is_mnemonic: true,
            format: KeyfileFormat::PlainMnemonic,
        };
        let result = data.to_keypair();
        assert!(result.is_ok());
    }

    #[test]
    fn test_to_keypair_from_seed() {
        let data = KeyfileData {
            secret: "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                .to_string(),
            is_mnemonic: false,
            format: KeyfileFormat::PlainHexSeed,
        };
        let result = data.to_keypair();
        assert!(result.is_ok());
    }

    #[test]
    fn test_to_keypair_invalid_mnemonic() {
        let data = KeyfileData {
            secret: "invalid mnemonic phrase".to_string(),
            is_mnemonic: true,
            format: KeyfileFormat::PlainMnemonic,
        };
        let result = data.to_keypair();
        assert!(result.is_err());
    }

    #[test]
    fn test_to_keypair_invalid_hex() {
        let data = KeyfileData {
            secret: "0xNOTHEX".to_string(),
            is_mnemonic: false,
            format: KeyfileFormat::PlainHexSeed,
        };
        let result = data.to_keypair();
        assert!(result.is_err());
    }

    #[test]
    fn test_to_keypair_wrong_seed_length() {
        let data = KeyfileData {
            secret: "0x0123456789abcdef".to_string(), // 16 bytes instead of 32
            is_mnemonic: false,
            format: KeyfileFormat::PlainHexSeed,
        };
        let result = data.to_keypair();
        assert!(result.is_err());
        if let Err(BittensorError::WalletError { message }) = result {
            assert!(message.contains("32 bytes"));
        }
    }

    #[test]
    fn test_json_missing_secret() {
        let content = r#"{"publicKey": "something"}"#;
        let result = parse_keyfile_content(content);
        assert!(result.is_err());
    }

    #[test]
    fn test_decrypt_too_short() {
        let data = vec![0u8; 10];
        let result = decrypt_keyfile(&data, "password");
        assert!(result.is_err());
    }

    #[test]
    fn test_parse_24_word_mnemonic() {
        let content = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art";
        let result = parse_keyfile_content(content).unwrap();
        assert!(result.is_mnemonic);
        assert_eq!(result.format, KeyfileFormat::PlainMnemonic);
    }

    #[test]
    fn test_keyfile_error_display() {
        let err = KeyfileError::ParseError("test".to_string());
        assert!(err.to_string().contains("parse"));

        let err = KeyfileError::DecryptionError("failed".to_string());
        assert!(err.to_string().contains("failed"));

        let err = KeyfileError::InvalidFormat("bad".to_string());
        assert!(err.to_string().contains("bad"));
    }

    #[test]
    fn test_keyfile_error_to_bittensor_error() {
        let err: BittensorError = KeyfileError::ParseError("test".to_string()).into();
        if let BittensorError::WalletError { message } = err {
            assert!(message.contains("parse"));
        } else {
            panic!("Expected WalletError");
        }
    }

    #[test]
    fn test_keyfile_data_clone() {
        let data = KeyfileData {
            secret: "test".to_string(),
            is_mnemonic: true,
            format: KeyfileFormat::PlainMnemonic,
        };
        let cloned = data.clone();
        assert_eq!(data.secret, cloned.secret);
        assert_eq!(data.is_mnemonic, cloned.is_mnemonic);
    }

    #[test]
    fn test_keyfile_format_equality() {
        assert_eq!(KeyfileFormat::PlainMnemonic, KeyfileFormat::PlainMnemonic);
        assert_ne!(KeyfileFormat::PlainMnemonic, KeyfileFormat::PlainHexSeed);
    }

    #[test]
    fn test_parse_whitespace_content() {
        let content = "  \n  abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about  \n  ";
        let result = parse_keyfile_content(content).unwrap();
        assert!(result.is_mnemonic);
    }
}
