use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GpuSpec {
    pub count: u32,
    #[serde(default)]
    pub model: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Resources {
    pub cpu: String,
    pub memory: String,
    pub gpus: GpuSpec,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PortSpec {
    #[serde(alias = "containerPort")]
    pub container_port: u16,
    #[serde(default = "default_protocol")]
    pub protocol: String,
}

fn default_protocol() -> String {
    "TCP".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StorageConfig {
    pub backend: String,
    #[serde(default)]
    pub bucket: Option<String>,
    #[serde(default)]
    pub prefix: Option<String>,
    #[serde(default)]
    pub credentials: Option<std::collections::HashMap<String, String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct JobSpecDto {
    pub image: String,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: Vec<(String, String)>,
    pub resources: Resources,
    #[serde(default)]
    pub ttl_seconds: u32,
    #[serde(default)]
    pub ports: Vec<PortSpec>,
    #[serde(default)]
    pub storage: Option<StorageConfig>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct JobStatusDto {
    pub phase: String,
    pub pod_name: Option<String>,
    #[serde(default)]
    pub endpoints: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ExecResultDto {
    pub stdout: String,
    #[serde(default)]
    pub stderr: String,
    pub exit_code: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalSpecDto {
    pub container_image: String,
    pub resources: Resources,
    #[serde(default)]
    pub container_env: Vec<(String, String)>,
    #[serde(default)]
    pub container_command: Vec<String>,
    #[serde(default)]
    pub container_ports: Vec<RentalPortDto>,
    #[serde(default)]
    pub network_ingress: Vec<IngressRuleDto>,
    #[serde(default)]
    pub ssh: Option<RentalSshDto>,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub namespace: Option<String>,
    #[serde(default)]
    pub labels: Option<std::collections::BTreeMap<String, String>>,
    #[serde(default)]
    pub annotations: Option<std::collections::BTreeMap<String, String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalStatusDto {
    pub state: String,
    pub pod_name: Option<String>,
    #[serde(default)]
    pub endpoints: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalListItemDto {
    pub rental_id: String,
    pub status: RentalStatusDto,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalPortDto {
    pub container_port: u16,
    pub protocol: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IngressRuleDto {
    pub port: u16,
    pub exposure: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalSshDto {
    pub enabled: bool,
    pub public_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeploymentEventDto {
    #[serde(rename = "type")]
    pub event_type: String,
    pub reason: String,
    pub message: String,
    pub count: Option<i32>,
    pub last_timestamp: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResourceQuotaDto {
    // limits.* quota (for containers with resource limits)
    pub cpu_limit: Option<String>,
    pub cpu_used: Option<String>,
    pub memory_limit: Option<String>,
    pub memory_used: Option<String>,
    // requests.* quota (for containers with resource requests)
    pub requests_cpu_limit: Option<String>,
    pub requests_cpu_used: Option<String>,
    pub requests_memory_limit: Option<String>,
    pub requests_memory_used: Option<String>,
    // pod count quota
    pub pods_limit: Option<i64>,
    pub pods_used: Option<i64>,
    // GPU quota (requests.nvidia.com/gpu)
    pub gpu_limit: Option<i64>,
    pub gpu_used: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClusterCapacityResult {
    pub has_capacity: bool,
    pub message: Option<String>,
    pub available_cpu: Option<String>,
    pub available_memory: Option<String>,
    pub available_gpus: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct DeploymentProgressDto {
    #[serde(default)]
    pub bytes_synced: Option<u64>,
    #[serde(default)]
    pub bytes_total: Option<u64>,
    #[serde(default)]
    pub percentage: Option<f32>,
    #[serde(default)]
    pub current_step: String,
    #[serde(default)]
    pub started_at: String,
    #[serde(default)]
    pub elapsed_seconds: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct PhaseTransitionDto {
    pub phase: String,
    pub timestamp: String,
    #[serde(default)]
    pub duration_seconds: Option<u64>,
    #[serde(default)]
    pub message: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct DeploymentPhaseDto {
    #[serde(default)]
    pub phase: String,
    #[serde(default)]
    pub progress: Option<DeploymentProgressDto>,
    #[serde(default)]
    pub phase_history: Vec<PhaseTransitionDto>,
    #[serde(default)]
    pub replicas_desired: u32,
    #[serde(default)]
    pub replicas_ready: u32,
}
