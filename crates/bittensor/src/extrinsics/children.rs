//! # Children Extrinsics
//!
//! Extrinsics for managing child hotkeys on the Bittensor network:
//! - `set_children`: Set child hotkeys with proportions
//! - `set_childkey_take`: Set the take rate for child keys

use crate::api::api;
use crate::error::BittensorError;
use crate::extrinsics::ExtrinsicResponse;
use crate::AccountId;
use subxt::OnlineClient;
use subxt::PolkadotConfig;

/// A child hotkey with its proportion of stake/rewards
#[derive(Debug, Clone)]
pub struct ChildKey {
    /// Child hotkey proportion (0-u64::MAX, where u64::MAX = 100%)
    pub proportion: u64,
    /// Child hotkey account ID
    pub child: AccountId,
}

impl ChildKey {
    /// Create a new child key entry
    ///
    /// # Arguments
    ///
    /// * `child` - The child hotkey account ID
    /// * `proportion` - The proportion (0.0 to 1.0 will be converted to u64)
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::extrinsics::ChildKey;
    /// use subxt::utils::AccountId32;
    ///
    /// let child = AccountId32::from([1u8; 32]);
    /// let child_key = ChildKey::new(child, 0.5); // 50% proportion
    /// ```
    pub fn new(child: AccountId, proportion: f64) -> Self {
        let proportion = (proportion.clamp(0.0, 1.0) * u64::MAX as f64) as u64;
        Self { proportion, child }
    }

    /// Create with raw proportion value
    pub fn new_raw(child: AccountId, proportion: u64) -> Self {
        Self { proportion, child }
    }
}

/// Parameters for setting children
#[derive(Debug, Clone)]
pub struct SetChildrenParams {
    /// The subnet netuid
    pub netuid: u16,
    /// List of child hotkeys with their proportions
    pub children: Vec<ChildKey>,
}

impl SetChildrenParams {
    /// Create new set children params
    pub fn new(netuid: u16) -> Self {
        Self {
            netuid,
            children: Vec::new(),
        }
    }

    /// Add a child hotkey
    pub fn with_child(mut self, child: AccountId, proportion: f64) -> Self {
        self.children.push(ChildKey::new(child, proportion));
        self
    }

    /// Add multiple children at once
    pub fn with_children(mut self, children: Vec<ChildKey>) -> Self {
        self.children.extend(children);
        self
    }
}

/// Set child hotkeys for the signing hotkey
///
/// Child hotkeys receive a portion of the parent's stake/rewards.
/// The proportions should sum to <= 1.0 (100%).
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The signer (parent hotkey's coldkey)
/// * `hotkey` - The parent hotkey
/// * `params` - Children configuration
pub async fn set_children<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    hotkey: AccountId,
    params: SetChildrenParams,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let children: Vec<(u64, AccountId)> = params
        .children
        .into_iter()
        .map(|c| (c.proportion, c.child))
        .collect();

    let call = api::tx()
        .subtensor_module()
        .set_children(hotkey, params.netuid, children);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to set children: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Children set successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

/// Set the take rate for child keys
///
/// This sets the percentage that the parent hotkey takes from child rewards.
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The signer (hotkey's coldkey)
/// * `hotkey` - The hotkey setting the take
/// * `netuid` - The subnet netuid
/// * `take` - The take rate (0.0 to 1.0)
pub async fn set_childkey_take<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    hotkey: AccountId,
    netuid: u16,
    take: f64,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    // Convert 0.0-1.0 to u16 (0-65535 where 65535 = 100%)
    let take_u16 = (take.clamp(0.0, 1.0) * u16::MAX as f64) as u16;

    let call = api::tx()
        .subtensor_module()
        .set_childkey_take(hotkey, netuid, take_u16);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to set childkey take: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Childkey take set successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

/// Revoke all children for a hotkey on a subnet
///
/// This is equivalent to setting an empty children list.
pub async fn revoke_children<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    hotkey: AccountId,
    netuid: u16,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let children: Vec<(u64, AccountId)> = Vec::new();

    let call = api::tx()
        .subtensor_module()
        .set_children(hotkey, netuid, children);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to revoke children: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Children revoked successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use subxt::utils::AccountId32;

    #[test]
    fn test_child_key_new() {
        let child = AccountId32::from([1u8; 32]);
        let key = ChildKey::new(child.clone(), 0.5);
        assert_eq!(key.child, child);
        // 0.5 * u64::MAX should be approximately half of u64::MAX
        let third = u64::MAX / 3;
        let two_thirds = u64::MAX / 3 * 2;
        assert!(key.proportion > third);
        assert!(key.proportion < two_thirds);
    }

    #[test]
    fn test_child_key_clamping() {
        let child = AccountId32::from([1u8; 32]);

        // Test negative proportion clamped to 0
        let key = ChildKey::new(child.clone(), -0.5);
        assert_eq!(key.proportion, 0);

        // Test proportion > 1 clamped to max
        let key = ChildKey::new(child, 1.5);
        assert_eq!(key.proportion, u64::MAX);
    }

    #[test]
    fn test_child_key_raw() {
        let child = AccountId32::from([1u8; 32]);
        let key = ChildKey::new_raw(child.clone(), 12345);
        assert_eq!(key.proportion, 12345);
    }

    #[test]
    fn test_set_children_params() {
        let child1 = AccountId32::from([1u8; 32]);
        let child2 = AccountId32::from([2u8; 32]);

        let params = SetChildrenParams::new(1)
            .with_child(child1.clone(), 0.3)
            .with_child(child2.clone(), 0.2);

        assert_eq!(params.netuid, 1);
        assert_eq!(params.children.len(), 2);
    }

    #[test]
    fn test_set_children_params_with_children() {
        let child1 = AccountId32::from([1u8; 32]);
        let child2 = AccountId32::from([2u8; 32]);

        let children = vec![ChildKey::new(child1, 0.5), ChildKey::new(child2, 0.5)];

        let params = SetChildrenParams::new(1).with_children(children);
        assert_eq!(params.children.len(), 2);
    }

    #[test]
    fn test_child_key_clone() {
        let child = AccountId32::from([1u8; 32]);
        let key = ChildKey::new(child, 0.5);
        let cloned = key.clone();
        assert_eq!(key.proportion, cloned.proportion);
        assert_eq!(key.child, cloned.child);
    }

    #[test]
    fn test_child_key_debug() {
        let child = AccountId32::from([1u8; 32]);
        let key = ChildKey::new(child, 0.5);
        let debug = format!("{:?}", key);
        assert!(debug.contains("ChildKey"));
    }
}
