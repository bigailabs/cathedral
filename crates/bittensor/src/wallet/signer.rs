//! # Wallet Signer
//!
//! A subxt-compatible signer wrapper for Bittensor wallets.

use subxt::config::polkadot::PolkadotConfig;
use subxt::ext::sp_core::sr25519;
use subxt::tx::Signer;

/// A signer that wraps an sr25519 keypair for use with subxt
///
/// This implements the `Signer` trait required by subxt for signing
/// extrinsics.
#[derive(Clone)]
pub struct WalletSigner {
    inner: subxt::tx::PairSigner<PolkadotConfig, sr25519::Pair>,
}

impl WalletSigner {
    /// Create a new signer from an sr25519 keypair
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::wallet::WalletSigner;
    /// use subxt::ext::sp_core::{sr25519, Pair};
    ///
    /// let (pair, _) = sr25519::Pair::generate();
    /// let signer = WalletSigner::new(pair);
    /// ```
    pub fn new(pair: sr25519::Pair) -> Self {
        Self {
            inner: subxt::tx::PairSigner::new(pair),
        }
    }

    /// Get the underlying PairSigner for advanced usage
    pub fn inner(&self) -> &subxt::tx::PairSigner<PolkadotConfig, sr25519::Pair> {
        &self.inner
    }
}

impl Signer<PolkadotConfig> for WalletSigner {
    fn account_id(&self) -> <PolkadotConfig as subxt::Config>::AccountId {
        self.inner.account_id().clone()
    }

    fn address(&self) -> <PolkadotConfig as subxt::Config>::Address {
        self.inner.address()
    }

    fn sign(&self, payload: &[u8]) -> <PolkadotConfig as subxt::Config>::Signature {
        self.inner.sign(payload)
    }
}

impl std::fmt::Debug for WalletSigner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WalletSigner")
            .field("account_id", &self.account_id())
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use subxt::ext::sp_core::Pair;

    #[test]
    fn test_signer_creation() {
        let (pair, _) = sr25519::Pair::generate();
        let signer = WalletSigner::new(pair.clone());

        // Check account ID matches
        let expected_account = subxt::config::polkadot::AccountId32::from(pair.public().0);
        assert_eq!(signer.account_id(), expected_account);
    }

    #[test]
    fn test_signer_sign() {
        let (pair, _) = sr25519::Pair::generate();
        let signer = WalletSigner::new(pair);

        let data = b"test message";
        let signature = signer.sign(data);

        // Signature should be Sr25519
        match signature {
            subxt::utils::MultiSignature::Sr25519(_) => {}
            _ => panic!("Expected Sr25519 signature"),
        }
    }

    #[test]
    fn test_signer_debug() {
        let (pair, _) = sr25519::Pair::generate();
        let signer = WalletSigner::new(pair);

        let debug_str = format!("{:?}", signer);
        assert!(debug_str.contains("WalletSigner"));
        assert!(debug_str.contains("account_id"));
    }
}
