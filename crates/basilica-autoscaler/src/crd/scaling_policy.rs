use chrono::{DateTime, Utc};
use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use super::node_pool::{K3sConfig, LifecycleConfig, SecretRef};
use crate::offering_matcher::OfferingConstraints;

/// ScalingPolicy defines the autoscaling behavior for the cluster
#[derive(CustomResource, Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[kube(
    group = "autoscaler.basilica.ai",
    version = "v1alpha1",
    kind = "ScalingPolicy",
    namespaced
)]
#[kube(status = "ScalingPolicyStatus")]
#[kube(printcolumn = r#"{"name":"Enabled", "type":"boolean", "jsonPath":".spec.enabled"}"#)]
#[kube(printcolumn = r#"{"name":"Min", "type":"integer", "jsonPath":".spec.minNodes"}"#)]
#[kube(printcolumn = r#"{"name":"Max", "type":"integer", "jsonPath":".spec.maxNodes"}"#)]
#[kube(printcolumn = r#"{"name":"Current", "type":"integer", "jsonPath":".status.currentNodes"}"#)]
#[kube(printcolumn = r#"{"name":"Age", "type":"date", "jsonPath":".metadata.creationTimestamp"}"#)]
#[serde(rename_all = "camelCase")]
pub struct ScalingPolicySpec {
    /// Enable/disable autoscaling
    #[serde(default = "default_enabled")]
    pub enabled: bool,

    /// Minimum number of GPU nodes
    #[serde(default)]
    pub min_nodes: u32,

    /// Maximum number of GPU nodes
    #[serde(default = "default_max_nodes")]
    pub max_nodes: u32,

    /// Scale-up configuration
    #[serde(default)]
    pub scale_up: ScaleUpConfig,

    /// Scale-down configuration
    #[serde(default)]
    pub scale_down: ScaleDownConfig,

    /// Node template for dynamic provisioning
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub node_template: Option<NodeTemplate>,

    /// Constraints for dynamic offering selection
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub offering_constraints: Option<OfferingConstraints>,

    /// Metrics collection configuration
    #[serde(default)]
    pub metrics: MetricsConfig,
}

fn default_enabled() -> bool {
    true
}

fn default_max_nodes() -> u32 {
    10
}

/// Scale-up configuration
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ScaleUpConfig {
    /// Number of pending GPU pods to trigger scale-up
    #[serde(default = "default_pending_threshold")]
    pub pending_pod_threshold: u32,

    /// Cooldown after scale-up in seconds
    #[serde(default = "default_cooldown")]
    pub cooldown_seconds: u32,

    /// Number of nodes to add per scale-up event
    #[serde(default = "default_increment")]
    pub increment: u32,
}

impl Default for ScaleUpConfig {
    fn default() -> Self {
        Self {
            pending_pod_threshold: default_pending_threshold(),
            cooldown_seconds: default_cooldown(),
            increment: default_increment(),
        }
    }
}

fn default_pending_threshold() -> u32 {
    1
}
fn default_cooldown() -> u32 {
    300
}
fn default_increment() -> u32 {
    1
}

/// Scale-down configuration
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ScaleDownConfig {
    /// Node must be idle for this duration (seconds)
    #[serde(default = "default_idle_timeout")]
    pub idle_timeout_seconds: u32,

    /// Scale down if GPU utilization below this percentage
    #[serde(default = "default_gpu_threshold")]
    pub gpu_utilization_threshold: f32,

    /// Node has zero pods for this duration (seconds)
    #[serde(default = "default_zero_pods_duration")]
    pub zero_pods_duration_seconds: u32,

    /// Cooldown after scale-down in seconds
    #[serde(default = "default_cooldown")]
    pub cooldown_seconds: u32,

    /// Number of nodes to remove per scale-down event
    #[serde(default = "default_increment")]
    pub decrement: u32,
}

impl Default for ScaleDownConfig {
    fn default() -> Self {
        Self {
            idle_timeout_seconds: default_idle_timeout(),
            gpu_utilization_threshold: default_gpu_threshold(),
            zero_pods_duration_seconds: default_zero_pods_duration(),
            cooldown_seconds: default_cooldown(),
            decrement: default_increment(),
        }
    }
}

fn default_idle_timeout() -> u32 {
    600
}
fn default_gpu_threshold() -> f32 {
    10.0
}
fn default_zero_pods_duration() -> u32 {
    300
}

/// Node template for dynamic provisioning
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct NodeTemplate {
    /// Secure Cloud configuration
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub secure_cloud: Option<SecureCloudTemplate>,

    /// K3s configuration
    pub k3s: K3sConfig,

    /// Lifecycle configuration
    #[serde(default)]
    pub lifecycle: LifecycleConfig,
}

