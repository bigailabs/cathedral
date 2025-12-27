//! # Neuron Queries
//!
//! Query individual neuron information from the Bittensor network.

use crate::api::api;
use crate::error::BittensorError;
use crate::AccountId;
use std::str::FromStr;
use subxt::OnlineClient;
use subxt::PolkadotConfig;

/// NeuronInfo type with AccountId
pub type NeuronInfo =
    crate::api::api::runtime_types::pallet_subtensor::rpc_info::neuron_info::NeuronInfo<AccountId>;

/// NeuronInfoLite type with AccountId
pub type NeuronInfoLite =
    crate::api::api::runtime_types::pallet_subtensor::rpc_info::neuron_info::NeuronInfoLite<
        AccountId,
    >;

/// Get full neuron information by UID
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `netuid` - The subnet netuid
/// * `uid` - The neuron UID
///
/// # Returns
///
/// Full neuron information including hotkey, coldkey, stake, etc.
pub async fn get_neuron(
    client: &OnlineClient<PolkadotConfig>,
    netuid: u16,
    uid: u16,
) -> Result<NeuronInfo, BittensorError> {
    let runtime_api =
        client
            .runtime_api()
            .at_latest()
            .await
            .map_err(|e| BittensorError::RpcError {
                message: format!("Failed to get runtime API: {}", e),
            })?;

    let neuron = runtime_api
        .call(
            api::runtime_apis::neuron_info_runtime_api::NeuronInfoRuntimeApi
                .get_neuron(netuid, uid),
        )
        .await
        .map_err(|e| BittensorError::RpcMethodError {
            method: "get_neuron".to_string(),
            message: e.to_string(),
        })?
        .ok_or(BittensorError::NeuronNotFound { uid, netuid })?;

    Ok(neuron)
}

/// Get lite neuron information by UID
///
/// This is more efficient than `get_neuron` if you don't need all fields.
pub async fn get_neuron_lite(
    client: &OnlineClient<PolkadotConfig>,
    netuid: u16,
    uid: u16,
) -> Result<NeuronInfoLite, BittensorError> {
    let runtime_api =
        client
            .runtime_api()
            .at_latest()
            .await
            .map_err(|e| BittensorError::RpcError {
                message: format!("Failed to get runtime API: {}", e),
            })?;

    let neuron = runtime_api
        .call(
            api::runtime_apis::neuron_info_runtime_api::NeuronInfoRuntimeApi
                .get_neuron_lite(netuid, uid),
        )
        .await
        .map_err(|e| BittensorError::RpcMethodError {
            method: "get_neuron_lite".to_string(),
            message: e.to_string(),
        })?
        .ok_or(BittensorError::NeuronNotFound { uid, netuid })?;

    Ok(neuron)
}

/// Get the UID for a hotkey on a subnet
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `netuid` - The subnet netuid
/// * `hotkey` - The hotkey SS58 address
///
/// # Returns
///
/// The UID of the neuron, or an error if not registered
pub async fn get_uid_for_hotkey(
    client: &OnlineClient<PolkadotConfig>,
    netuid: u16,
    hotkey: &str,
) -> Result<u16, BittensorError> {
    let hotkey_id = AccountId::from_str(hotkey).map_err(|_| BittensorError::InvalidHotkey {
        hotkey: hotkey.to_string(),
    })?;

    let storage = api::storage().subtensor_module().uids(netuid, &hotkey_id);

    let uid = client
        .storage()
        .at_latest()
        .await
        .map_err(|e| BittensorError::RpcError {
            message: format!("Failed to get storage: {}", e),
        })?
        .fetch(&storage)
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "uids".to_string(),
            message: format!("Failed to fetch uid: {}", e),
        })?;

    uid.ok_or_else(|| BittensorError::HotkeyNotRegistered {
        hotkey: hotkey.to_string(),
        netuid,
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn test_neuron_query_types() {
        let _uid: u16 = 0;
        let _netuid: u16 = 1;
    }
}
