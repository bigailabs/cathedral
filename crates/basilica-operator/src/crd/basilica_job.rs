use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(CustomResource, Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[kube(group = "basilica.io", version = "v1", kind = "BasilicaJob", namespaced)]
#[kube(status = "BasilicaJobStatus")]
#[serde(rename_all = "camelCase")]
pub struct BasilicaJobSpec {
    pub image: String,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: Vec<(String, String)>,
    pub resources: Resources,
    #[serde(default)]
    pub storage: Option<StorageSpec>,
    #[serde(default)]
    pub ttl_seconds: u32,
    #[serde(default)]
    pub priority: String,
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
pub struct StorageSpec {
    pub ephemeral: String,
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
    use serde_json::json;

    #[test]
    fn crd_metadata_is_correct() {
        let crd = BasilicaJob::crd();
        let name = crd.metadata.name.unwrap();
        assert_eq!(name, "basilicajobs.basilica.io");
        assert_eq!(crd.spec.group, "basilica.io");
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
