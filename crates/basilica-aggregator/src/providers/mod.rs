use crate::error::Result;
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};

pub mod datacrunch;
pub mod http_utils;
pub mod hydrahost;
pub mod hyperstack;
pub mod lambda;

// ============================================================================
// Provider Constants
// ============================================================================

/// API base URLs for each provider
pub const DATACRUNCH_API_BASE_URL: &str = "https://api.datacrunch.io/v1";
pub const HYPERSTACK_API_BASE_URL: &str = "https://infrahub-api.nexgencloud.com/v1";
pub const LAMBDA_API_BASE_URL: &str = "https://cloud.lambda.ai/api/v1";
pub const HYDRAHOST_API_BASE_URL: &str = "https://api.brokkr.hydrahost.com/api/v0.1.0";

/// Default timeout for HTTP requests to provider APIs (in seconds)
pub const DEFAULT_TIMEOUT_SECONDS: u64 = 10;

/// Default cooldown between fetches from the same provider (in seconds)
pub const DEFAULT_COOLDOWN_SECONDS: u64 = 30;

// ============================================================================
// Unified Provider Types
// ============================================================================

/// Unified SSH key representation across providers
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderSshKey {
    /// Provider's internal SSH key ID (String for DataCrunch, u32 for Hyperstack)
    pub id: String,
    /// Name of the SSH key
    pub name: String,
    /// Public key content
    pub public_key: String,
    /// Optional fingerprint (Hyperstack provides this)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fingerprint: Option<String>,
}

impl From<datacrunch::SshKey> for ProviderSshKey {
    fn from(key: datacrunch::SshKey) -> Self {
        Self {
            id: key.id,
            name: key.name,
            public_key: key.public_key,
            fingerprint: None,
        }
    }
}

impl From<hyperstack::Keypair> for ProviderSshKey {
    fn from(key: hyperstack::Keypair) -> Self {
        Self {
            id: key.id.to_string(),
            name: key.name,
            public_key: key.public_key,
            fingerprint: Some(key.fingerprint),
        }
    }
}

/// Unified deployment request
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeployRequest {
    /// Instance/flavor identifier (instance_type for DataCrunch, flavor_name for Hyperstack)
    pub instance_type: String,
    /// Hostname for the deployment
    pub hostname: String,
    /// SSH key name
    pub ssh_key_name: String,
    /// SSH public key content
    pub ssh_public_key: String,
    /// Optional location/region code
    #[serde(skip_serializing_if = "Option::is_none")]
    pub location_code: Option<String>,
    /// Optional image name (defaults to Ubuntu 22.04 with CUDA if not specified)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub image_name: Option<String>,
    /// Optional Hyperstack-specific environment name
    #[serde(skip_serializing_if = "Option::is_none")]
    pub environment_name: Option<String>,
}

/// Unified deployment result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderDeployment {
    /// Provider's internal instance ID
    pub id: String,
    /// Deployment status
    pub status: String,
    /// Optional IP address (available when running)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ip_address: Option<String>,
    /// Hostname
    pub hostname: String,
    /// Instance type/flavor
    pub instance_type: String,
    /// SSH key ID used
    pub ssh_key_id: String,
    /// Raw provider-specific data
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_data: Option<serde_json::Value>,
}

impl From<datacrunch::Instance> for ProviderDeployment {
    fn from(instance: datacrunch::Instance) -> Self {
        Self {
            id: instance.id.clone(),
            status: format!("{:?}", instance.status),
            ip_address: instance.ip.clone(),
            hostname: instance.hostname.clone(),
            instance_type: instance.instance_type.clone(),
            ssh_key_id: instance
                .ssh_key_ids
                .as_ref()
                .and_then(|keys| keys.first().cloned())
                .unwrap_or_default(),
            raw_data: serde_json::to_value(&instance).ok(),
        }
    }
}

impl From<hyperstack::VirtualMachine> for ProviderDeployment {
    fn from(vm: hyperstack::VirtualMachine) -> Self {
        Self {
            id: vm.id.to_string(),
            status: vm.status.clone(),
            ip_address: vm.floating_ip.clone().or(vm.fixed_ip.clone()),
            hostname: vm.name.clone(),
            instance_type: vm.flavor_name.clone(),
            ssh_key_id: vm.key_name.clone().unwrap_or_default(),
            raw_data: serde_json::to_value(&vm).ok(),
        }
    }
}

