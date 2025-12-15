use chrono::{DateTime, Utc};
use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

/// Finalizer for NodePool cleanup
pub const FINALIZER: &str = "autoscaler.basilica.ai/node-cleanup";

/// NodePool represents a single GPU node in the cluster
#[derive(CustomResource, Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[kube(
    group = "autoscaler.basilica.ai",
    version = "v1alpha1",
    kind = "NodePool",
    namespaced
)]
#[kube(status = "NodePoolStatus")]
#[kube(printcolumn = r#"{"name":"Mode", "type":"string", "jsonPath":".spec.mode"}"#)]
#[kube(printcolumn = r#"{"name":"Phase", "type":"string", "jsonPath":".status.phase"}"#)]
#[kube(printcolumn = r#"{"name":"Node", "type":"string", "jsonPath":".status.nodeName"}"#)]
#[kube(printcolumn = r#"{"name":"Age", "type":"date", "jsonPath":".metadata.creationTimestamp"}"#)]
#[serde(rename_all = "camelCase")]
pub struct NodePoolSpec {
    /// Provisioning mode: Manual or Dynamic
    pub mode: NodePoolMode,

    /// SSH configuration (required for Manual mode)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ssh: Option<SshConfig>,

    /// Secure Cloud configuration (required for Dynamic mode)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub secure_cloud: Option<SecureCloudConfig>,

    /// K3s agent configuration
    pub k3s: K3sConfig,

    /// WireGuard configuration
    #[serde(default)]
    pub wireguard: WireGuardConfig,

    /// Health check configuration
    #[serde(default)]
    pub health_check: HealthCheckConfig,

    /// Lifecycle hooks
    #[serde(default)]
    pub lifecycle: LifecycleConfig,

    /// Unique node identifier (auto-generated if not provided)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub node_id: Option<String>,

    /// Datacenter/owner identifier
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub datacenter_id: Option<String>,

    /// Node password reference for K3s re-registration
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub node_password: Option<NodePasswordRef>,

    /// If true and node already exists, adopt it instead of failing
    #[serde(default)]
    pub adopt_existing: bool,
}

/// Provisioning mode for NodePool
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
pub enum NodePoolMode {
    Manual,
    Dynamic,
}

/// SSH configuration for Manual mode
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct SshConfig {
    /// IP address or hostname
    pub host: String,

    /// SSH port (default: 22)
    #[serde(default = "default_ssh_port")]
    pub port: u16,

    /// SSH username (default: root)
    #[serde(default = "default_ssh_user")]
    pub user: String,

    /// Reference to Secret containing SSH private key
    pub auth_secret_ref: SecretRef,

    /// Authentication type
    #[serde(default)]
    pub auth_type: SshAuthType,
}

fn default_ssh_port() -> u16 {
    22
}

fn default_ssh_user() -> String {
    "root".to_string()
}

/// SSH authentication type
#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
pub enum SshAuthType {
    #[default]
    PrivateKey,
    PrivateKeyPem,
}

/// Secure Cloud configuration for Dynamic mode
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct SecureCloudConfig {
    /// GPU offering ID from Basilica API
    pub offering_id: String,

    /// SSH key ID registered with provider
    pub ssh_key_id: String,

    /// Reference to Secret containing the corresponding private key
    pub ssh_key_secret_ref: SecretRef,
}

/// K3s agent configuration
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct K3sConfig {
    /// K3s version (e.g., v1.31.1+k3s1)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,

    /// K3s API server URL
    pub server_url: String,

    /// Reference to Secret containing K3s join token
    pub token_secret_ref: SecretRef,

    /// Additional K3s agent arguments
    #[serde(default)]
    pub extra_args: Vec<String>,

    /// Labels to apply to the node
    #[serde(default)]
    pub node_labels: std::collections::BTreeMap<String, String>,

    /// Taints to apply (format: key=value:effect)
    #[serde(default)]
    pub node_taints: Vec<String>,
}

/// WireGuard configuration
#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct WireGuardConfig {
    /// Enable WireGuard (default: true for remote nodes)
    #[serde(default = "default_wireguard_enabled")]
    pub enabled: bool,
}

fn default_wireguard_enabled() -> bool {
    true
}

