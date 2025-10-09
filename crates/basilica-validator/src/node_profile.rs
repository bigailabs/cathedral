use std::collections::BTreeMap;

use crate::miner_prover::types::NodeResult;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeProfileSpec {
    pub provider: String,
    pub region: String,
    pub gpu: NodeGpu,
    pub cpu: NodeCpu,
    pub memory_gb: u32,
    pub storage_gb: u32,
    pub network_gbps: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeGpu {
    pub model: String,
    pub count: u32,
    pub memory_gb: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeCpu {
    pub model: String,
    pub cores: u32,
}

#[derive(Debug, Clone)]
pub struct NodeProfileInput<'a> {
    pub provider: &'a str,
    pub region: &'a str,
    pub node_result: &'a NodeResult,
}

pub fn to_node_profile_spec(input: &NodeProfileInput<'_>) -> NodeProfileSpec {
    let nr = input.node_result;
    let gpu_count = nr.gpu_infos.len() as u32;
    let gpu_model = nr
        .gpu_infos
        .first()
        .map(|g| g.gpu_name.clone())
        .unwrap_or_else(|| nr.gpu_name.clone());
    let gpu_mem = nr
        .gpu_infos
        .first()
        .map(|g| g.gpu_memory_gb as u32)
        .unwrap_or(0);
    let memory_gb = nr.memory_info.total_gb as u32;
    let cpu = NodeCpu {
        model: nr.cpu_info.model.clone(),
        cores: nr.cpu_info.cores,
    };
    let gpu = NodeGpu {
        model: gpu_model,
        count: gpu_count,
        memory_gb: gpu_mem,
    };
    NodeProfileSpec {
        provider: input.provider.to_string(),
        region: input.region.to_string(),
        gpu,
        cpu,
        memory_gb,
        storage_gb: 0,
        network_gbps: 1,
    }
}

/// Produce Kubernetes node labels from a validation result and context.
pub fn labels_from_validation(
    nr: &NodeResult,
    provider: &str,
    region: &str,
) -> BTreeMap<String, String> {
    let mut labels = BTreeMap::new();
    labels.insert("basilica.ai/validated".into(), "true".into());
    labels.insert("basilica.ai/provider".into(), provider.to_string());
    labels.insert("basilica.ai/region".into(), region.to_string());
    let model = nr
        .gpu_infos
        .first()
        .map(|g| g.gpu_name.clone())
        .unwrap_or_else(|| nr.gpu_name.clone());
    labels.insert("basilica.ai/gpu-model".into(), model);
    labels.insert(
        "basilica.ai/gpu-count".into(),
        nr.gpu_infos.len().to_string(),
    );
    labels.insert(
        "basilica.ai/gpu-mem".into(),
        nr.gpu_infos
            .first()
            .map(|g| g.gpu_memory_gb as u32)
            .unwrap_or(0)
            .to_string(),
    );
    labels
}

/// Suggested taint when node is not validated.
pub fn taint_for_non_validated() -> (&'static str, &'static str) {
    ("basilica.ai/validated", "NoSchedule")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::miner_prover::types::{
        BinaryCpuInfo, BinaryMemoryInfo, BinaryNetworkInfo, CompressedMatrix, GpuInfo,
        NetworkInterface, NodeResult, SmUtilizationStats,
    };

    fn sample_node_result() -> NodeResult {
        NodeResult {
            gpu_name: "NVIDIA A100".into(),
            gpu_uuid: "GPU-XYZ".into(),
            gpu_infos: vec![GpuInfo {
                index: 0,
                gpu_name: "NVIDIA A100".into(),
                gpu_uuid: "GPU-XYZ".into(),
                gpu_memory_gb: 80.0,
                computation_time_ns: 0,
                memory_bandwidth_gbps: 0.0,
                sm_utilization: SmUtilizationStats {
                    min_utilization: 0.0,
                    max_utilization: 0.0,
                    avg_utilization: 0.0,
                    per_sm_stats: vec![],
                },
                active_sms: 0,
                total_sms: 0,
                anti_debug_passed: true,
            }],
            cpu_info: BinaryCpuInfo {
                model: "AMD EPYC".into(),
                cores: 64,
                threads: 128,
                frequency_mhz: 0,
            },
            memory_info: BinaryMemoryInfo {
                total_gb: 256.0,
                available_gb: 0.0,
            },
            network_info: BinaryNetworkInfo {
                interfaces: vec![NetworkInterface {
                    name: "eth0".into(),
                    mac_address: "aa:bb".into(),
                    ip_addresses: vec!["10.0.0.2".into()],
                }],
            },
            matrix_c: CompressedMatrix {
                rows: 0,
                cols: 0,
                data: vec![],
            },
            computation_time_ns: 0,
            checksum: [0u8; 32],
            sm_utilization: SmUtilizationStats {
                min_utilization: 0.0,
                max_utilization: 0.0,
                avg_utilization: 0.0,
                per_sm_stats: vec![],
            },
            active_sms: 0,
            total_sms: 0,
            memory_bandwidth_gbps: 0.0,
            anti_debug_passed: true,
            timing_fingerprint: 0,
        }
    }

    #[test]
    fn maps_to_node_profile_spec() {
        let nr = sample_node_result();
        let input = NodeProfileInput {
            provider: "onprem",
            region: "us-east-1",
            node_result: &nr,
        };
        let spec = to_node_profile_spec(&input);
        assert_eq!(spec.provider, "onprem");
        assert_eq!(spec.region, "us-east-1");
        assert_eq!(spec.gpu.model, "NVIDIA A100");
        assert_eq!(spec.gpu.count, 1);
        assert_eq!(spec.gpu.memory_gb, 80);
        assert_eq!(spec.cpu.model, "AMD EPYC");
        assert_eq!(spec.cpu.cores, 64);
        assert_eq!(spec.memory_gb, 256);
    }

    #[test]
    fn produces_k8s_labels() {
        let nr = sample_node_result();
        let labels = labels_from_validation(&nr, "onprem", "us-east-1");
        assert_eq!(labels.get("basilica.ai/provider").unwrap(), "onprem");
        assert_eq!(labels.get("basilica.ai/region").unwrap(), "us-east-1");
        assert_eq!(labels.get("basilica.ai/gpu-model").unwrap(), "NVIDIA A100");
        assert_eq!(labels.get("basilica.ai/gpu-count").unwrap(), "1");
        assert_eq!(labels.get("basilica.ai/gpu-mem").unwrap(), "80");
        assert_eq!(labels.get("basilica.ai/validated").unwrap(), "true");
    }
}
