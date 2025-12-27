//! # Wallet Module
//!
//! Wallet management for Bittensor, including key loading, signing, and
//! transaction creation.
//!
//! # Example
//!
//! ```rust,no_run
//! use bittensor::wallet::Wallet;
//!
//! // Load an existing wallet
//! let wallet = Wallet::load("my_wallet", "my_hotkey")?;
//!
//! // Sign data with the hotkey
//! let signature = wallet.sign(b"message");
//!
//! // Get the hotkey address
//! let hotkey = wallet.hotkey();
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```

mod keyfile;
mod signer;

pub use keyfile::{KeyfileData, KeyfileError};
pub use signer::WalletSigner;

use crate::error::BittensorError;
use crate::types::Hotkey;
use crate::AccountId;
use std::path::{Path, PathBuf};
use subxt::ext::sp_core::{sr25519, Pair};

/// Bittensor wallet for managing keys and signing transactions
///
/// A wallet contains:
/// - A hotkey (required) for signing transactions
/// - An optional coldkey for staking operations
///
/// # Example
///
/// ```rust,no_run
/// use bittensor::wallet::Wallet;
///
/// // Load from default ~/.bittensor/wallets path
/// let wallet = Wallet::load("my_wallet", "my_hotkey")?;
/// println!("Hotkey: {}", wallet.hotkey());
/// # Ok::<(), bittensor::BittensorError>(())
/// ```
#[derive(Clone)]
pub struct Wallet {
    /// Wallet name
    pub name: String,
    /// Hotkey name
    pub hotkey_name: String,
    /// Path to the wallet directory
    pub path: PathBuf,
    /// Hotkey keypair
    hotkey_pair: sr25519::Pair,
    /// Optional coldkey keypair (requires unlock)
    coldkey_pair: Option<sr25519::Pair>,
}

impl std::fmt::Debug for Wallet {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Wallet")
            .field("name", &self.name)
            .field("hotkey_name", &self.hotkey_name)
            .field("path", &self.path)
            .field("hotkey", &self.hotkey().to_string())
            .field("coldkey_unlocked", &self.is_coldkey_unlocked())
            .finish()
    }
}

impl Wallet {
    /// Load a wallet from the default Bittensor wallet path
    ///
    /// Wallets are stored in `~/.bittensor/wallets/<wallet_name>/hotkeys/<hotkey_name>`
    ///
    /// # Arguments
    ///
    /// * `wallet_name` - Name of the wallet directory
    /// * `hotkey_name` - Name of the hotkey file
    ///
    /// # Returns
    ///
    /// * `Ok(Wallet)` if the wallet was loaded successfully
    /// * `Err(BittensorError)` if the wallet could not be loaded
    ///
    /// # Example
    ///
    /// ```rust,no_run
    /// use bittensor::wallet::Wallet;
    ///
    /// let wallet = Wallet::load("default", "default")?;
    /// # Ok::<(), bittensor::BittensorError>(())
    /// ```
    pub fn load(wallet_name: &str, hotkey_name: &str) -> Result<Self, BittensorError> {
        let wallet_path = Self::default_wallet_path()?;
        Self::load_from_path(wallet_name, hotkey_name, &wallet_path)
    }

