use crate::error::{PaymentsError, Result};
use anyhow::Context;
use basilica_common::crypto::{wallet::sr25519_pair_from_mnemonic, Aead};
use sp_core::sr25519;
use std::sync::Arc;
use zeroize::Zeroizing;

pub struct WalletManager {
    aead: Arc<Aead>,
}

impl WalletManager {
    pub fn new(aead: Arc<Aead>) -> Self {
        Self { aead }
    }

    #[allow(clippy::result_large_err)]
    pub fn decrypt_and_create_keypair(&self, encrypted_mnemonic: &str) -> Result<sr25519::Pair> {
        let mnemonic_string = self
            .aead
            .decrypt(encrypted_mnemonic)
            .context("Failed to decrypt mnemonic")
            .map_err(|e| PaymentsError::Encryption(e.to_string()))?;

        let mnemonic = Zeroizing::new(mnemonic_string);

        let keypair = sr25519_pair_from_mnemonic(mnemonic.as_str())
            .context("Failed to create keypair from mnemonic")
            .map_err(|e| PaymentsError::Encryption(e.to_string()))?;

        Ok(keypair)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sp_core::Pair as PairTrait;

    const TEST_MNEMONIC: &str =
        "bottom drive obey lake curtain smoke basket hold race lonely fit walk";
    const TEST_AEAD_KEY: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    #[test]
    fn test_decrypt_and_create_keypair() {
        let aead = Arc::new(Aead::new(TEST_AEAD_KEY).unwrap());
        let manager = WalletManager::new(aead.clone());

        let encrypted = aead.encrypt(TEST_MNEMONIC).unwrap();

        let keypair = manager.decrypt_and_create_keypair(&encrypted).unwrap();

        assert!(!format!("{:?}", keypair.public()).is_empty());
    }

    #[test]
    fn test_invalid_encrypted_data() {
        let aead = Arc::new(Aead::new(TEST_AEAD_KEY).unwrap());
        let manager = WalletManager::new(aead);

        let result = manager.decrypt_and_create_keypair("invalid:data");
        assert!(result.is_err());
    }

    #[test]
    fn test_invalid_mnemonic_after_decryption() {
        let aead = Arc::new(Aead::new(TEST_AEAD_KEY).unwrap());
        let manager = WalletManager::new(aead.clone());

        let invalid_mnemonic = "not a valid mnemonic phrase at all";
        let encrypted = aead.encrypt(invalid_mnemonic).unwrap();

        let result = manager.decrypt_and_create_keypair(&encrypted);
        assert!(result.is_err());
    }
}
