use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(CustomResource, Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[kube(group = "basilica.io", version = "v1", kind = "BasilicaNodeProfile", namespaced)]
#[kube(status = "BasilicaNodeProfileStatus")]
pub struct BasilicaNodeProfileSpec {
    pub provider: String,
    pub region: String,
    pub gpu: NodeGpu,
    pub cpu: NodeCpu,
    pub memory_gb: u32,
    pub storage_gb: u32,
    pub network_gbps: u32,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
pub struct NodeGpu { pub model: String, pub count: u32, pub memory_gb: u32 }

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
pub struct NodeCpu { pub model: String, pub cores: u32 }

#[derive(Serialize, Deserialize, Clone, Debug, Default, JsonSchema)]
pub struct BasilicaNodeProfileStatus {
    #[serde(default)]
    pub last_validated: Option<String>,
    #[serde(default)]
    pub kube_node_name: Option<String>,
    #[serde(default)]
    pub health: Option<String>,
}
