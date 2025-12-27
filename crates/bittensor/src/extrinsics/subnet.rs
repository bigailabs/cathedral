//! # Subnet Extrinsics
//!
//! Extrinsics for managing subnets on the Bittensor network:
//! - `register_network`: Register a new subnet
//! - `register_network_with_identity`: Register with identity info
//! - `set_subnet_identity`: Update subnet identity

use crate::api::api;
use crate::error::BittensorError;
use crate::extrinsics::ExtrinsicResponse;
use subxt::OnlineClient;
use subxt::PolkadotConfig;
use tracing::{debug, warn};

/// Subnet identity information
#[derive(Debug, Clone, Default)]
pub struct SubnetIdentity {
    /// Subnet name
    pub name: String,
    /// GitHub repository URL
    pub github_repo: String,
    /// Contact email
    pub contact: String,
    /// Subnet description
    pub description: String,
    /// Subnet URL
    pub url: String,
    /// Discord invite
    pub discord: String,
    /// Logo URL
    pub logo_url: String,
    /// Additional info
    pub additional: String,
}

impl SubnetIdentity {
    /// Create a new subnet identity with just a name
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::extrinsics::SubnetIdentity;
    ///
    /// let identity = SubnetIdentity::new("My Subnet");
    /// assert_eq!(identity.name, "My Subnet");
    /// ```
    pub fn new(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            ..Default::default()
        }
    }

    /// Set the GitHub repository URL
    pub fn with_github(mut self, repo: impl Into<String>) -> Self {
        self.github_repo = repo.into();
        self
    }

    /// Set the contact email
    pub fn with_contact(mut self, contact: impl Into<String>) -> Self {
        self.contact = contact.into();
        self
    }

    /// Set the description
    pub fn with_description(mut self, desc: impl Into<String>) -> Self {
        self.description = desc.into();
        self
    }

    /// Set the URL
    pub fn with_url(mut self, url: impl Into<String>) -> Self {
        self.url = url.into();
        self
    }

    /// Set the Discord invite
    pub fn with_discord(mut self, discord: impl Into<String>) -> Self {
        self.discord = discord.into();
        self
    }

    /// Set the logo URL
    pub fn with_logo(mut self, logo: impl Into<String>) -> Self {
        self.logo_url = logo.into();
        self
    }
}

/// Register a new subnet on the network
///
/// This creates a new subnet by paying the registration cost.
/// The subnet netuid is returned on success by parsing the `NetworkAdded` event.
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The signer (coldkey)
///
/// # Returns
///
/// The newly registered subnet netuid extracted from the NetworkAdded event
///
/// # Errors
///
/// Returns an error if:
/// - Transaction submission fails
/// - Transaction is not finalized successfully
/// - NetworkAdded event is not found in the transaction events
pub async fn register_network<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
) -> Result<ExtrinsicResponse<u16>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let call = api::tx()
        .subtensor_module()
        .register_network(signer.account_id());

    debug!("Submitting register_network transaction");

    // Submit and watch the transaction to get events
    let tx_progress = client
        .tx()
        .sign_and_submit_then_watch_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit register_network: {}", e),
        })?;

    let tx_hash = tx_progress.extrinsic_hash();
    debug!("Transaction submitted with hash: {:?}", tx_hash);

    // Wait for finalization and get events
    let tx_events = tx_progress
        .wait_for_finalized_success()
        .await
        .map_err(|e| {
            warn!("Transaction finalization failed: {}", e);
            BittensorError::TxFinalizationError {
                reason: format!("register_network transaction failed: {}", e),
            }
        })?;

    debug!("Transaction finalized successfully");

    // Find the NetworkAdded event to extract the netuid
    // NetworkAdded event has format: NetworkAdded(netuid: u16, modality: u16)
    let network_added_event = tx_events
        .find_first::<api::subtensor_module::events::NetworkAdded>()
        .map_err(|e| {
            warn!("Failed to decode NetworkAdded event: {}", e);
            BittensorError::ChainError {
                message: format!("Failed to decode NetworkAdded event: {}", e),
            }
        })?;

    match network_added_event {
        Some(event) => {
            let netuid = event.0;
            debug!(
                "NetworkAdded event found: netuid={}, modality={}",
                netuid, event.1
            );
            Ok(ExtrinsicResponse::success()
                .with_message("Network registered successfully")
                .with_extrinsic_hash(&format!("{:?}", tx_hash))
                .with_data(netuid))
        }
        None => {
            warn!("NetworkAdded event not found in transaction events");
            // Log all events for debugging
            for event in tx_events.iter().flatten() {
                debug!(
                    "Event found: {}::{}",
                    event.pallet_name(),
                    event.variant_name()
                );
            }
            Err(BittensorError::ChainError {
                message: "NetworkAdded event not found - network may not have been registered"
                    .to_string(),
            })
        }
    }
}

