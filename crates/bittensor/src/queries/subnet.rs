//! # Subnet Queries
//!
//! Query subnet information from the Bittensor network.

use crate::api::api;
use crate::error::BittensorError;
use subxt::OnlineClient;
use subxt::PolkadotConfig;

/// Subnet information
#[derive(Debug, Clone)]
pub struct SubnetInfo {
    /// Subnet netuid
    pub netuid: u16,
    /// Subnet tempo (blocks per epoch)
    pub tempo: u16,
    /// Number of neurons
    pub n: u16,
    /// Maximum number of neurons
    pub max_n: u16,
    /// Immunity period
    pub immunity_period: u16,
    /// Registration allowed
    pub registration_allowed: bool,
}

/// Subnet hyperparameters
#[derive(Debug, Clone)]
pub struct SubnetHyperparameters {
    /// Blocks per epoch
    pub tempo: u16,
    /// Maximum neurons
    pub max_n: u16,
    /// Minimum allowed weights
    pub min_allowed_weights: u16,
    /// Maximum allowed weights
    pub max_allowed_weights: u16,
    /// Weights version key
    pub weights_version_key: u64,
    /// Weights rate limit
    pub weights_rate_limit: u64,
    /// Registration allowed
    pub registration_allowed: bool,
    /// Adjustment interval
    pub adjustment_interval: u16,
    /// Target registration per interval
    pub target_regs_per_interval: u16,
    /// Immunity period
    pub immunity_period: u16,
}

/// Get information about a subnet
pub async fn get_subnet_info(
    client: &OnlineClient<PolkadotConfig>,
    netuid: u16,
) -> Result<SubnetInfo, BittensorError> {
    let storage = client
        .storage()
        .at_latest()
        .await
        .map_err(|e| BittensorError::RpcError {
            message: format!("Failed to get storage: {}", e),
        })?;

    let tempo = storage
        .fetch(&api::storage().subtensor_module().tempo(netuid))
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "tempo".to_string(),
            message: e.to_string(),
        })?
        .ok_or(BittensorError::SubnetNotFound { netuid })?;

    let n = storage
        .fetch(&api::storage().subtensor_module().subnetwork_n(netuid))
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "n".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    let max_n = storage
        .fetch(&api::storage().subtensor_module().max_allowed_uids(netuid))
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "max_n".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    let immunity_period = storage
        .fetch(&api::storage().subtensor_module().immunity_period(netuid))
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "immunity_period".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    let registration_allowed = storage
        .fetch(
            &api::storage()
                .subtensor_module()
                .network_registration_allowed(netuid),
        )
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "registration_allowed".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(false);

    Ok(SubnetInfo {
        netuid,
        tempo,
        n,
        max_n,
        immunity_period,
        registration_allowed,
    })
}

/// Get hyperparameters for a subnet
pub async fn get_subnet_hyperparameters(
    client: &OnlineClient<PolkadotConfig>,
    netuid: u16,
) -> Result<SubnetHyperparameters, BittensorError> {
    let storage = client
        .storage()
        .at_latest()
        .await
        .map_err(|e| BittensorError::RpcError {
            message: format!("Failed to get storage: {}", e),
        })?;

    let tempo = storage
        .fetch(&api::storage().subtensor_module().tempo(netuid))
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "tempo".to_string(),
            message: e.to_string(),
        })?
        .ok_or(BittensorError::SubnetNotFound { netuid })?;

    let max_n = storage
        .fetch(&api::storage().subtensor_module().max_allowed_uids(netuid))
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "max_n".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    let min_allowed_weights = storage
        .fetch(
            &api::storage()
                .subtensor_module()
                .min_allowed_weights(netuid),
        )
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "min_allowed_weights".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    let max_allowed_weights = storage
        .fetch(&api::storage().subtensor_module().max_weights_limit(netuid))
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "max_allowed_weights".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    let weights_version_key = storage
        .fetch(
            &api::storage()
                .subtensor_module()
                .weights_version_key(netuid),
        )
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "weights_version_key".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    let weights_rate_limit = storage
        .fetch(
            &api::storage()
                .subtensor_module()
                .weights_set_rate_limit(netuid),
        )
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "weights_rate_limit".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    let registration_allowed = storage
        .fetch(
            &api::storage()
                .subtensor_module()
                .network_registration_allowed(netuid),
        )
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "registration_allowed".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(false);

    let adjustment_interval = storage
        .fetch(
            &api::storage()
                .subtensor_module()
                .adjustment_interval(netuid),
        )
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "adjustment_interval".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    let target_regs_per_interval = storage
        .fetch(
            &api::storage()
                .subtensor_module()
                .target_registrations_per_interval(netuid),
        )
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "target_regs_per_interval".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    let immunity_period = storage
        .fetch(&api::storage().subtensor_module().immunity_period(netuid))
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "immunity_period".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    Ok(SubnetHyperparameters {
        tempo,
        max_n,
        min_allowed_weights,
        max_allowed_weights,
        weights_version_key,
        weights_rate_limit,
        registration_allowed,
        adjustment_interval,
        target_regs_per_interval,
        immunity_period,
    })
}

/// Get the number of active subnets
pub async fn get_total_subnets(
    client: &OnlineClient<PolkadotConfig>,
) -> Result<u16, BittensorError> {
    let storage = client
        .storage()
        .at_latest()
        .await
        .map_err(|e| BittensorError::RpcError {
            message: format!("Failed to get storage: {}", e),
        })?;

    let count = storage
        .fetch(&api::storage().subtensor_module().total_networks())
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "total_networks".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(0);

    Ok(count)
}

/// Check if a subnet exists
pub async fn subnet_exists(
    client: &OnlineClient<PolkadotConfig>,
    netuid: u16,
) -> Result<bool, BittensorError> {
    let storage = client
        .storage()
        .at_latest()
        .await
        .map_err(|e| BittensorError::RpcError {
            message: format!("Failed to get storage: {}", e),
        })?;

    let exists = storage
        .fetch(&api::storage().subtensor_module().networks_added(netuid))
        .await
        .map_err(|e| BittensorError::StorageQueryError {
            key: "networks_added".to_string(),
            message: e.to_string(),
        })?
        .unwrap_or(false);

    Ok(exists)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_subnet_info_struct() {
        let info = SubnetInfo {
            netuid: 1,
            tempo: 360,
            n: 256,
            max_n: 4096,
            immunity_period: 100,
            registration_allowed: true,
        };

        assert_eq!(info.netuid, 1);
        assert_eq!(info.tempo, 360);
    }

    #[test]
    fn test_hyperparameters_struct() {
        let params = SubnetHyperparameters {
            tempo: 360,
            max_n: 4096,
            min_allowed_weights: 0,
            max_allowed_weights: 65535,
            weights_version_key: 0,
            weights_rate_limit: 100,
            registration_allowed: true,
            adjustment_interval: 112,
            target_regs_per_interval: 2,
            immunity_period: 100,
        };

        assert_eq!(params.tempo, 360);
        assert!(params.registration_allowed);
    }
}