/// Health check configuration
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct HealthCheckConfig {
    /// Enable health checks
    #[serde(default = "default_health_check_enabled")]
    pub enabled: bool,

    /// Check interval in seconds
    #[serde(default = "default_health_check_interval")]
    pub interval_seconds: u32,

    /// Timeout for each check in seconds
    #[serde(default = "default_health_check_timeout")]
    pub timeout_seconds: u32,

    /// Number of failures before marking unhealthy
    #[serde(default = "default_health_check_threshold")]
    pub failure_threshold: u32,
}

impl Default for HealthCheckConfig {
    fn default() -> Self {
        Self {
            enabled: default_health_check_enabled(),
            interval_seconds: default_health_check_interval(),
            timeout_seconds: default_health_check_timeout(),
            failure_threshold: default_health_check_threshold(),
        }
    }
}

fn default_health_check_enabled() -> bool {
    true
}
fn default_health_check_interval() -> u32 {
    30
}
fn default_health_check_timeout() -> u32 {
    10
}
fn default_health_check_threshold() -> u32 {
    5
}

/// Lifecycle hooks configuration
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct LifecycleConfig {
    /// Drain node before removal
    #[serde(default = "default_drain_on_removal")]
    pub drain_on_removal: bool,

    /// Drain timeout in seconds
    #[serde(default = "default_drain_timeout")]
    pub drain_timeout_seconds: u32,

    /// Delete grace period in seconds
    #[serde(default = "default_delete_grace_period")]
    pub delete_grace_period_seconds: u32,

    /// Force drain (delete pods after timeout)
    #[serde(default)]
    pub force_drain: bool,

    /// Script to run before K3s join (optional)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pre_join_script: Option<String>,

    /// Script to run after K3s join (optional)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub post_join_script: Option<String>,
}

impl Default for LifecycleConfig {
    fn default() -> Self {
        Self {
            drain_on_removal: default_drain_on_removal(),
            drain_timeout_seconds: default_drain_timeout(),
            delete_grace_period_seconds: default_delete_grace_period(),
            force_drain: false,
            pre_join_script: None,
            post_join_script: None,
        }
    }
}

fn default_drain_on_removal() -> bool {
    true
}
fn default_drain_timeout() -> u32 {
    600
}
fn default_delete_grace_period() -> u32 {
    30
}

/// Reference to a Kubernetes Secret
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct SecretRef {
    /// Secret name
    pub name: String,

    /// Key within secret (default varies by usage)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub key: Option<String>,

    /// Namespace of the secret (defaults to autoscaler namespace)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,
}

/// Node password reference
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct NodePasswordRef {
    pub secret_ref: SecretRef,
}

/// NodePool phase (lifecycle state)
#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
pub enum NodePoolPhase {
    #[default]
    Pending,
    Provisioning,
    Configuring,
    InstallingWireGuard,
    ValidatingNetwork,
    JoiningCluster,
    WaitingForNode,
    Ready,
    Unhealthy,
    Draining,
    Terminating,
    Failed,
    Deleted,
}

impl std::fmt::Display for NodePoolPhase {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Pending => write!(f, "Pending"),
            Self::Provisioning => write!(f, "Provisioning"),
            Self::Configuring => write!(f, "Configuring"),
            Self::InstallingWireGuard => write!(f, "InstallingWireGuard"),
            Self::ValidatingNetwork => write!(f, "ValidatingNetwork"),
            Self::JoiningCluster => write!(f, "JoiningCluster"),
            Self::WaitingForNode => write!(f, "WaitingForNode"),
            Self::Ready => write!(f, "Ready"),
            Self::Unhealthy => write!(f, "Unhealthy"),
            Self::Draining => write!(f, "Draining"),
            Self::Terminating => write!(f, "Terminating"),
            Self::Failed => write!(f, "Failed"),
            Self::Deleted => write!(f, "Deleted"),
        }
    }
}

/// Manager type for node
#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
pub enum NodeManagedBy {
    #[default]
    Autoscaler,
    OnboardScript,
}