/// Register a new subnet with identity information
///
/// This creates a new subnet with associated metadata.
/// The subnet netuid is returned on success by parsing the `NetworkAdded` event.
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The signer (coldkey)
/// * `identity` - Subnet identity information
///
/// # Returns
///
/// The newly registered subnet netuid extracted from the NetworkAdded event
///
/// # Errors
///
/// Returns an error if:
/// - Transaction submission fails
/// - Transaction is not finalized successfully
/// - NetworkAdded event is not found in the transaction events
pub async fn register_network_with_identity<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    identity: SubnetIdentity,
) -> Result<ExtrinsicResponse<u16>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    // Save name for logging before consuming identity
    let subnet_name = identity.name.clone();

    // Convert to API type
    let api_identity = api::runtime_types::pallet_subtensor::pallet::SubnetIdentityV3 {
        subnet_name: identity.name.into_bytes(),
        github_repo: identity.github_repo.into_bytes(),
        subnet_contact: identity.contact.into_bytes(),
        subnet_url: identity.url.into_bytes(),
        discord: identity.discord.into_bytes(),
        description: identity.description.into_bytes(),
        logo_url: identity.logo_url.into_bytes(),
        additional: identity.additional.into_bytes(),
    };

    let call = api::tx()
        .subtensor_module()
        .register_network_with_identity(signer.account_id(), Some(api_identity));

    debug!(
        "Submitting register_network_with_identity transaction for '{}'",
        subnet_name
    );

    // Submit and watch the transaction to get events
    let tx_progress = client
        .tx()
        .sign_and_submit_then_watch_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit register_network_with_identity: {}", e),
        })?;

    let tx_hash = tx_progress.extrinsic_hash();
    debug!("Transaction submitted with hash: {:?}", tx_hash);

    // Wait for finalization and get events
    let tx_events = tx_progress
        .wait_for_finalized_success()
        .await
        .map_err(|e| {
            warn!("Transaction finalization failed: {}", e);
            BittensorError::TxFinalizationError {
                reason: format!("register_network_with_identity transaction failed: {}", e),
            }
        })?;

    debug!("Transaction finalized successfully");

    // Find the NetworkAdded event to extract the netuid
    let network_added_event = tx_events
        .find_first::<api::subtensor_module::events::NetworkAdded>()
        .map_err(|e| {
            warn!("Failed to decode NetworkAdded event: {}", e);
            BittensorError::ChainError {
                message: format!("Failed to decode NetworkAdded event: {}", e),
            }
        })?;

    match network_added_event {
        Some(event) => {
            let netuid = event.0;
            debug!(
                "NetworkAdded event found: netuid={}, modality={}",
                netuid, event.1
            );
            Ok(ExtrinsicResponse::success()
                .with_message("Network registered with identity successfully")
                .with_extrinsic_hash(&format!("{:?}", tx_hash))
                .with_data(netuid))
        }
        None => {
            warn!("NetworkAdded event not found in transaction events");
            // Log all events for debugging
            for event in tx_events.iter().flatten() {
                debug!(
                    "Event found: {}::{}",
                    event.pallet_name(),
                    event.variant_name()
                );
            }
            Err(BittensorError::ChainError {
                message: "NetworkAdded event not found - network may not have been registered"
                    .to_string(),
            })
        }
    }
}

/// Set or update subnet identity information
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The signer (subnet owner coldkey)
/// * `netuid` - The subnet netuid
/// * `identity` - New identity information
pub async fn set_subnet_identity<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    netuid: u16,
    identity: SubnetIdentity,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let call = api::tx().subtensor_module().set_subnet_identity(
        netuid,
        identity.name.into_bytes(),
        identity.github_repo.into_bytes(),
        identity.contact.into_bytes(),
        identity.url.into_bytes(),
        identity.discord.into_bytes(),
        identity.description.into_bytes(),
        identity.logo_url.into_bytes(),
        identity.additional.into_bytes(),
    );

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to set subnet identity: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Subnet identity updated")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

/// Register in the root network (netuid 0)
///
/// This registers a hotkey in the root network for senate voting.
///
/// # Arguments
///
/// * `client` - The subxt client
/// * `signer` - The signer (coldkey)
/// * `hotkey` - The hotkey to register
pub async fn root_register<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    hotkey: crate::AccountId,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let call = api::tx().subtensor_module().root_register(hotkey);

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to root register: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Root registration successful")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_subnet_identity_new() {
        let identity = SubnetIdentity::new("Test Subnet");
        assert_eq!(identity.name, "Test Subnet");
        assert!(identity.github_repo.is_empty());
    }

    #[test]
    fn test_subnet_identity_builder() {
        let identity = SubnetIdentity::new("My Subnet")
            .with_github("https://github.com/example/subnet")
            .with_contact("admin@example.com")
            .with_description("A test subnet")
            .with_url("https://example.com")
            .with_discord("abc123")
            .with_logo("https://example.com/logo.png");

        assert_eq!(identity.name, "My Subnet");
        assert_eq!(identity.github_repo, "https://github.com/example/subnet");
        assert_eq!(identity.contact, "admin@example.com");
        assert_eq!(identity.description, "A test subnet");
        assert_eq!(identity.url, "https://example.com");
        assert_eq!(identity.discord, "abc123");
        assert_eq!(identity.logo_url, "https://example.com/logo.png");
    }

    #[test]
    fn test_subnet_identity_default() {
        let identity = SubnetIdentity::default();
        assert!(identity.name.is_empty());
        assert!(identity.github_repo.is_empty());
    }

    #[test]
    fn test_subnet_identity_clone() {
        let identity = SubnetIdentity::new("Test").with_github("https://github.com/test");
        let cloned = identity.clone();
        assert_eq!(identity.name, cloned.name);
        assert_eq!(identity.github_repo, cloned.github_repo);
    }

    #[test]
    fn test_subnet_identity_debug() {
        let identity = SubnetIdentity::new("Test");
        let debug = format!("{:?}", identity);
        assert!(debug.contains("SubnetIdentity"));
        assert!(debug.contains("Test"));
    }
}
