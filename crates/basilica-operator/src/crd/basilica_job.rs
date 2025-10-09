use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(CustomResource, Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[kube(
    group = "basilica.ai",
    version = "v1",
    kind = "BasilicaJob",
    namespaced
)]
#[kube(status = "BasilicaJobStatus")]
#[serde(rename_all = "camelCase")]
pub struct BasilicaJobSpec {
    pub image: String,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: Vec<EnvVar>,
    pub resources: Resources,
    #[serde(default)]
    pub storage: Option<StorageSpec>,
    #[serde(default)]
    pub artifacts: Option<ArtifactUploadSpec>,
    #[serde(default)]
    pub ttl_seconds: u32,
    #[serde(default)]
    pub priority: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct EnvVar {
    pub name: String,
    pub value: String,
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
pub struct StorageSpec {
    /// Ephemeral storage size (e.g., "10Gi")
    #[serde(default)]
    pub ephemeral: String,

    /// Persistent storage configuration
    #[serde(default)]
    pub persistent: Option<PersistentStorageSpec>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct PersistentStorageSpec {
    /// Whether to enable FUSE-based persistent storage
    #[serde(default)]
    pub enabled: bool,

    /// Storage backend type (r2, s3, gcs)
    #[serde(default)]
    pub backend: String,

    /// Bucket name
    #[serde(default)]
    pub bucket: String,

    /// Optional region (for S3)
    #[serde(default)]
    pub region: Option<String>,

    /// Optional endpoint (for R2 or custom S3-compatible services)
    #[serde(default)]
    pub endpoint: Option<String>,

    /// K8s Secret name containing storage credentials
    /// Expected keys: access_key_id, secret_access_key
    #[serde(default)]
    pub credentials_secret: Option<String>,

    /// Sync interval in milliseconds (default: 1000)
    #[serde(default)]
    pub sync_interval_ms: Option<u64>,

    /// Cache size in MB (default: 2048)
    #[serde(default)]
    pub cache_size_mb: Option<usize>,

    /// Mount path for persistent storage (default: /data)
    #[serde(default)]
    pub mount_path: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactUploadSpec {
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

#[derive(Serialize, Deserialize, Clone, Debug, Default, JsonSchema)]
pub struct BasilicaJobStatus {
    #[serde(default)]
    pub phase: Option<String>,
    #[serde(default)]
    pub pod_name: Option<String>,
    #[serde(default)]
    pub start_time: Option<String>,
    #[serde(default)]
    pub completion_time: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use kube::core::CustomResourceExt;

    #[test]
    fn crd_metadata_is_correct() {
        let crd = BasilicaJob::crd();
        let name = crd.metadata.name.unwrap();
        assert_eq!(name, "basilicajobs.basilica.ai");
        assert_eq!(crd.spec.group, "basilica.ai");
        assert_eq!(crd.spec.names.kind, "BasilicaJob");
        assert_eq!(crd.spec.scope, "Namespaced");
    }

    #[test]
    fn spec_has_expected_fields() {
        let crd = BasilicaJob::crd();
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
        assert!(spec_props.contains_key("image"));
        assert!(spec_props.contains_key("resources"));
        assert!(spec_props.contains_key("ttlSeconds"));
    }
}