    /// Load a wallet from a custom path
    ///
    /// # Arguments
    ///
    /// * `wallet_name` - Name of the wallet directory
    /// * `hotkey_name` - Name of the hotkey file
    /// * `base_path` - Base path where wallets are stored
    ///
    /// # Example
    ///
    /// ```rust,no_run
    /// use bittensor::wallet::Wallet;
    /// use std::path::PathBuf;
    ///
    /// let base_path = PathBuf::from("/custom/wallets");
    /// let wallet = Wallet::load_from_path("my_wallet", "my_hotkey", &base_path)?;
    /// # Ok::<(), bittensor::BittensorError>(())
    /// ```
    pub fn load_from_path(
        wallet_name: &str,
        hotkey_name: &str,
        base_path: &Path,
    ) -> Result<Self, BittensorError> {
        let hotkey_path = base_path
            .join(wallet_name)
            .join("hotkeys")
            .join(hotkey_name);

        if !hotkey_path.exists() {
            return Err(BittensorError::WalletError {
                message: format!("Hotkey file not found: {}", hotkey_path.display()),
            });
        }

        let keyfile_data = keyfile::load_keyfile(&hotkey_path)?;
        let hotkey_pair = keyfile_data.to_keypair()?;

        Ok(Self {
            name: wallet_name.to_string(),
            hotkey_name: hotkey_name.to_string(),
            path: base_path.join(wallet_name),
            hotkey_pair,
            coldkey_pair: None,
        })
    }

    /// Create a new wallet with a random seed
    ///
    /// # Arguments
    ///
    /// * `wallet_name` - Name of the wallet
    /// * `hotkey_name` - Name of the hotkey
    ///
    /// # Returns
    ///
    /// A new wallet with a randomly generated keypair (not saved to disk)
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::wallet::Wallet;
    ///
    /// let wallet = Wallet::create_random("test_wallet", "test_hotkey");
    /// assert!(!wallet.hotkey().as_str().is_empty());
    /// ```
    pub fn create_random(wallet_name: &str, hotkey_name: &str) -> Result<Self, BittensorError> {
        let (pair, _) = sr25519::Pair::generate();
        let path = Self::default_wallet_path()?;

        Ok(Self {
            name: wallet_name.to_string(),
            hotkey_name: hotkey_name.to_string(),
            path: path.join(wallet_name),
            hotkey_pair: pair,
            coldkey_pair: None,
        })
    }

    /// Create a wallet from a mnemonic phrase
    ///
    /// # Arguments
    ///
    /// * `wallet_name` - Name of the wallet
    /// * `hotkey_name` - Name of the hotkey
    /// * `mnemonic` - BIP39 mnemonic phrase (12 or 24 words)
    ///
    /// # Example
    ///
    /// ```rust,no_run
    /// use bittensor::wallet::Wallet;
    ///
    /// let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
    /// let wallet = Wallet::from_mnemonic("test", "test", mnemonic)?;
    /// # Ok::<(), bittensor::BittensorError>(())
    /// ```
    pub fn from_mnemonic(
        wallet_name: &str,
        hotkey_name: &str,
        mnemonic: &str,
    ) -> Result<Self, BittensorError> {
        let pair = sr25519::Pair::from_string(mnemonic, None).map_err(|e| {
            BittensorError::WalletError {
                message: format!("Invalid mnemonic: {e:?}"),
            }
        })?;

        let path =
            Self::default_wallet_path().unwrap_or_else(|_| PathBuf::from("~/.bittensor/wallets"));

        Ok(Self {
            name: wallet_name.to_string(),
            hotkey_name: hotkey_name.to_string(),
            path: path.join(wallet_name),
            hotkey_pair: pair,
            coldkey_pair: None,
        })
    }

    /// Create a wallet from a hex seed
    ///
    /// # Arguments
    ///
    /// * `wallet_name` - Name of the wallet
    /// * `hotkey_name` - Name of the hotkey
    /// * `seed_hex` - Hex-encoded seed (32 bytes, optionally prefixed with "0x")
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::wallet::Wallet;
    ///
    /// let seed = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    /// let wallet = Wallet::from_seed_hex("test", "test", seed).unwrap();
    /// ```
    pub fn from_seed_hex(
        wallet_name: &str,
        hotkey_name: &str,
        seed_hex: &str,
    ) -> Result<Self, BittensorError> {
        let hex_str = seed_hex.strip_prefix("0x").unwrap_or(seed_hex);
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
        let pair = sr25519::Pair::from_seed(&seed_array);

        let path =
            Self::default_wallet_path().unwrap_or_else(|_| PathBuf::from("~/.bittensor/wallets"));

        Ok(Self {
            name: wallet_name.to_string(),
            hotkey_name: hotkey_name.to_string(),
            path: path.join(wallet_name),
            hotkey_pair: pair,
            coldkey_pair: None,
        })
    }