/// NodePool status
#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct NodePoolStatus {
    /// Current phase
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<NodePoolPhase>,

    /// When current phase was entered
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase_entered_at: Option<DateTime<Utc>>,

    /// Kubernetes conditions
    #[serde(default)]
    pub conditions: Vec<NodePoolCondition>,

    /// Generated node ID (persisted for idempotency)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub node_id: Option<String>,

    /// WireGuard status
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wireguard: Option<WireGuardStatus>,

    /// K3s node name
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub node_name: Option<String>,

    /// K8s Node UID
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub node_uid: Option<String>,

    /// WireGuard IP (internal)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub internal_ip: Option<String>,

    /// Public IP (for SSH)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub external_ip: Option<String>,

    /// Secure cloud rental ID (dynamic mode)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rental_id: Option<String>,

    /// Provider instance ID
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_id: Option<String>,

    /// Provider name
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,

    /// GPU offering ID (for dynamic mode)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub offering_id: Option<String>,

    /// GPU model
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gpu_model: Option<String>,

    /// GPU count
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gpu_count: Option<u32>,

    /// GPU memory in GB
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gpu_memory_gb: Option<u32>,

    /// CUDA version
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cuda_version: Option<String>,

    /// Driver version
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub driver_version: Option<String>,

    /// When VM was leased (dynamic mode)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provisioned_at: Option<DateTime<Utc>>,

    /// When node joined K3s cluster
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub joined_at: Option<DateTime<Utc>>,

    /// Last health check timestamp
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_health_check_at: Option<DateTime<Utc>>,

    /// Last error message
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,

    /// Failure count
    #[serde(default)]
    pub failure_count: u32,

    /// When to retry after failure
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_after: Option<DateTime<Utc>>,

    /// Cleanup in progress flag
    #[serde(default)]
    pub cleanup_in_progress: bool,

    /// Node identity verified
    #[serde(default)]
    pub identity_verified: bool,

    /// Identity check details
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub identity_check_details: Option<String>,

    /// Management metadata
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub managed_by: Option<NodeManagedBy>,

    /// Observed generation
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub observed_generation: Option<i64>,

    /// Whether GPU labels have been applied to the K8s node.
    /// Used to track label application state and avoid race conditions.
    #[serde(default)]
    pub labels_applied: bool,
}

/// NodePool condition
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct NodePoolCondition {
    /// Condition type
    #[serde(rename = "type")]
    pub type_: String,

    /// Status: True, False, Unknown
    pub status: String,

    /// Machine-readable reason
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,

    /// Human-readable message
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,

    /// Last transition time
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_transition_time: Option<DateTime<Utc>>,

    /// Last probe time
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_probe_time: Option<DateTime<Utc>>,
}

/// WireGuard status
#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct WireGuardStatus {
    /// WireGuard IP assigned by API
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub node_ip: Option<String>,

    /// Node's WireGuard public key
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub public_key: Option<String>,

    /// Public endpoint
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub endpoint: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use kube::core::CustomResourceExt;

    #[test]
    fn crd_metadata_is_correct() {
        let crd = NodePool::crd();
        let name = crd.metadata.name.as_ref().unwrap();
        assert_eq!(name, "nodepools.autoscaler.basilica.ai");
        assert_eq!(crd.spec.group, "autoscaler.basilica.ai");
        assert_eq!(crd.spec.names.kind, "NodePool");
        assert_eq!(crd.spec.scope, "Namespaced");
    }

    #[test]
    fn spec_has_required_fields() {
        let crd = NodePool::crd();
        let schema = &crd.spec.versions[0]
            .schema
            .as_ref()
            .unwrap()
            .open_api_v3_schema
            .as_ref()
            .unwrap();
        let spec_props = schema
            .properties
            .as_ref()
            .unwrap()
            .get("spec")
            .and_then(|s| s.properties.as_ref())
            .unwrap();
        assert!(spec_props.contains_key("mode"));
        assert!(spec_props.contains_key("k3s"));
    }

    #[test]
    fn phase_display_is_correct() {
        assert_eq!(NodePoolPhase::Pending.to_string(), "Pending");
        assert_eq!(NodePoolPhase::Ready.to_string(), "Ready");
        assert_eq!(
            NodePoolPhase::InstallingWireGuard.to_string(),
            "InstallingWireGuard"
        );
    }
}
