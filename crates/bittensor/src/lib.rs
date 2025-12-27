//! # Bittensor Rust SDK
//!
//! A standalone Rust SDK for interacting with the Bittensor blockchain network.
//!
//! This crate provides:
//! - **Connection Management**: Connection pooling, health checks, and automatic failover
//! - **Wallet Management**: Key loading, signing, and transaction submission
//! - **Chain Queries**: Metagraph data, neuron info, and subnet information
//! - **Extrinsics**: Weight setting, axon serving, staking, and more
//!
//! # Quick Start
//!
//! ```rust,no_run
//! use bittensor::{config::BittensorConfig, Service};
//!
//! #[tokio::main]
//! async fn main() -> Result<(), Box<dyn std::error::Error>> {
//!     // Create configuration
//!     let config = BittensorConfig::finney("my_wallet", "my_hotkey", 1);
//!     
//!     // Initialize the service
//!     let service = Service::new(config).await?;
//!     
//!     // Get the metagraph
//!     let metagraph = service.get_metagraph(1).await?;
//!     println!("Found {} neurons", metagraph.hotkeys.len());
//!     
//!     Ok(())
//! }
//! ```
//!
//! # Modules
//!
//! - [`config`]: Configuration types for network and wallet settings
//! - [`types`]: Core types including `Hotkey`, `Balance`, and identity types
//! - [`connect`]: Connection pooling and health monitoring
//! - [`error`]: Error types and retry configuration
//! - [`service`]: Main `Service` struct for chain interactions
//! - [`discovery`]: Neuron discovery from metagraph data
//! - [`registration`]: Chain registration for miners and validators

// Core modules
pub mod api;
pub mod config;
pub mod connect;
pub mod discovery;
pub mod error;
pub mod extrinsics;
pub mod queries;
pub mod registration;
pub mod retry;
pub mod service;
pub mod types;
pub mod utils;
pub mod wallet;

#[cfg(test)]
mod error_tests;

// Re-export config types
pub use config::BittensorConfig;

// Re-export connection types
pub use connect::{
    ConnectionManager, ConnectionMetricsSnapshot, ConnectionPool, ConnectionPoolBuilder,
    ConnectionState, HealthCheckMetrics, HealthChecker,
};

// Re-export discovery
pub use discovery::NeuronDiscovery;

// Re-export error types
pub use error::{BittensorError, ErrorCategory, RetryConfig};

// Re-export registration
pub use registration::{
    ChainRegistration, RegistrationConfig, RegistrationConfigBuilder, RegistrationStateSnapshot,
};

// Re-export retry utilities
pub use retry::{retry_operation, retry_operation_with_timeout, CircuitBreaker, RetryNode};

// Re-export service
pub use service::{ConnectionPoolMetrics, Service};

// Re-export types
pub use types::{
    account_id_to_hotkey, hotkey_to_account_id, rao_to_tao, tao_to_rao, Balance, Hotkey, MinerUid,
    ValidatorUid,
};

// Re-export utility functions
pub use utils::{
    create_signature, normalize_weights, set_weights_payload, sign_message_hex, sign_with_keypair,
    verify_bittensor_signature, BittensorSignature, NormalizedWeight,
};

// Re-export wallet types
pub use wallet::{Wallet, WalletSigner};

// Re-export extrinsics
pub use extrinsics::{
    add_stake, burned_register, commit_weights, delegate_stake, register_network,
    register_network_with_identity, remove_stake, reveal_weights, revoke_children, root_register,
    serve_axon, serve_prometheus, set_childkey_take, set_children, set_root_weights,
    set_subnet_identity, set_weights, transfer, transfer_all, transfer_keep_alive,
    undelegate_stake, ChildKey, CommitRevealParams, ExtrinsicResponse, ExtrinsicStatus,
    RootWeightsParams, ServeAxonParams, ServePrometheusParams, SetChildrenParams, StakeParams,
    SubnetIdentity, TransferParams, WeightsParams,
};

// Re-export queries
pub use queries::{
    fields as metagraph_fields, get_balance, get_metagraph, get_neuron, get_neuron_lite, get_stake,
    get_stake_info_for_coldkey, get_subnet_hyperparameters, get_subnet_info,
    get_total_network_stake, get_total_subnets, get_uid_for_hotkey, subnet_exists, StakeInfo,
    SubnetHyperparameters, SubnetInfo,
};

// Re-export key types from our generated API
pub use crate::api::api::runtime_types::pallet_subtensor::{
    pallet::{AxonInfo, PrometheusInfo},
    rpc_info::{
        metagraph::{Metagraph, SelectiveMetagraph},
        neuron_info::{NeuronInfo, NeuronInfoLite},
    },
};

// Type alias for AccountId
pub type AccountId = subxt::config::polkadot::AccountId32;

/// Re-export sp_core for cryptographic operations
///
/// This allows dependents to use bittensor's bundled sp_core without
/// adding it as a direct dependency.
pub mod crypto {
    pub use subxt::ext::sp_core::crypto::{Pair, Ss58AddressFormat, Ss58Codec};
    pub use subxt::ext::sp_core::sr25519;
    pub use subxt::ext::sp_core::Pair as PairTrait;
}
