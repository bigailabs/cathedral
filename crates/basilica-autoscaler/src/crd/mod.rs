pub mod node_pool;
pub mod scaling_policy;

pub use node_pool::{
    HealthCheckConfig, K3sConfig, LifecycleConfig, NodeManagedBy, NodePasswordRef, NodePool,
    NodePoolCondition, NodePoolMode, NodePoolPhase, NodePoolSpec, NodePoolStatus, SecretRef,
    SecureCloudConfig, SshAuthType, SshConfig, WireGuardConfig, WireGuardPeerStatus,
    WireGuardStatus, FINALIZER,
};
pub use scaling_policy::{
    MetricsConfig, MetricsSnapshot, NodeTemplate, ScaleDownConfig, ScaleUpConfig, ScalingPolicy,
    ScalingPolicyCondition, ScalingPolicySpec, ScalingPolicyStatus, SecureCloudTemplate,
};