// ============================================================================
// Provider Trait
// ============================================================================

#[async_trait]
pub trait Provider: Send + Sync {
    /// Unique provider identifier
    fn provider_id(&self) -> ProviderEnum;

    /// Fetch GPU offerings from provider API
    async fn fetch_offerings(&self) -> Result<Vec<GpuOffering>>;

    /// Health check for provider API
    async fn health_check(&self) -> Result<ProviderHealth>;

    // ------------------------------------------------------------------------
    // Unified SSH Key Management
    // ------------------------------------------------------------------------

    /// Create SSH key with provider (unified interface)
    async fn create_ssh_key(&self, name: String, public_key: String) -> Result<ProviderSshKey>;

    /// List all SSH keys from provider
    async fn list_ssh_keys(&self) -> Result<Vec<ProviderSshKey>>;

    /// Delete SSH key from provider
    async fn delete_ssh_key(&self, provider_key_id: &str) -> Result<()>;

    // ------------------------------------------------------------------------
    // Unified Deployment Management
    // ------------------------------------------------------------------------

    /// Deploy a new instance/VM with unified request
    async fn deploy(&self, request: DeployRequest) -> Result<ProviderDeployment>;

    /// Get deployment status and details
    async fn get_deployment(&self, instance_id: &str) -> Result<ProviderDeployment>;

    /// Delete/terminate a deployment
    async fn delete_deployment(&self, instance_id: &str) -> Result<()>;
}

// ============================================================================
// ProviderClient Enum
// ============================================================================

/// Enum wrapper for concrete provider implementations
pub enum ProviderClient {
    DataCrunch(datacrunch::DataCrunchProvider),
    Hyperstack(hyperstack::HyperstackProvider),
}

impl ProviderClient {
    /// Get provider identifier
    pub fn provider_id(&self) -> ProviderEnum {
        match self {
            Self::DataCrunch(p) => p.provider_id(),
            Self::Hyperstack(p) => p.provider_id(),
        }
    }
}

#[async_trait]
impl Provider for ProviderClient {
    fn provider_id(&self) -> ProviderEnum {
        match self {
            Self::DataCrunch(p) => p.provider_id(),
            Self::Hyperstack(p) => p.provider_id(),
        }
    }

    async fn fetch_offerings(&self) -> Result<Vec<GpuOffering>> {
        match self {
            Self::DataCrunch(p) => p.fetch_offerings().await,
            Self::Hyperstack(p) => p.fetch_offerings().await,
        }
    }

    async fn health_check(&self) -> Result<ProviderHealth> {
        match self {
            Self::DataCrunch(p) => p.health_check().await,
            Self::Hyperstack(p) => p.health_check().await,
        }
    }

    async fn create_ssh_key(&self, name: String, public_key: String) -> Result<ProviderSshKey> {
        match self {
            Self::DataCrunch(p) => p.create_ssh_key(name, public_key).await,
            Self::Hyperstack(p) => p.create_ssh_key(name, public_key).await,
        }
    }

    async fn list_ssh_keys(&self) -> Result<Vec<ProviderSshKey>> {
        match self {
            Self::DataCrunch(p) => p.list_ssh_keys().await,
            Self::Hyperstack(p) => p.list_ssh_keys().await,
        }
    }

    async fn delete_ssh_key(&self, provider_key_id: &str) -> Result<()> {
        match self {
            Self::DataCrunch(p) => p.delete_ssh_key(provider_key_id).await,
            Self::Hyperstack(p) => p.delete_ssh_key(provider_key_id).await,
        }
    }

    async fn deploy(&self, request: DeployRequest) -> Result<ProviderDeployment> {
        match self {
            Self::DataCrunch(p) => p.deploy(request).await,
            Self::Hyperstack(p) => p.deploy(request).await,
        }
    }

    async fn get_deployment(&self, instance_id: &str) -> Result<ProviderDeployment> {
        match self {
            Self::DataCrunch(p) => p.get_deployment(instance_id).await,
            Self::Hyperstack(p) => p.get_deployment(instance_id).await,
        }
    }

    async fn delete_deployment(&self, instance_id: &str) -> Result<()> {
        match self {
            Self::DataCrunch(p) => p.delete_deployment(instance_id).await,
            Self::Hyperstack(p) => p.delete_deployment(instance_id).await,
        }
    }
}
