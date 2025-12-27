//! # Weights Extrinsics
//!
//! Extrinsics for setting neuron weights on the Bittensor network:
//! - `set_weights`: Directly set weights for neurons
//! - `commit_weights`: Commit weights hash (for commit-reveal scheme)
//! - `reveal_weights`: Reveal previously committed weights

use crate::api::api;
use crate::error::BittensorError;
use crate::extrinsics::ExtrinsicResponse;
use crate::utils::{normalize_weights, NormalizedWeight};
use subxt::OnlineClient;
use subxt::PolkadotConfig;

/// Parameters for setting weights
#[derive(Debug, Clone)]
pub struct WeightsParams {
    /// Subnet netuid
    pub netuid: u16,
    /// UIDs to set weights for
    pub uids: Vec<u16>,
    /// Weight values (will be normalized)
    pub weights: Vec<u16>,
    /// Version key for the weights
    pub version_key: u64,
}

impl WeightsParams {
    /// Create new weights params
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::extrinsics::WeightsParams;
    ///
    /// let params = WeightsParams::new(1, vec![0, 1, 2], vec![100, 200, 300]);
    /// assert_eq!(params.netuid, 1);
    /// assert_eq!(params.uids.len(), 3);
    /// ```
    pub fn new(netuid: u16, uids: Vec<u16>, weights: Vec<u16>) -> Result<Self, &'static str> {
        if uids.len() != weights.len() {
            return Err("UIDs and weights must have the same length");
        }
        Ok(Self {
            netuid,
            uids,
            weights,
            version_key: 0,
        })
    }

    /// Set the version key
    pub fn with_version_key(mut self, version_key: u64) -> Self {
        self.version_key = version_key;
        self
    }

    /// Convert to normalized weights
    pub fn to_normalized(&self) -> Vec<NormalizedWeight> {
        let weight_pairs: Vec<(u16, u16)> = self
            .uids
            .iter()
            .zip(self.weights.iter())
            .map(|(u, w)| (*u, *w))
            .collect();
        normalize_weights(&weight_pairs)
    }
}

/// Parameters for commit-reveal weights scheme
#[derive(Debug, Clone)]
pub struct CommitRevealParams {
    /// Subnet netuid
    pub netuid: u16,
    /// Commit hash (32 bytes)
    pub commit_hash: [u8; 32],
    /// UIDs for the weights (used in reveal)
    pub uids: Vec<u16>,
    /// Weight values (used in reveal)
    pub weights: Vec<u16>,
    /// Salt for the commit (used in reveal)
    pub salt: Vec<u16>,
    /// Version key
    pub version_key: u64,
}

impl CommitRevealParams {
    /// Create new commit-reveal params with a pre-computed hash
    pub fn new_with_hash(netuid: u16, commit_hash: [u8; 32]) -> Self {
        Self {
            netuid,
            commit_hash,
            uids: Vec::new(),
            weights: Vec::new(),
            salt: Vec::new(),
            version_key: 0,
        }
    }

    /// Create commit-reveal params with weights and salt
    ///
    /// The commit hash will be computed from the weights and salt.
    pub fn new_with_weights(
        netuid: u16,
        uids: Vec<u16>,
        weights: Vec<u16>,
        salt: Vec<u16>,
        version_key: u64,
    ) -> Self {
        let commit_hash = compute_commit_hash(&uids, &weights, &salt, version_key);

        Self {
            netuid,
            commit_hash,
            uids,
            weights,
            salt,
            version_key,
        }
    }
}

/// Compute the commit hash for weights
fn compute_commit_hash(uids: &[u16], weights: &[u16], salt: &[u16], version_key: u64) -> [u8; 32] {
    use subxt::ext::sp_core::keccak_256;

    let mut data = Vec::new();

    for uid in uids {
        data.extend_from_slice(&uid.to_le_bytes());
    }

    for weight in weights {
        data.extend_from_slice(&weight.to_le_bytes());
    }

    for s in salt {
        data.extend_from_slice(&s.to_le_bytes());
    }

    data.extend_from_slice(&version_key.to_le_bytes());

    keccak_256(&data)
}

