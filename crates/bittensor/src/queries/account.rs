//! # Account Queries
//!
//! Query account balances and stake information.

use crate::api::api;
use crate::error::BittensorError;
use crate::types::Balance;
use crate::AccountId;
use std::str::FromStr;
use subxt::OnlineClient;
use subxt::PolkadotConfig;

/// Get the free balance of an account
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `address` - The SS58 address to query
///
/// # Returns
///
/// The free balance in RAO
///
/// # Example
///
/// ```rust,no_run
/// use bittensor::queries::get_balance;
///
/// # async fn example() -> Result<(), Box<dyn std::error::Error>> {
/// # let client: subxt::OnlineClient<subxt::PolkadotConfig> = todo!();
/// let balance = get_balance(&client, "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY").await?;
/// println!("Balance: {}", balance);
/// # Ok(())
/// # }
/// ```
pub async fn get_balance(
    client: &OnlineClient<PolkadotConfig>,
    address: &str,
) -> Result<Balance, BittensorError> {
    let account_id = AccountId::from_str(address).map_err(|_| BittensorError::InvalidHotkey {
        hotkey: address.to_string(),
    })?;

    let storage = api::storage().system().account(&account_id);

    let account_info = client
        .storage()
        .at_latest()
        .await
        .map_err(|e| BittensorError::RpcError {
            message: format!("Failed to get storage: {}", e),
        })?
        .fetch(&storage)
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "account".to_string(),
            message: format!("Failed to fetch account: {}", e),
        })?;

    match account_info {
        Some(info) => Ok(Balance::from_rao(info.data.free)),
        None => Ok(Balance::zero()),
    }
}

/// Get the stake of a hotkey on a specific coldkey for a subnet
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `hotkey` - The hotkey SS58 address
/// * `coldkey` - The coldkey SS58 address
/// * `netuid` - The subnet netuid
pub async fn get_stake(
    client: &OnlineClient<PolkadotConfig>,
    hotkey: &str,
    coldkey: &str,
    netuid: u16,
) -> Result<Balance, BittensorError> {
    let hotkey_id = AccountId::from_str(hotkey).map_err(|_| BittensorError::InvalidHotkey {
        hotkey: hotkey.to_string(),
    })?;

    let coldkey_id = AccountId::from_str(coldkey).map_err(|_| BittensorError::InvalidHotkey {
        hotkey: coldkey.to_string(),
    })?;

    let storage = api::storage()
        .subtensor_module()
        .alpha(&hotkey_id, &coldkey_id, netuid);

    let stake = client
        .storage()
        .at_latest()
        .await
        .map_err(|e| BittensorError::RpcError {
            message: format!("Failed to get storage: {}", e),
        })?
        .fetch(&storage)
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "stake".to_string(),
            message: format!("Failed to fetch stake: {}", e),
        })?;

    match stake {
        // alpha returns FixedU128 with a `bits` field
        // The fixed-point representation stores the value shifted by 64 bits
        Some(amount) => {
            // Get the raw bits and convert to u64 representing RAO
            let raw_bits: u128 = amount.bits;
            // Shift right by 64 to get the integer part
            let rao = (raw_bits >> 64) as u64;
            Ok(Balance::from_rao(rao))
        }
        None => Ok(Balance::zero()),
    }
}

/// Get the global total stake on the network
///
/// This returns the total stake across all coldkeys and hotkeys.
///
/// # Arguments
///
/// * `client` - The subxt client
pub async fn get_total_network_stake(
    client: &OnlineClient<PolkadotConfig>,
) -> Result<Balance, BittensorError> {
    let storage = api::storage().subtensor_module().total_stake();

    let stake = client
        .storage()
        .at_latest()
        .await
        .map_err(|e| BittensorError::RpcError {
            message: format!("Failed to get storage: {}", e),
        })?
        .fetch(&storage)
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "total_stake".to_string(),
            message: format!("Failed to fetch total stake: {}", e),
        })?;

    match stake {
        Some(amount) => Ok(Balance::from_rao(amount)),
        None => Ok(Balance::zero()),
    }
}

/// Stake information returned from runtime API
#[derive(Debug, Clone)]
pub struct StakeInfo {
    /// The hotkey address
    pub hotkey: AccountId,
    /// The coldkey address
    pub coldkey: AccountId,
    /// The subnet netuid
    pub netuid: u16,
    /// The stake amount
    pub stake: Balance,
    /// Locked stake amount
    pub locked: Balance,
    /// Emission earned
    pub emission: Balance,
    /// TAO emission earned
    pub tao_emission: Balance,
    /// Drain amount
    pub drain: Balance,
    /// Whether this is registered
    pub is_registered: bool,
}

/// Get stake information for a coldkey using runtime API
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `coldkey` - The coldkey SS58 address
pub async fn get_stake_info_for_coldkey(
    client: &OnlineClient<PolkadotConfig>,
    coldkey: &str,
) -> Result<Vec<StakeInfo>, BittensorError> {
    let coldkey_id = AccountId::from_str(coldkey).map_err(|_| BittensorError::InvalidHotkey {
        hotkey: coldkey.to_string(),
    })?;

    let runtime_api =
        client
            .runtime_api()
            .at_latest()
            .await
            .map_err(|e| BittensorError::RpcError {
                message: format!("Failed to get runtime API: {}", e),
            })?;

    let stake_infos = runtime_api
        .call(
            api::runtime_apis::stake_info_runtime_api::StakeInfoRuntimeApi
                .get_stake_info_for_coldkey(coldkey_id.clone()),
        )
        .await
        .map_err(|e| BittensorError::RpcMethodError {
            method: "get_stake_info_for_coldkey".to_string(),
            message: e.to_string(),
        })?;

    let result = stake_infos
        .into_iter()
        .map(|info| StakeInfo {
            hotkey: info.hotkey,
            coldkey: info.coldkey,
            netuid: info.netuid,
            stake: Balance::from_rao(info.stake),
            locked: Balance::from_rao(info.locked),
            emission: Balance::from_rao(info.emission),
            tao_emission: Balance::from_rao(info.tao_emission),
            drain: Balance::from_rao(info.drain),
            is_registered: info.is_registered,
        })
        .collect();

    Ok(result)
}

#[cfg(test)]
mod tests {
    #[test]
    fn test_balance_type() {
        use crate::types::Balance;
        let balance = Balance::from_rao(1_000_000_000);
        assert_eq!(balance.as_tao(), 1.0);
    }

    #[test]
    fn test_stake_info_struct() {
        use super::*;
        use subxt::utils::AccountId32;

        let _info = StakeInfo {
            hotkey: AccountId32::from([0u8; 32]),
            coldkey: AccountId32::from([1u8; 32]),
            netuid: 1,
            stake: Balance::from_rao(1000),
            locked: Balance::from_rao(0),
            emission: Balance::from_rao(100),
            tao_emission: Balance::from_rao(50),
            drain: Balance::from_rao(0),
            is_registered: true,
        };
    }
}
