use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(CustomResource, Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[kube(group = "basilica.io", version = "v1", kind = "GpuRental", namespaced)]
#[kube(status = "GpuRentalStatus")]
#[serde(rename_all = "camelCase")]
pub struct GpuRentalSpec {
    pub container: RentalContainer,
    pub duration: RentalDuration,
    pub access_type: AccessType,
    #[serde(default)]
    pub network: RentalNetwork,
    #[serde(default)]
    pub storage: Option<RentalStorage>,
    #[serde(default)]
    pub artifacts: Option<RentalArtifacts>,
    #[serde(default)]
    pub ssh: Option<RentalSsh>,
    #[serde(default)]
    pub jupyter_access: Option<RentalJupyter>,
    #[serde(default)]
    pub environment: Option<RentalEnvironment>,
    #[serde(default)]
    pub miner_selector: Option<MinerSelector>,
    #[serde(default)]
    pub billing: Option<RentalBilling>,
    #[serde(default)]
    pub ttl_seconds: u32,
    #[serde(default)]
    pub tenancy: Option<TenancyRef>,
    #[serde(default)]
    pub exclusive: bool,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct TenancyRef {
    pub user_id: String,
    pub project_id: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct RentalContainer {
    pub image: String,
    #[serde(default)]
    pub env: Vec<(String, String)>,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub ports: Vec<RentalPort>,
    #[serde(default)]
    pub volumes: Vec<VolumeMount>,
    pub resources: Resources,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct RentalPort {
    pub container_port: u16,
    #[serde(default = "default_protocol")]
    pub protocol: String, // TCP | UDP
}

fn default_protocol() -> String {
    "TCP".into()
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct VolumeMount {
    #[serde(default)]
    pub host_path: Option<String>,
    pub container_path: String,
    #[serde(default)]
    pub read_only: bool,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct Resources {
    pub cpu: String,
    pub memory: String,
    pub gpus: GpuSpec,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
pub struct GpuSpec {
    pub count: u32,
    #[serde(default)]
    pub model: Vec<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct RentalDuration {
    pub hours: u32,
    #[serde(default)]
    pub auto_extend: bool,
    #[serde(default)]
    pub max_extensions: u32,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum AccessType {
    Ssh,
    Jupyter,
    Vscode,
    Custom,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, Default)]
#[serde(rename_all = "camelCase")]
pub struct RentalNetwork {
    #[serde(default)]
    pub ingress: Vec<IngressRule>,
    #[serde(default)]
    pub egress_policy: String,
    #[serde(default)]
    pub allowed_egress: Vec<String>,
    #[serde(default)]
    pub public_ip_required: bool,
    #[serde(default)]
    pub bandwidth_mbps: Option<u32>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
pub struct IngressRule {
    pub port: u16,
    pub exposure: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
pub struct RentalSsh {
    pub enabled: bool,
    pub public_key: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
pub struct RentalJupyter {
    pub password: Option<String>,
    pub token: Option<String>,
    pub base_image: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct RentalEnvironment {
    pub base_image: Option<String>,
    pub pre_install_script: Option<String>,
    pub environment_variables: Vec<(String, String)>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct RentalStorage {
    pub persistent_volume_gb: u32,
    pub storage_class: Option<String>,
    pub mount_path: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct RentalArtifacts {
    /// Destination URI (e.g., s3://bucket/prefix)
    pub destination: String,
    /// Path inside the container to upload from
    pub from_path: String,
    /// Provider identifier (e.g., s3, gcs). Optional; default s3
    #[serde(default)]
    pub provider: String,
    /// Optional K8s Secret name containing credentials
    #[serde(default)]
    pub credentials_secret: Option<String>,
    /// Whether artifact upload sidecar is enabled
    #[serde(default)]
    pub enabled: bool,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct MinerSelector {
    pub id: Option<String>,
    pub region: Option<String>,
    pub tier: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct RentalBilling {
    pub max_hourly_rate: f64,
    pub payment_method: String,
    pub account_id: Option<String>,
    pub deposit_amount: Option<f64>,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default, JsonSchema)]
pub struct GpuRentalStatus {
    #[serde(default)]
    pub state: Option<String>,
    #[serde(default)]
    pub pod_name: Option<String>,
    #[serde(default)]
    pub node_name: Option<String>,
    #[serde(default)]
    pub start_time: Option<String>,
    #[serde(default)]
    pub expiry_time: Option<String>,
    #[serde(default)]
    pub renewal_time: Option<String>,
    #[serde(default)]
    pub total_cost: Option<f64>,
    #[serde(default)]
    pub total_extensions: Option<u32>,
    #[serde(default)]
    pub endpoints: Option<Vec<String>>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use kube::core::CustomResourceExt;

    #[test]
    fn crd_metadata_is_correct() {
        let crd = GpuRental::crd();
        let name = crd.metadata.name.unwrap();
        assert_eq!(name, "gpurentals.basilica.io");
        assert_eq!(crd.spec.group, "basilica.io");
        assert_eq!(crd.spec.names.kind, "GpuRental");
        assert_eq!(crd.spec.scope, "Namespaced");
    }

    #[test]
    fn spec_includes_duration_access_billing() {
        let crd = GpuRental::crd();
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
        assert!(spec_props.contains_key("duration"));
        assert!(spec_props.contains_key("accessType"));
        assert!(spec_props.contains_key("billing"));
    }
}