/// Set weights for neurons in a subnet
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The validator's hotkey signer
/// * `params` - Weight parameters
///
/// # Returns
///
/// An `ExtrinsicResponse` with the result
///
/// # Example
///
/// ```rust,no_run
/// use bittensor::extrinsics::{set_weights, WeightsParams};
///
/// # async fn example() -> Result<(), Box<dyn std::error::Error>> {
/// # let client: subxt::OnlineClient<subxt::PolkadotConfig> = todo!();
/// # let signer: bittensor::WalletSigner = todo!();
/// let params = WeightsParams::new(1, vec![0, 1, 2], vec![100, 200, 300]);
/// let result = set_weights(&client, &signer, params).await?;
/// # Ok(())
/// # }
/// ```
pub async fn set_weights<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: WeightsParams,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let normalized = params.to_normalized();
    let (dests, values): (Vec<u16>, Vec<u16>) =
        normalized.into_iter().map(|w| (w.uid, w.weight)).unzip();

    let call =
        api::tx()
            .subtensor_module()
            .set_weights(params.netuid, dests, values, params.version_key);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit set_weights: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Weights set successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

/// Commit weights hash for the commit-reveal scheme
///
/// In the commit-reveal scheme, validators first commit a hash of their
/// weights, then reveal the actual weights later.
pub async fn commit_weights<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: CommitRevealParams,
) -> Result<ExtrinsicResponse<[u8; 32]>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let commit_hash_h256 = subxt::utils::H256::from_slice(&params.commit_hash);

    let call = api::tx()
        .subtensor_module()
        .commit_weights(params.netuid, commit_hash_h256);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit commit_weights: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Weights committed successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(params.commit_hash))
}

/// Reveal previously committed weights
pub async fn reveal_weights<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: CommitRevealParams,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    if params.uids.is_empty() || params.weights.is_empty() || params.salt.is_empty() {
        return Err(BittensorError::ConfigError {
            field: "params".to_string(),
            message: "UIDs, weights, and salt are required for reveal".to_string(),
        });
    }

    let call = api::tx().subtensor_module().reveal_weights(
        params.netuid,
        params.uids,
        params.weights,
        params.salt,
        params.version_key,
    );

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit reveal_weights: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Weights revealed successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_weights_params() {
        let params = WeightsParams::new(1, vec![0, 1, 2], vec![100, 200, 300]);
        assert_eq!(params.netuid, 1);
        assert_eq!(params.uids.len(), 3);
        assert_eq!(params.weights.len(), 3);
        assert_eq!(params.version_key, 0);
    }

    #[test]
    fn test_weights_params_builder() {
        let params = WeightsParams::new(1, vec![0], vec![100]).with_version_key(42);
        assert_eq!(params.version_key, 42);
    }

    #[test]
    fn test_normalize_weights() {
        let params = WeightsParams::new(1, vec![0, 1], vec![100, 100]);
        let normalized = params.to_normalized();

        assert_eq!(normalized.len(), 2);
        // Both should be approximately equal (half of u16::MAX each)
        let diff = (normalized[0].weight as i32 - normalized[1].weight as i32).abs();
        assert!(diff < 2);
    }

    #[test]
    fn test_commit_reveal_params() {
        let params = CommitRevealParams::new_with_weights(
            1,
            vec![0, 1, 2],
            vec![100, 200, 300],
            vec![1, 2, 3],
            0,
        );

        assert_eq!(params.netuid, 1);
        assert_eq!(params.uids.len(), 3);
        // Commit hash should be non-zero
        assert!(params.commit_hash.iter().any(|&b| b != 0));
    }

    #[test]
    fn test_commit_hash_deterministic() {
        let hash1 = compute_commit_hash(&[0, 1], &[100, 200], &[1, 2], 0);
        let hash2 = compute_commit_hash(&[0, 1], &[100, 200], &[1, 2], 0);
        assert_eq!(hash1, hash2);

        let hash3 = compute_commit_hash(&[0, 1], &[100, 201], &[1, 2], 0);
        assert_ne!(hash1, hash3);
    }

    #[test]
    fn test_commit_reveal_params_with_hash() {
        let hash = [1u8; 32];
        let params = CommitRevealParams::new_with_hash(1, hash);
        assert_eq!(params.commit_hash, hash);
        assert!(params.uids.is_empty());
    }
}
