//! # Staking Extrinsics
//!
//! Extrinsics for staking operations on the Bittensor network:
//! - `add_stake`: Add stake to a hotkey
//! - `remove_stake`: Remove stake from a hotkey
//! - `delegate_stake`: Delegate stake to another validator
//! - `undelegate_stake`: Remove delegated stake

use crate::api::api;
use crate::error::BittensorError;
use crate::extrinsics::ExtrinsicResponse;
use crate::types::Balance;
use crate::AccountId;
use std::str::FromStr;
use subxt::OnlineClient;
use subxt::PolkadotConfig;

/// Parameters for staking operations
#[derive(Debug, Clone)]
pub struct StakeParams {
    /// The hotkey to stake to/from
    pub hotkey: String,
    /// Subnet netuid
    pub netuid: u16,
    /// Amount to stake in RAO
    pub amount_rao: u64,
}

impl StakeParams {
    /// Create new stake params with amount in TAO
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::extrinsics::StakeParams;
    ///
    /// let params = StakeParams::new_tao(
    ///     "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    ///     1,   // netuid
    ///     1.5  // TAO
    /// );
    /// assert_eq!(params.amount_rao, 1_500_000_000);
    /// ```
    pub fn new_tao(hotkey: &str, netuid: u16, amount_tao: f64) -> Self {
        Self {
            hotkey: hotkey.to_string(),
            netuid,
            amount_rao: (amount_tao * 1_000_000_000.0) as u64,
        }
    }

    /// Create new stake params with amount in RAO
    pub fn new_rao(hotkey: &str, netuid: u16, amount_rao: u64) -> Self {
        Self {
            hotkey: hotkey.to_string(),
            netuid,
            amount_rao,
        }
    }
}

/// Add stake to a hotkey
///
/// Stakes TAO from the coldkey to the specified hotkey.
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The coldkey signer (must have sufficient balance)
/// * `params` - Staking parameters
///
/// # Returns
///
/// An `ExtrinsicResponse` with the staking result
///
/// # Example
///
/// ```rust,no_run
/// use bittensor::extrinsics::{add_stake, StakeParams};
///
/// # async fn example() -> Result<(), Box<dyn std::error::Error>> {
/// # let client: subxt::OnlineClient<subxt::PolkadotConfig> = todo!();
/// # let signer: bittensor::WalletSigner = todo!();
/// let params = StakeParams::new_tao(
///     "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
///     1,    // netuid
///     1.0   // TAO amount
/// );
/// let result = add_stake(&client, &signer, params).await?;
/// # Ok(())
/// # }
/// ```
pub async fn add_stake<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: StakeParams,
) -> Result<ExtrinsicResponse<Balance>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let hotkey_account =
        AccountId::from_str(&params.hotkey).map_err(|_| BittensorError::InvalidHotkey {
            hotkey: params.hotkey.clone(),
        })?;

    let call =
        api::tx()
            .subtensor_module()
            .add_stake(hotkey_account, params.netuid, params.amount_rao);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit add_stake: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Stake added successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(Balance::from_rao(params.amount_rao)))
}

/// Remove stake from a hotkey
///
/// Unstakes TAO from the specified hotkey back to the coldkey.
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The coldkey signer
/// * `params` - Staking parameters
///
/// # Returns
///
/// An `ExtrinsicResponse` with the unstaking result
pub async fn remove_stake<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: StakeParams,
) -> Result<ExtrinsicResponse<Balance>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let hotkey_account =
        AccountId::from_str(&params.hotkey).map_err(|_| BittensorError::InvalidHotkey {
            hotkey: params.hotkey.clone(),
        })?;

    let call =
        api::tx()
            .subtensor_module()
            .remove_stake(hotkey_account, params.netuid, params.amount_rao);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit remove_stake: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Stake removed successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(Balance::from_rao(params.amount_rao)))
}

/// Delegate stake to a validator
///
/// Delegates your stake to another validator's hotkey.
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The coldkey signer
/// * `delegate_hotkey` - The hotkey to delegate to
/// * `netuid` - Subnet netuid
/// * `amount_rao` - Amount to delegate in RAO
pub async fn delegate_stake<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    delegate_hotkey: &str,
    netuid: u16,
    amount_rao: u64,
) -> Result<ExtrinsicResponse<Balance>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let params = StakeParams::new_rao(delegate_hotkey, netuid, amount_rao);
    add_stake(client, signer, params).await
}

/// Remove delegated stake
///
/// Removes stake that was delegated to another validator.
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The coldkey signer
/// * `delegate_hotkey` - The hotkey to undelegate from
/// * `netuid` - Subnet netuid
/// * `amount_rao` - Amount to undelegate in RAO
pub async fn undelegate_stake<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    delegate_hotkey: &str,
    netuid: u16,
    amount_rao: u64,
) -> Result<ExtrinsicResponse<Balance>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let params = StakeParams::new_rao(delegate_hotkey, netuid, amount_rao);
    remove_stake(client, signer, params).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_stake_params_tao() {
        let params =
            StakeParams::new_tao("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY", 1, 1.5);
        assert_eq!(params.amount_rao, 1_500_000_000);
        assert_eq!(params.netuid, 1);
    }

    #[test]
    fn test_stake_params_rao() {
        let params =
            StakeParams::new_rao("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY", 1, 1000);
        assert_eq!(params.amount_rao, 1000);
    }
}
