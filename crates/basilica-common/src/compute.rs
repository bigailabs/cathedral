use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GpuSpec {
    pub count: u32,
    #[serde(default)]
    pub model: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Resources {
    pub cpu: String,    // e.g., "4"
    pub memory: String, // e.g., "16Gi"
    pub gpus: GpuSpec,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ComputeSpec {
    pub image: String,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: Vec<(String, String)>,
    pub resources: Resources,
    #[serde(default)]
    pub storage_ephemeral_gi: u32,
    #[serde(default)]
    pub ttl_seconds: u32,
    #[serde(default)]
    pub priority: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serde_round_trip_compute_spec() {
        let spec = ComputeSpec {
            image: "nvidia/cuda:12.0-base".to_string(),
            command: vec!["python".into(), "main.py".into()],
            args: vec!["--epochs".into(), "3".into()],
            env: vec![("FOO".into(), "bar".into())],
            resources: Resources {
                cpu: "4".into(),
                memory: "16Gi".into(),
                gpus: GpuSpec {
                    count: 1,
                    model: vec!["A100".into()],
                },
            },
            storage_ephemeral_gi: 50,
            ttl_seconds: 3600,
            priority: "normal".into(),
        };

        let json = serde_json::to_string(&spec).unwrap();
        let de: ComputeSpec = serde_json::from_str(&json).unwrap();
        assert_eq!(de, spec);
    }
}