    /// Get the hotkey address as a `Hotkey` type
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::wallet::Wallet;
    ///
    /// let wallet = Wallet::create_random("test", "test");
    /// let hotkey = wallet.hotkey();
    /// println!("Address: {}", hotkey);
    /// ```
    pub fn hotkey(&self) -> Hotkey {
        let public = self.hotkey_pair.public();
        let account_id = AccountId::from(public.0);
        Hotkey::from_account_id(&account_id)
    }

    /// Get the hotkey as an AccountId
    pub fn account_id(&self) -> AccountId {
        AccountId::from(self.hotkey_pair.public().0)
    }

    /// Sign data with the hotkey
    ///
    /// # Arguments
    ///
    /// * `data` - The data to sign
    ///
    /// # Returns
    ///
    /// A 64-byte signature
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::wallet::Wallet;
    ///
    /// let wallet = Wallet::create_random("test", "test");
    /// let signature = wallet.sign(b"hello world");
    /// assert_eq!(signature.len(), 64);
    /// ```
    pub fn sign(&self, data: &[u8]) -> Vec<u8> {
        let signature = self.hotkey_pair.sign(data);
        signature.0.to_vec()
    }

    /// Sign data and return hex-encoded signature
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::wallet::Wallet;
    ///
    /// let wallet = Wallet::create_random("test", "test");
    /// let sig_hex = wallet.sign_hex(b"hello");
    /// assert_eq!(sig_hex.len(), 128); // 64 bytes = 128 hex chars
    /// ```
    pub fn sign_hex(&self, data: &[u8]) -> String {
        hex::encode(self.sign(data))
    }

    /// Get a subxt-compatible signer for this wallet
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::wallet::Wallet;
    ///
    /// let wallet = Wallet::create_random("test", "test");
    /// let signer = wallet.signer();
    /// ```
    pub fn signer(&self) -> WalletSigner {
        WalletSigner::new(self.hotkey_pair.clone())
    }

    /// Get the underlying keypair (for advanced usage)
    pub fn keypair(&self) -> &sr25519::Pair {
        &self.hotkey_pair
    }

    /// Verify a signature against this wallet's hotkey
    ///
    /// # Arguments
    ///
    /// * `data` - The original data that was signed
    /// * `signature` - The 64-byte signature
    ///
    /// # Returns
    ///
    /// `true` if the signature is valid, `false` otherwise
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::wallet::Wallet;
    ///
    /// let wallet = Wallet::create_random("test", "test");
    /// let message = b"hello world";
    /// let signature = wallet.sign(message);
    /// assert!(wallet.verify(message, &signature));
    /// ```
    pub fn verify(&self, data: &[u8], signature: &[u8]) -> bool {
        if signature.len() != 64 {
            return false;
        }

        let mut sig_array = [0u8; 64];
        sig_array.copy_from_slice(signature);
        let sig = sr25519::Signature::from_raw(sig_array);

        use subxt::ext::sp_runtime::traits::Verify;
        sig.verify(data, &self.hotkey_pair.public())
    }

    /// Load and unlock the coldkey with a password
    ///
    /// The coldkey is stored in `<wallet_path>/coldkey` and is encrypted.
    ///
    /// # Arguments
    ///
    /// * `password` - The password to decrypt the coldkey
    ///
    /// # Returns
    ///
    /// * `Ok(())` if the coldkey was loaded and decrypted
    /// * `Err(BittensorError)` if loading or decryption failed
    pub fn unlock_coldkey(&mut self, password: &str) -> Result<(), BittensorError> {
        let coldkey_path = self.path.join("coldkey");

        if !coldkey_path.exists() {
            return Err(BittensorError::WalletError {
                message: format!("Coldkey file not found: {}", coldkey_path.display()),
            });
        }

        let keyfile_data = keyfile::load_encrypted_keyfile(&coldkey_path, password)?;
        let coldkey_pair = keyfile_data.to_keypair()?;

        self.coldkey_pair = Some(coldkey_pair);
        Ok(())
    }

