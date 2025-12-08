use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(CustomResource, Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[kube(
    group = "basilica.ai",
    version = "v1",
    kind = "UserDeployment",
    namespaced
)]
#[kube(status = "UserDeploymentStatus")]
#[kube(printcolumn = r#"{"name":"State", "type":"string", "jsonPath":".status.state"}"#)]
#[kube(printcolumn = r#"{"name":"Phase", "type":"string", "jsonPath":".status.phase"}"#)]
#[kube(printcolumn = r#"{"name":"Replicas", "type":"string", "jsonPath":".status.replicasReady"}"#)]
#[kube(printcolumn = r#"{"name":"Age", "type":"date", "jsonPath":".metadata.creationTimestamp"}"#)]
#[serde(rename_all = "camelCase")]
pub struct UserDeploymentSpec {
    pub user_id: String,
    pub instance_name: String,
    pub image: String,
    pub replicas: u32,
    pub port: u32,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: Vec<EnvVar>,
    #[serde(default)]
    pub resources: Option<ResourceRequirements>,
    pub path_prefix: String,
    #[serde(default)]
    pub ttl_seconds: Option<u32>,
    #[serde(default)]
    pub health_check: Option<HealthCheckConfig>,
    #[serde(default)]
    pub storage: Option<StorageSpec>,
    #[serde(default = "default_enable_billing")]
    pub enable_billing: bool,
    #[serde(default)]
    #[schemars(length(max = 255))]
    pub queue_name: Option<String>,
    #[serde(default)]
    pub suspended: bool,
    #[serde(default)]
    #[schemars(length(max = 50))]
    pub priority: Option<String>,
    #[serde(default)]
    pub public: bool,
    #[serde(default)]
    pub topology_spread: Option<TopologySpreadConfig>,
}

fn default_enable_billing() -> bool {
    true
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct EnvVar {
    pub name: String,
    pub value: String,
}

fn default_cpu_request_ratio() -> f32 {
    1.0
}

fn default_max_skew() -> i32 {
    1
}

fn default_when_unsatisfiable() -> String {
    "ScheduleAnyway".to_string()
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct TopologySpreadConfig {
    #[serde(default = "default_max_skew")]
    #[schemars(range(min = 1, max = 10))]
    pub max_skew: i32,
    #[serde(default = "default_when_unsatisfiable")]
    pub when_unsatisfiable: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ResourceRequirements {
    pub cpu: String,
    pub memory: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub gpus: Option<GpuSpec>,
    #[serde(default = "default_cpu_request_ratio")]
    #[schemars(range(min = 0.5, max = 1.0))]
    pub cpu_request_ratio: f32,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, PartialEq, Eq, Hash)]
#[serde(rename_all = "camelCase")]
pub struct GpuSpec {
    #[schemars(range(min = 1, max = 8))]
    pub count: u32,
    #[schemars(length(min = 1, max = 10))]
    pub model: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[schemars(regex(pattern = r"^\d+\.\d+$"))]
    pub min_cuda_version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[schemars(range(min = 1, max = 256))]
    pub min_gpu_memory_gb: Option<u32>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct StorageSpec {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ephemeral: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub persistent: Option<PersistentStorageSpec>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum StorageBackend {
    R2,
    S3,
    GCS,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct PersistentStorageSpec {
    pub enabled: bool,
    pub backend: StorageBackend,
    pub bucket: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub region: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub endpoint: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub credentials_secret: Option<String>,
    #[schemars(range(min = 100, max = 60000))]
    #[serde(default = "default_sync_interval")]
    pub sync_interval_ms: u64,
    #[schemars(range(min = 512, max = 16384))]
    #[serde(default = "default_cache_size")]
    pub cache_size_mb: usize,
    #[serde(default = "default_mount_path")]
    pub mount_path: String,
}

fn default_sync_interval() -> u64 {
    1000
}

fn default_cache_size() -> usize {
    2048
}

fn default_mount_path() -> String {
    "/data".to_string()
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct HealthCheckConfig {
    #[serde(default)]
    pub liveness: Option<ProbeConfig>,
    #[serde(default)]
    pub readiness: Option<ProbeConfig>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ProbeConfig {
    pub path: String,
    #[serde(default = "default_initial_delay")]
    pub initial_delay_seconds: u32,
    #[serde(default = "default_period")]
    pub period_seconds: u32,
    #[serde(default = "default_timeout")]
    pub timeout_seconds: u32,
    #[serde(default = "default_failure_threshold")]
    pub failure_threshold: u32,
}

fn default_initial_delay() -> u32 {
    30
}

fn default_period() -> u32 {
    10
}

fn default_timeout() -> u32 {
    5
}

fn default_failure_threshold() -> u32 {
    3
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, Default)]
#[serde(rename_all = "camelCase")]
pub struct UserDeploymentStatus {
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub deployment_name: String,
    #[serde(default)]
    pub service_name: String,
    #[serde(default)]
    pub replicas_ready: u32,
    #[serde(default)]
    pub replicas_desired: u32,
    #[serde(default)]
    pub endpoint: String,
    #[serde(default)]
    pub public_url: String,
    #[serde(default)]
    pub message: Option<String>,
    #[serde(default)]
    pub start_time: Option<String>,
    #[serde(default)]
    pub last_updated: String,
    #[serde(default)]
    pub suspended: bool,
    #[serde(default)]
    pub queued: bool,
    #[serde(default)]
    pub queue_position: Option<u32>,
    #[serde(default)]
    pub resource_usage: Option<ResourceUsage>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<DeploymentPhase>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub progress: Option<ProgressInfo>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub phase_history: Vec<PhaseTransition>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ResourceUsage {
    pub cpu_usage: f64,
    pub memory_usage: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub gpu_usage: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub storage_used: Option<u64>,
}

/// Deployment lifecycle phase for granular progress tracking.
#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum DeploymentPhase {
    #[default]
    Pending,
    Scheduling,
    Pulling,
    Initializing,
    StorageSync,
    Starting,
    HealthCheck,
    Ready,
    Degraded,
    Failed,
    Suspended,
    Terminating,
}

impl DeploymentPhase {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Scheduling => "scheduling",
            Self::Pulling => "pulling",
            Self::Initializing => "initializing",
            Self::StorageSync => "storage_sync",
            Self::Starting => "starting",
            Self::HealthCheck => "health_check",
            Self::Ready => "ready",
            Self::Degraded => "degraded",
            Self::Failed => "failed",
            Self::Suspended => "suspended",
            Self::Terminating => "terminating",
        }
    }

    pub fn requeue_interval(&self) -> std::time::Duration {
        use std::time::Duration;
        match self {
            Self::Scheduling | Self::Pulling | Self::Initializing => Duration::from_secs(5),
            Self::StorageSync | Self::HealthCheck | Self::Starting => Duration::from_secs(5),
            Self::Ready | Self::Suspended => Duration::from_secs(120),
            Self::Degraded => Duration::from_secs(30),
            Self::Failed => Duration::from_secs(60),
            Self::Pending | Self::Terminating => Duration::from_secs(10),
        }
    }

    pub fn to_state_string(&self) -> String {
        match self {
            Self::Ready => "Active".to_string(),
            Self::Failed => "Failed".to_string(),
            Self::Terminating => "Terminating".to_string(),
            Self::Suspended => "Suspended".to_string(),
            _ => "Pending".to_string(),
        }
    }
}

/// Progress information for phases that support tracking.
#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, Default)]
#[serde(rename_all = "camelCase")]
pub struct ProgressInfo {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bytes_synced: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bytes_total: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub percentage: Option<f32>,
    #[serde(default)]
    pub current_step: String,
    #[serde(default)]
    pub started_at: String,
    #[serde(default)]
    pub elapsed_seconds: u64,
}

impl ProgressInfo {
    pub fn new(current_step: &str) -> Self {
        Self {
            bytes_synced: None,
            bytes_total: None,
            percentage: None,
            current_step: current_step.to_string(),
            started_at: k8s_openapi::chrono::Utc::now().to_rfc3339(),
            elapsed_seconds: 0,
        }
    }

    pub fn with_bytes(mut self, synced: u64, total: Option<u64>) -> Self {
        self.bytes_synced = Some(synced);
        self.bytes_total = total;
        if let Some(t) = total {
            if t > 0 {
                self.percentage = Some((synced as f32 / t as f32) * 100.0);
            }
        }
        self
    }

    pub fn with_elapsed(mut self, seconds: u64) -> Self {
        self.elapsed_seconds = seconds;
        self
    }
}

/// Record of a phase transition for history tracking.
#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct PhaseTransition {
    pub phase: DeploymentPhase,
    pub timestamp: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration_seconds: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

impl PhaseTransition {
    pub fn new(phase: DeploymentPhase) -> Self {
        Self {
            phase,
            timestamp: k8s_openapi::chrono::Utc::now().to_rfc3339(),
            duration_seconds: None,
            message: None,
        }
    }

    pub fn with_message(mut self, message: &str) -> Self {
        self.message = Some(message.to_string());
        self
    }

    pub fn with_duration(mut self, seconds: u64) -> Self {
        self.duration_seconds = Some(seconds);
        self
    }
}

impl UserDeploymentSpec {
    pub fn new(
        user_id: String,
        instance_name: String,
        image: String,
        replicas: u32,
        port: u32,
        path_prefix: String,
    ) -> Self {
        Self {
            user_id,
            instance_name,
            image,
            replicas,
            port,
            command: Vec::new(),
            args: Vec::new(),
            env: Vec::new(),
            resources: None,
            path_prefix,
            ttl_seconds: None,
            health_check: None,
            storage: None,
            enable_billing: default_enable_billing(),
            queue_name: None,
            suspended: false,
            priority: None,
            public: false,
            topology_spread: None,
        }
    }

    pub fn with_command(mut self, command: Vec<String>) -> Self {
        self.command = command;
        self
    }

    pub fn with_args(mut self, args: Vec<String>) -> Self {
        self.args = args;
        self
    }

    pub fn with_env(mut self, env: Vec<EnvVar>) -> Self {
        self.env = env;
        self
    }

    pub fn with_resources(mut self, resources: ResourceRequirements) -> Self {
        self.resources = Some(resources);
        self
    }

    pub fn with_ttl(mut self, ttl_seconds: u32) -> Self {
        self.ttl_seconds = Some(ttl_seconds);
        self
    }

    pub fn with_health_check(mut self, health_check: HealthCheckConfig) -> Self {
        self.health_check = Some(health_check);
        self
    }

    pub fn with_storage(mut self, storage: StorageSpec) -> Self {
        self.storage = Some(storage);
        self
    }

    pub fn with_queue(mut self, queue_name: String) -> Self {
        self.queue_name = Some(queue_name);
        self
    }

    pub fn with_priority(mut self, priority: String) -> Self {
        self.priority = Some(priority);
        self
    }

    pub fn suspended(mut self) -> Self {
        self.suspended = true;
        self
    }

    pub fn disable_billing(mut self) -> Self {
        self.enable_billing = false;
        self
    }

    pub fn with_public(mut self, public: bool) -> Self {
        self.public = public;
        self
    }

    pub fn with_topology_spread(mut self, topology_spread: TopologySpreadConfig) -> Self {
        self.topology_spread = Some(topology_spread);
        self
    }
}

impl UserDeploymentStatus {
    pub const MAX_PHASE_HISTORY: usize = 5;

    pub fn new() -> Self {
        Self {
            state: "Pending".to_string(),
            deployment_name: String::new(),
            service_name: String::new(),
            replicas_ready: 0,
            replicas_desired: 0,
            endpoint: String::new(),
            public_url: String::new(),
            message: None,
            start_time: None,
            last_updated: String::new(),
            suspended: false,
            queued: false,
            queue_position: None,
            resource_usage: None,
            phase: None,
            progress: None,
            phase_history: Vec::new(),
        }
    }

    pub fn with_state(mut self, state: &str) -> Self {
        self.state = state.to_string();
        self
    }

    pub fn with_deployment_name(mut self, name: String) -> Self {
        self.deployment_name = name;
        self
    }

    pub fn with_service_name(mut self, name: String) -> Self {
        self.service_name = name;
        self
    }

    pub fn with_replicas(mut self, desired: u32, ready: u32) -> Self {
        self.replicas_desired = desired;
        self.replicas_ready = ready;
        self
    }

    pub fn with_endpoint(mut self, endpoint: String) -> Self {
        self.endpoint = endpoint;
        self
    }

    pub fn with_public_url(mut self, url: String) -> Self {
        self.public_url = url;
        self
    }

    pub fn with_message(mut self, message: String) -> Self {
        self.message = Some(message);
        self
    }

    pub fn with_suspended(mut self, suspended: bool) -> Self {
        self.suspended = suspended;
        self
    }

    pub fn with_queued(mut self, queued: bool, position: Option<u32>) -> Self {
        self.queued = queued;
        self.queue_position = position;
        self
    }

    pub fn with_resource_usage(mut self, usage: ResourceUsage) -> Self {
        self.resource_usage = Some(usage);
        self
    }

    pub fn with_phase(mut self, phase: DeploymentPhase) -> Self {
        self.state = phase.to_state_string();
        self.phase = Some(phase);
        self
    }

    pub fn with_progress(mut self, progress: ProgressInfo) -> Self {
        self.progress = Some(progress);
        self
    }

    pub fn add_phase_transition(&mut self, transition: PhaseTransition) {
        self.phase_history.push(transition);
        if self.phase_history.len() > Self::MAX_PHASE_HISTORY {
            let excess = self.phase_history.len() - Self::MAX_PHASE_HISTORY;
            self.phase_history.drain(0..excess);
        }
    }

    pub fn with_phase_transition(mut self, transition: PhaseTransition) -> Self {
        self.add_phase_transition(transition);
        self
    }

    pub fn clear_progress(&mut self) {
        self.progress = None;
    }

    pub fn is_pending(&self) -> bool {
        self.state == "Pending"
    }

    pub fn is_active(&self) -> bool {
        self.state == "Active"
    }

    pub fn is_failed(&self) -> bool {
        self.state == "Failed"
    }

    pub fn is_terminating(&self) -> bool {
        self.state == "Terminating"
    }

    pub fn is_suspended(&self) -> bool {
        self.suspended || self.state == "Suspended"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_spec_builder() {
        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "my-nginx".to_string(),
            "nginx:latest".to_string(),
            2,
            80,
            "/deployments/my-nginx".to_string(),
        )
        .with_command(vec!["/bin/sh".to_string()])
        .with_env(vec![EnvVar {
            name: "ENV_VAR".to_string(),
            value: "value".to_string(),
        }])
        .with_ttl(3600);

        assert_eq!(spec.user_id, "user123");
        assert_eq!(spec.instance_name, "my-nginx");
        assert_eq!(spec.replicas, 2);
        assert_eq!(spec.port, 80);
        assert_eq!(spec.command, vec!["/bin/sh"]);
        assert_eq!(spec.env.len(), 1);
        assert_eq!(spec.ttl_seconds, Some(3600));
        assert!(spec.enable_billing);
        assert!(!spec.suspended);
    }

    #[test]
    fn test_status_builder() {
        let status = UserDeploymentStatus::new()
            .with_state("Active")
            .with_replicas(2, 2)
            .with_public_url("http://3.21.154.119:8080/deployments/test/".to_string());

        assert_eq!(status.state, "Active");
        assert_eq!(status.replicas_desired, 2);
        assert_eq!(status.replicas_ready, 2);
        assert!(status.is_active());
        assert!(!status.is_pending());
    }

    #[test]
    fn test_status_predicates() {
        let pending = UserDeploymentStatus::new().with_state("Pending");
        assert!(pending.is_pending());
        assert!(!pending.is_active());

        let active = UserDeploymentStatus::new().with_state("Active");
        assert!(active.is_active());
        assert!(!active.is_failed());

        let failed = UserDeploymentStatus::new().with_state("Failed");
        assert!(failed.is_failed());
        assert!(!failed.is_terminating());

        let terminating = UserDeploymentStatus::new().with_state("Terminating");
        assert!(terminating.is_terminating());
    }

    #[test]
    fn test_env_var_equality() {
        let env1 = EnvVar {
            name: "KEY".to_string(),
            value: "value".to_string(),
        };
        let env2 = EnvVar {
            name: "KEY".to_string(),
            value: "value".to_string(),
        };
        assert_eq!(env1, env2);
    }

    #[test]
    fn test_resource_requirements() {
        let resources = ResourceRequirements {
            cpu: "500m".to_string(),
            memory: "512Mi".to_string(),
            gpus: None,
            cpu_request_ratio: 1.0,
        };

        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "test".to_string(),
            "nginx:latest".to_string(),
            1,
            80,
            "/deployments/test".to_string(),
        )
        .with_resources(resources.clone());

        assert!(spec.resources.is_some());
        let res = spec.resources.unwrap();
        assert_eq!(res.cpu, "500m");
        assert_eq!(res.memory, "512Mi");
        assert!(res.gpus.is_none());
        assert!((res.cpu_request_ratio - 1.0).abs() < f32::EPSILON);
    }

    #[test]
    fn test_gpu_spec() {
        let gpu_spec = GpuSpec {
            count: 2,
            model: vec!["A100".to_string(), "H100".to_string()],
            min_cuda_version: Some("12.0".to_string()),
            min_gpu_memory_gb: Some(80),
        };

        let resources = ResourceRequirements {
            cpu: "8".to_string(),
            memory: "32Gi".to_string(),
            gpus: Some(gpu_spec.clone()),
            cpu_request_ratio: 0.75,
        };

        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "ml-training".to_string(),
            "pytorch/pytorch:2.0".to_string(),
            1,
            8080,
            "/deployments/ml-training".to_string(),
        )
        .with_resources(resources);

        assert!(spec.resources.is_some());
        let res = spec.resources.unwrap();
        assert!(res.gpus.is_some());
        let gpus = res.gpus.unwrap();
        assert_eq!(gpus.count, 2);
        assert_eq!(gpus.model.len(), 2);
        assert_eq!(gpus.min_cuda_version, Some("12.0".to_string()));
        assert_eq!(gpus.min_gpu_memory_gb, Some(80));
        assert!((res.cpu_request_ratio - 0.75).abs() < f32::EPSILON);
    }

    #[test]
    fn test_storage_spec() {
        let storage = StorageSpec {
            ephemeral: None,
            persistent: Some(PersistentStorageSpec {
                enabled: true,
                backend: StorageBackend::R2,
                bucket: "my-bucket".to_string(),
                region: Some("auto".to_string()),
                endpoint: Some("https://account.r2.cloudflarestorage.com".to_string()),
                credentials_secret: Some("my-r2-creds".to_string()),
                sync_interval_ms: 1000,
                cache_size_mb: 2048,
                mount_path: "/data".to_string(),
            }),
        };

        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "storage-test".to_string(),
            "ubuntu:latest".to_string(),
            1,
            8080,
            "/deployments/storage-test".to_string(),
        )
        .with_storage(storage.clone());

        assert!(spec.storage.is_some());
        let s = spec.storage.unwrap();
        assert!(s.persistent.is_some());
        let p = s.persistent.unwrap();
        assert!(p.enabled);
        assert_eq!(p.bucket, "my-bucket");
        assert_eq!(p.mount_path, "/data");
    }

    #[test]
    fn test_suspend_resume() {
        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "test".to_string(),
            "nginx:latest".to_string(),
            2,
            80,
            "/deployments/test".to_string(),
        )
        .suspended();

        assert!(spec.suspended);

        let status = UserDeploymentStatus::new()
            .with_state("Suspended")
            .with_suspended(true);

        assert!(status.is_suspended());
    }

    #[test]
    fn test_queue_and_priority() {
        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "test".to_string(),
            "nginx:latest".to_string(),
            2,
            80,
            "/deployments/test".to_string(),
        )
        .with_queue("default-queue".to_string())
        .with_priority("high".to_string());

        assert_eq!(spec.queue_name, Some("default-queue".to_string()));
        assert_eq!(spec.priority, Some("high".to_string()));

        let status = UserDeploymentStatus::new().with_queued(true, Some(5));

        assert!(status.queued);
        assert_eq!(status.queue_position, Some(5));
    }

    #[test]
    fn test_billing_toggle() {
        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "test".to_string(),
            "nginx:latest".to_string(),
            1,
            80,
            "/deployments/test".to_string(),
        );

        assert!(spec.enable_billing);

        let spec_no_billing = spec.disable_billing();
        assert!(!spec_no_billing.enable_billing);
    }

    #[test]
    fn test_resource_usage() {
        let usage = ResourceUsage {
            cpu_usage: 0.5,
            memory_usage: 1024.0,
            gpu_usage: Some(0.8),
            storage_used: Some(1073741824),
        };

        let status = UserDeploymentStatus::new().with_resource_usage(usage.clone());

        assert!(status.resource_usage.is_some());
        let u = status.resource_usage.unwrap();
        assert_eq!(u.cpu_usage, 0.5);
        assert_eq!(u.memory_usage, 1024.0);
        assert_eq!(u.gpu_usage, Some(0.8));
        assert_eq!(u.storage_used, Some(1073741824));
    }

    #[test]
    fn test_storage_backend_serialization() {
        let backend_r2 = StorageBackend::R2;
        let backend_s3 = StorageBackend::S3;
        let backend_gcs = StorageBackend::GCS;

        let json_r2 = serde_json::to_string(&backend_r2).unwrap();
        let json_s3 = serde_json::to_string(&backend_s3).unwrap();
        let json_gcs = serde_json::to_string(&backend_gcs).unwrap();

        assert_eq!(json_r2, "\"r2\"");
        assert_eq!(json_s3, "\"s3\"");
        assert_eq!(json_gcs, "\"gcs\"");

        let deserialized_r2: StorageBackend = serde_json::from_str(&json_r2).unwrap();
        assert!(matches!(deserialized_r2, StorageBackend::R2));
    }

    #[test]
    fn test_progress_info_new_sets_started_at() {
        let progress = ProgressInfo::new("Pulling image");

        assert!(!progress.started_at.is_empty());
        assert!(progress.started_at.contains("T")); // ISO 8601 format
        assert_eq!(progress.elapsed_seconds, 0);
        assert_eq!(progress.current_step, "Pulling image");
    }
}
