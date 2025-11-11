use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(CustomResource, Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[kube(
    group = "basilica.ai",
    version = "v1",
    kind = "BasilicaQueue",
    namespaced
)]
pub struct BasilicaQueueSpec {
    pub concurrency: u32,
    #[serde(default)]
    pub gpu_limits: Option<GpuLimits>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
pub struct GpuLimits {
    pub total: u32,
    #[serde(default)]
    pub models: Option<std::collections::BTreeMap<String, u32>>, // e.g., { "A100": 4 }
}