    /// Check if the coldkey is unlocked
    pub fn is_coldkey_unlocked(&self) -> bool {
        self.coldkey_pair.is_some()
    }

    /// Get the coldkey address if unlocked
    pub fn coldkey(&self) -> Option<Hotkey> {
        self.coldkey_pair.as_ref().map(|pair| {
            let public = pair.public();
            let account_id = AccountId::from(public.0);
            Hotkey::from_account_id(&account_id)
        })
    }

    /// Get the default Bittensor wallet path
    fn default_wallet_path() -> Result<PathBuf, BittensorError> {
        home::home_dir()
            .map(|home| home.join(".bittensor").join("wallets"))
            .ok_or_else(|| BittensorError::WalletError {
                message: "Could not determine home directory".to_string(),
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_create_random_wallet() {
        let wallet = Wallet::create_random("test_wallet", "test_hotkey");
        assert_eq!(wallet.name, "test_wallet");
        assert_eq!(wallet.hotkey_name, "test_hotkey");
        // Check that we have a valid hotkey
        let hotkey = wallet.hotkey();
        assert!(!hotkey.as_str().is_empty());
    }

    #[test]
    fn test_sign_and_verify() {
        let wallet = Wallet::create_random("test", "test");
        let message = b"test message";
        let signature = wallet.sign(message);

        assert_eq!(signature.len(), 64);
        assert!(wallet.verify(message, &signature));
    }

    #[test]
    fn test_sign_hex() {
        let wallet = Wallet::create_random("test", "test");
        let sig_hex = wallet.sign_hex(b"test");
        assert_eq!(sig_hex.len(), 128);
        assert!(hex::decode(&sig_hex).is_ok());
    }

    #[test]
    fn test_from_seed_hex() {
        let seed = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        let wallet1 = Wallet::from_seed_hex("test", "test", seed).unwrap();
        let wallet2 = Wallet::from_seed_hex("test", "test", &format!("0x{}", seed)).unwrap();

        // Same seed should produce same hotkey
        assert_eq!(wallet1.hotkey().as_str(), wallet2.hotkey().as_str());
    }

    #[test]
    fn test_from_seed_hex_invalid() {
        // Too short
        let result = Wallet::from_seed_hex("test", "test", "0123");
        assert!(result.is_err());

        // Invalid hex
        let result = Wallet::from_seed_hex("test", "test", "not_hex_at_all!");
        assert!(result.is_err());
    }

    #[test]
    fn test_verify_wrong_signature() {
        let wallet = Wallet::create_random("test", "test");
        let wrong_sig = vec![0u8; 64];
        assert!(!wallet.verify(b"test", &wrong_sig));
    }

    #[test]
    fn test_verify_wrong_length() {
        let wallet = Wallet::create_random("test", "test");
        let short_sig = vec![0u8; 32];
        assert!(!wallet.verify(b"test", &short_sig));
    }

    #[test]
    fn test_account_id() {
        let wallet = Wallet::create_random("test", "test");
        let account_id = wallet.account_id();
        let hotkey = wallet.hotkey();

        // Account ID and hotkey should be consistent
        assert_eq!(account_id.to_string(), hotkey.as_str());
    }

    #[test]
    fn test_coldkey_not_unlocked() {
        let wallet = Wallet::create_random("test", "test");
        assert!(!wallet.is_coldkey_unlocked());
        assert!(wallet.coldkey().is_none());
    }
}
