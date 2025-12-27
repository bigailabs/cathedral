//! # Root Network Extrinsics
//!
//! Extrinsics for the root network (netuid 0):
//! - `set_root_weights`: Set weights on root network (uses set_weights with netuid=0)

use crate::api::api;
use crate::error::BittensorError;
use crate::extrinsics::ExtrinsicResponse;
use crate::AccountId;
use subxt::OnlineClient;
use subxt::PolkadotConfig;

/// Parameters for setting root network weights
#[derive(Debug, Clone)]
pub struct RootWeightsParams {
    /// Hotkey setting the weights
    pub hotkey: AccountId,
    /// Destination subnet netuids
    pub dests: Vec<u16>,
    /// Weight values (normalized to u16)
    pub weights: Vec<u16>,
    /// Version key for the weights
    pub version_key: u64,
}

impl RootWeightsParams {
    /// Create new root weights params
    ///
    /// # Arguments
    ///
    /// * `hotkey` - The hotkey setting weights
    /// * `dests` - Destination subnet netuids
    /// * `weights` - Weight values (will be normalized)
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::extrinsics::RootWeightsParams;
    /// use subxt::utils::AccountId32;
    ///
    /// let hotkey = AccountId32::from([1u8; 32]);
    /// let params = RootWeightsParams::new(
    ///     hotkey,
    ///     vec![1, 2, 3],     // subnet netuids
    ///     vec![100, 200, 300], // weights
    /// );
    /// ```
    pub fn new(hotkey: AccountId, dests: Vec<u16>, weights: Vec<u16>) -> Self {
        Self {
            hotkey,
            dests,
            weights,
            version_key: 0,
        }
    }

    /// Set the version key
    pub fn with_version_key(mut self, version_key: u64) -> Self {
        self.version_key = version_key;
        self
    }

    /// Normalize weights to ensure they sum to u16::MAX
    pub fn normalize_weights(&mut self) {
        if self.weights.is_empty() {
            return;
        }

        let sum: u64 = self.weights.iter().map(|&w| w as u64).sum();
        if sum == 0 {
            return;
        }

        let target = u16::MAX as u64;
        self.weights = self
            .weights
            .iter()
            .map(|&w| ((w as u64 * target) / sum) as u16)
            .collect();
    }
}

/// Set weights on the root network (netuid 0)
///
/// Root network weights determine the emission distribution across subnets.
/// Only validators registered in the root network can set these weights.
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The signer (coldkey)
/// * `params` - Root weights configuration
pub async fn set_root_weights<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: RootWeightsParams,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    // Root network is netuid 0
    let call = api::tx().subtensor_module().set_weights(
        0u16, // Root network
        params.dests,
        params.weights,
        params.version_key,
    );

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to set root weights: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Root weights set successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use subxt::utils::AccountId32;

    #[test]
    fn test_root_weights_params_new() {
        let hotkey = AccountId32::from([1u8; 32]);
        let params = RootWeightsParams::new(hotkey.clone(), vec![1, 2, 3], vec![100, 200, 300]);

        assert_eq!(params.hotkey, hotkey);
        assert_eq!(params.dests, vec![1, 2, 3]);
        assert_eq!(params.weights, vec![100, 200, 300]);
        assert_eq!(params.version_key, 0);
    }

    #[test]
    fn test_root_weights_with_version_key() {
        let hotkey = AccountId32::from([1u8; 32]);
        let params = RootWeightsParams::new(hotkey, vec![1], vec![100]).with_version_key(12345);

        assert_eq!(params.version_key, 12345);
    }

    #[test]
    fn test_normalize_weights() {
        let hotkey = AccountId32::from([1u8; 32]);
        let mut params = RootWeightsParams::new(hotkey, vec![1, 2], vec![100, 100]);

        params.normalize_weights();

        // After normalization, weights should sum to approximately u16::MAX
        let sum: u64 = params.weights.iter().map(|&w| w as u64).sum();
        // Allow for rounding errors
        assert!(sum >= u16::MAX as u64 - 2);
        assert!(sum <= u16::MAX as u64);
    }

    #[test]
    fn test_normalize_weights_unequal() {
        let hotkey = AccountId32::from([1u8; 32]);
        let mut params = RootWeightsParams::new(hotkey, vec![1, 2, 3], vec![1, 2, 1]);

        params.normalize_weights();

        // Weight[1] should be about 2x the others
        assert!(params.weights[1] > params.weights[0]);
        assert!(params.weights[1] > params.weights[2]);
    }

    #[test]
    fn test_normalize_weights_empty() {
        let hotkey = AccountId32::from([1u8; 32]);
        let mut params = RootWeightsParams::new(hotkey, vec![], vec![]);

        params.normalize_weights(); // Should not panic
        assert!(params.weights.is_empty());
    }

    #[test]
    fn test_normalize_weights_zero_sum() {
        let hotkey = AccountId32::from([1u8; 32]);
        let mut params = RootWeightsParams::new(hotkey, vec![1, 2], vec![0, 0]);

        params.normalize_weights();
        assert_eq!(params.weights, vec![0, 0]);
    }

    #[test]
    fn test_root_weights_clone() {
        let hotkey = AccountId32::from([1u8; 32]);
        let params = RootWeightsParams::new(hotkey, vec![1, 2], vec![100, 200]);
        let cloned = params.clone();

        assert_eq!(params.dests, cloned.dests);
        assert_eq!(params.weights, cloned.weights);
    }

    #[test]
    fn test_root_weights_debug() {
        let hotkey = AccountId32::from([1u8; 32]);
        let params = RootWeightsParams::new(hotkey, vec![1], vec![100]);
        let debug = format!("{:?}", params);
        assert!(debug.contains("RootWeightsParams"));
    }
}
