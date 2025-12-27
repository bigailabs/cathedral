//! # Metagraph Queries
//!
//! Query metagraph data from the Bittensor network.
//!
//! For full metagraph queries, use the `Service::get_metagraph` method.
//! This module provides the underlying implementation.

use crate::api::api;
use crate::error::BittensorError;
use crate::AccountId;
use std::time::Duration;
use subxt::OnlineClient;
use subxt::PolkadotConfig;

/// Metagraph type with AccountId
pub type Metagraph =
    crate::api::api::runtime_types::pallet_subtensor::rpc_info::metagraph::Metagraph<AccountId>;

/// SelectiveMetagraph type with AccountId
pub type SelectiveMetagraph =
    crate::api::api::runtime_types::pallet_subtensor::rpc_info::metagraph::SelectiveMetagraph<
        AccountId,
    >;

/// Get the full metagraph for a subnet
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `netuid` - The subnet netuid
///
/// # Returns
///
/// The complete metagraph data including all neuron information
///
/// # Example
///
/// ```rust,no_run
/// use bittensor::queries::get_metagraph;
///
/// # async fn example() -> Result<(), Box<dyn std::error::Error>> {
/// # let client: subxt::OnlineClient<subxt::PolkadotConfig> = todo!();
/// let metagraph = get_metagraph(&client, 1).await?;
/// println!("Found {} neurons", metagraph.hotkeys.len());
/// # Ok(())
/// # }
/// ```
pub async fn get_metagraph(
    client: &OnlineClient<PolkadotConfig>,
    netuid: u16,
) -> Result<Metagraph, BittensorError> {
    let runtime_api =
        client
            .runtime_api()
            .at_latest()
            .await
            .map_err(|e| BittensorError::RpcError {
                message: format!("Failed to get runtime API: {}", e),
            })?;

    let metagraph = runtime_api
        .call(
            api::runtime_apis::subnet_info_runtime_api::SubnetInfoRuntimeApi.get_metagraph(netuid),
        )
        .await
        .map_err(|e| {
            let err_msg = e.to_string();
            if err_msg.to_lowercase().contains("timeout") {
                BittensorError::RpcTimeoutError {
                    message: format!("get_metagraph call timeout: {err_msg}"),
                    timeout: Duration::from_secs(30),
                }
            } else {
                BittensorError::RpcMethodError {
                    method: "get_metagraph".to_string(),
                    message: err_msg,
                }
            }
        })?
        .ok_or(BittensorError::SubnetNotFound { netuid })?;

    Ok(metagraph)
}

/// Field bitmask constants for selective metagraph queries
pub mod fields {
    /// Include hotkeys
    pub const HOTKEYS: u64 = 1 << 0;
    /// Include coldkeys
    pub const COLDKEYS: u64 = 1 << 1;
    /// Include stake
    pub const STAKE: u64 = 1 << 2;
    /// Include trust
    pub const TRUST: u64 = 1 << 3;
    /// Include consensus
    pub const CONSENSUS: u64 = 1 << 4;
    /// Include incentive
    pub const INCENTIVE: u64 = 1 << 5;
    /// Include dividends
    pub const DIVIDENDS: u64 = 1 << 6;
    /// Include emission
    pub const EMISSION: u64 = 1 << 7;
    /// Include axon info
    pub const AXONS: u64 = 1 << 8;
    /// Include all fields
    pub const ALL: u64 = u64::MAX;
}

#[cfg(test)]
mod tests {
    use super::fields::*;

    #[test]
    fn test_field_constants() {
        assert_eq!(HOTKEYS, 1);
        assert_eq!(COLDKEYS, 2);
        assert_eq!(STAKE, 4);
        assert_ne!(HOTKEYS | COLDKEYS, STAKE);
    }
}