/// Secure Cloud template for dynamic provisioning
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct SecureCloudTemplate {
    /// GPU offering ID from Basilica API.
    /// If not specified, the autoscaler will dynamically select an offering
    /// based on pending pod GPU requirements.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub offering_id: Option<String>,

    /// Preferred GPU type (e.g., RTX_4090, A100) for dynamic selection
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gpu_type: Option<String>,

    /// Minimum GPUs per node for dynamic selection
    #[serde(default = "default_min_gpu_count")]
    pub min_gpu_count: u32,

    /// Maximum acceptable hourly rate for dynamic selection
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_hourly_rate: Option<f64>,

    /// Preferred provider for dynamic selection
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preferred_provider: Option<String>,

    /// Preferred region for dynamic selection
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub region: Option<String>,

    /// SSH key ID for provisioning
    pub ssh_key_id: String,

    /// Reference to Secret containing SSH private key
    pub ssh_key_secret_ref: SecretRef,
}

fn default_min_gpu_count() -> u32 {
    1
}

/// Metrics collection configuration
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct MetricsConfig {
    /// Collection interval in seconds
    #[serde(default = "default_collection_interval")]
    pub collection_interval_seconds: u32,

    /// Evaluation interval in seconds
    #[serde(default = "default_evaluation_interval")]
    pub evaluation_interval_seconds: u32,
}

impl Default for MetricsConfig {
    fn default() -> Self {
        Self {
            collection_interval_seconds: default_collection_interval(),
            evaluation_interval_seconds: default_evaluation_interval(),
        }
    }
}

fn default_collection_interval() -> u32 {
    30
}
fn default_evaluation_interval() -> u32 {
    60
}

/// ScalingPolicy status
#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ScalingPolicyStatus {
    /// Number of active NodePool CRs
    #[serde(default)]
    pub current_nodes: u32,

    /// Target number based on demand
    #[serde(default)]
    pub desired_nodes: u32,

    /// Number of nodes being provisioned
    #[serde(default)]
    pub pending_scale_up: u32,

    /// Number of nodes being drained
    #[serde(default)]
    pub pending_scale_down: u32,

    /// Last scale-up timestamp
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_scale_up_time: Option<DateTime<Utc>>,

    /// Last scale-down timestamp
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_scale_down_time: Option<DateTime<Utc>>,

    /// Last evaluation timestamp
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_evaluation_time: Option<DateTime<Utc>>,

    /// Current metrics snapshot
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metrics: Option<MetricsSnapshot>,

    /// Kubernetes conditions
    #[serde(default)]
    pub conditions: Vec<ScalingPolicyCondition>,

    /// Observed generation
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub observed_generation: Option<i64>,
}

/// Current metrics snapshot
#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct MetricsSnapshot {
    /// Number of pending GPU pods
    #[serde(default)]
    pub pending_gpu_pods: u32,

    /// Total GPU nodes in cluster
    #[serde(default)]
    pub total_gpu_nodes: u32,

    /// Healthy GPU nodes
    #[serde(default)]
    pub healthy_gpu_nodes: u32,

    /// Average GPU utilization percentage (None when metrics unavailable)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub average_gpu_utilization: Option<f32>,

    /// Number of idle nodes
    #[serde(default)]
    pub idle_nodes: u32,
}

/// ScalingPolicy condition
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ScalingPolicyCondition {
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use kube::core::CustomResourceExt;

    #[test]
    fn crd_metadata_is_correct() {
        let crd = ScalingPolicy::crd();
        let name = crd.metadata.name.as_ref().unwrap();
        assert_eq!(name, "scalingpolicies.autoscaler.basilica.ai");
        assert_eq!(crd.spec.group, "autoscaler.basilica.ai");
        assert_eq!(crd.spec.names.kind, "ScalingPolicy");
        assert_eq!(crd.spec.scope, "Namespaced");
    }

    #[test]
    fn spec_has_required_fields() {
        let crd = ScalingPolicy::crd();
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
        assert!(spec_props.contains_key("enabled"));
        assert!(spec_props.contains_key("scaleUp"));
        assert!(spec_props.contains_key("scaleDown"));
    }

    #[test]
    fn defaults_are_sensible() {
        let scale_up = ScaleUpConfig::default();
        assert_eq!(scale_up.pending_pod_threshold, 1);
        assert_eq!(scale_up.cooldown_seconds, 300);

        let scale_down = ScaleDownConfig::default();
        assert_eq!(scale_down.idle_timeout_seconds, 600);
        assert!((scale_down.gpu_utilization_threshold - 10.0).abs() < 0.01);
    }
}
