use crate::crd::basilica_node_profile::{
    BasilicaNodeProfile, BasilicaNodeProfileSpec, BasilicaNodeProfileStatus, NodeCpu, NodeGpu,
};
use crate::k8s_client::K8sClient;
use anyhow::Result;
use k8s_openapi::api::core::v1::Node;
use k8s_openapi::chrono::Utc;
use tracing::{debug, error, info, warn};

#[derive(Clone)]
pub struct NodeProfileController<C: K8sClient> {
    pub client: C,
}

impl<C: K8sClient> NodeProfileController<C> {
    pub fn new(client: C) -> Self {
        Self { client }
    }

    fn validate_node_labels(labels: &std::collections::BTreeMap<String, String>) -> Result<()> {
        let node_type = labels
            .get("basilica.ai/node-type")
            .ok_or_else(|| anyhow::anyhow!("Missing required label: basilica.ai/node-type"))?;

        if node_type != "gpu" {
            return Err(anyhow::anyhow!(
                "Invalid node-type: expected 'gpu', got '{}'",
                node_type
            ));
        }

        let datacenter = labels
            .get("basilica.ai/datacenter")
            .ok_or_else(|| anyhow::anyhow!("Missing required label: basilica.ai/datacenter"))?;

        if datacenter.is_empty() {
            return Err(anyhow::anyhow!("Invalid datacenter label: cannot be empty"));
        }

        let gpu_model = labels
            .get("basilica.ai/gpu-model")
            .ok_or_else(|| anyhow::anyhow!("Missing required label: basilica.ai/gpu-model"))?;

        if gpu_model.is_empty() {
            return Err(anyhow::anyhow!("Invalid gpu-model label: cannot be empty"));
        }

        let gpu_count_str = labels
            .get("basilica.ai/gpu-count")
            .ok_or_else(|| anyhow::anyhow!("Missing required label: basilica.ai/gpu-count"))?;

        let gpu_count = gpu_count_str.parse::<u32>().map_err(|_| {
            anyhow::anyhow!(
                "Invalid gpu-count label: must be a positive integer, got '{}'",
                gpu_count_str
            )
        })?;

        if gpu_count == 0 {
            return Err(anyhow::anyhow!("Invalid gpu-count: must be greater than 0"));
        }

        let gpu_memory_str = labels
            .get("basilica.ai/gpu-memory-gb")
            .ok_or_else(|| anyhow::anyhow!("Missing required label: basilica.ai/gpu-memory-gb"))?;

        let gpu_memory = gpu_memory_str.parse::<u32>().map_err(|_| {
            anyhow::anyhow!(
                "Invalid gpu-memory-gb label: must be a positive integer, got '{}'",
                gpu_memory_str
            )
        })?;

        if gpu_memory == 0 {
            return Err(anyhow::anyhow!(
                "Invalid gpu-memory-gb: must be greater than 0"
            ));
        }

        let driver_version = labels
            .get("basilica.ai/driver-version")
            .ok_or_else(|| anyhow::anyhow!("Missing required label: basilica.ai/driver-version"))?;

        if driver_version.is_empty() {
            return Err(anyhow::anyhow!(
                "Invalid driver-version label: cannot be empty"
            ));
        }

        let cuda_version = labels
            .get("basilica.ai/cuda-version")
            .ok_or_else(|| anyhow::anyhow!("Missing required label: basilica.ai/cuda-version"))?;

        if cuda_version.is_empty() {
            return Err(anyhow::anyhow!(
                "Invalid cuda-version label: cannot be empty"
            ));
        }

        Ok(())
    }

    pub async fn reconcile(&self, node: &Node) -> Result<()> {
        let node_name = node
            .metadata
            .name
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("Node has no name"))?;

        debug!(node = %node_name, "NodeProfileController: reconciling node");

        let labels = node
            .metadata
            .labels
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("Node has no labels"))?;

        let datacenter_id = match labels.get("basilica.ai/datacenter") {
            Some(id) => id,
            None => {
                debug!(node = %node_name, "Skipping node without datacenter label");
                return Ok(());
            }
        };

        debug!(node = %node_name, datacenter = %datacenter_id, "Node has datacenter label, validating labels");

        if let Err(e) = Self::validate_node_labels(labels) {
            warn!(node = %node_name, error = %e, "Node label validation failed");
            return Err(e);
        }

        debug!(node = %node_name, "Node labels validated successfully");

        if !is_node_ready(node) {
            debug!(node = %node_name, "Skipping node that is not Ready");
            return Ok(());
        }

        info!(node = %node_name, datacenter = %datacenter_id, "Processing GPU node for validation");

        let node_id = labels
            .get("basilica.ai/node-id")
            .map(|s| s.as_str())
            .unwrap_or(node_name);

        let gpu_model = labels
            .get("basilica.ai/gpu-model")
            .map(|s| s.as_str())
            .unwrap_or("unknown");

        let gpu_count = labels
            .get("basilica.ai/gpu-count")
            .and_then(|s| s.parse::<u32>().ok())
            .unwrap_or(0);

        let gpu_memory_gb = labels
            .get("basilica.ai/gpu-memory-gb")
            .and_then(|s| s.parse::<u32>().ok())
            .unwrap_or(0);

        let (cpu_cores, memory_gb, storage_gb) = node
            .status
            .as_ref()
            .and_then(|s| s.capacity.as_ref())
            .map(|capacity| {
                let cores = capacity
                    .get("cpu")
                    .and_then(|q| q.0.parse::<i64>().ok())
                    .unwrap_or(0);
                let memory_bytes = capacity
                    .get("memory")
                    .and_then(|q| parse_memory_quantity(&q.0))
                    .unwrap_or(0);
                let storage_bytes = capacity
                    .get("ephemeral-storage")
                    .and_then(|q| parse_storage_quantity(&q.0))
                    .unwrap_or(0);
                (
                    cores as u32,
                    (memory_bytes / (1024 * 1024 * 1024)) as u32,
                    (storage_bytes / (1024 * 1024 * 1024)) as u32,
                )
            })
            .unwrap_or((0, 0, 0));

        let cpu_model = labels
            .get("node.kubernetes.io/instance-type")
            .map(|s| s.as_str())
            .unwrap_or("datacenter-gpu-node");

        let node_uid = node
            .metadata
            .uid
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("Node has no UID"))?;

        let owner_reference = k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference {
            api_version: "v1".to_string(),
            kind: "Node".to_string(),
            name: node_name.clone(),
            uid: node_uid.clone(),
            controller: Some(true),
            block_owner_deletion: Some(true),
        };

        let profile = BasilicaNodeProfile {
            metadata: k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta {
                name: Some(node_id.to_string()),
                namespace: Some("basilica-system".to_string()),
                owner_references: Some(vec![owner_reference]),
                ..Default::default()
            },
            spec: BasilicaNodeProfileSpec {
                provider: "datacenter".to_string(),
                region: datacenter_id.to_string(),
                gpu: NodeGpu {
                    model: gpu_model.to_string(),
                    count: gpu_count,
                    memory_gb: gpu_memory_gb,
                },
                cpu: NodeCpu {
                    model: cpu_model.to_string(),
                    cores: cpu_cores,
                },
                memory_gb,
                storage_gb,
                network_gbps: 10,
            },
            status: Some(BasilicaNodeProfileStatus {
                kube_node_name: Some(node_name.clone()),
                last_validated: Some(Utc::now().to_rfc3339()),
                health: Some("Active".to_string()),
            }),
        };

        debug!(node = %node_name, node_id = %node_id, "Creating BasilicaNodeProfile");

        self.client
            .create_node_profile("basilica-system", &profile)
            .await
            .or_else(|e| {
                if e.to_string().contains("AlreadyExists") {
                    debug!(node = %node_name, "NodeProfile already exists, continuing");
                    Ok(profile)
                } else {
                    error!(node = %node_name, error = %e, "Failed to create NodeProfile");
                    Err(e)
                }
            })?;

        info!(node = %node_name, "NodeProfile created successfully, removing unvalidated taint");

        self.client
            .remove_node_taint(node_name, "basilica.ai/unvalidated")
            .await
            .map_err(|e| {
                error!(node = %node_name, error = %e, "Failed to remove taint");
                e
            })?;

        info!(node = %node_name, datacenter = %datacenter_id, "Successfully validated GPU node and removed taint");

        Ok(())
    }
}

fn parse_memory_quantity(s: &str) -> Option<i64> {
    let s = s.trim();
    if let Some(num_str) = s.strip_suffix("Ki") {
        num_str.parse::<i64>().ok().map(|n| n * 1024)
    } else if let Some(num_str) = s.strip_suffix("Mi") {
        num_str.parse::<i64>().ok().map(|n| n * 1024 * 1024)
    } else if let Some(num_str) = s.strip_suffix("Gi") {
        num_str.parse::<i64>().ok().map(|n| n * 1024 * 1024 * 1024)
    } else if let Some(num_str) = s.strip_suffix("Ti") {
        num_str
            .parse::<i64>()
            .ok()
            .map(|n| n * 1024 * 1024 * 1024 * 1024)
    } else {
        s.parse::<i64>().ok()
    }
}

fn parse_storage_quantity(s: &str) -> Option<i64> {
    parse_memory_quantity(s)
}

fn is_node_ready(node: &Node) -> bool {
    node.status
        .as_ref()
        .and_then(|s| s.conditions.as_ref())
        .map(|conditions| {
            conditions
                .iter()
                .any(|c| c.type_ == "Ready" && c.status == "True")
        })
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::k8s_client::MockK8sClient;
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
    use std::collections::BTreeMap;

    #[tokio::test]
    async fn node_with_datacenter_label_creates_profile() {
        let client = MockK8sClient::default();
        let mut labels = BTreeMap::new();
        labels.insert("basilica.ai/node-type".to_string(), "gpu".to_string());
        labels.insert("basilica.ai/datacenter".to_string(), "dc-1".to_string());
        labels.insert("basilica.ai/node-id".to_string(), "node-1".to_string());
        labels.insert("basilica.ai/gpu-model".to_string(), "A100".to_string());
        labels.insert("basilica.ai/gpu-count".to_string(), "4".to_string());
        labels.insert("basilica.ai/gpu-memory-gb".to_string(), "80".to_string());
        labels.insert(
            "basilica.ai/driver-version".to_string(),
            "535.104.05".to_string(),
        );
        labels.insert("basilica.ai/cuda-version".to_string(), "12.2".to_string());

        let node = Node {
            metadata: ObjectMeta {
                name: Some("test-node".to_string()),
                uid: Some("test-node-uid".to_string()),
                labels: Some(labels),
                ..Default::default()
            },
            status: Some(k8s_openapi::api::core::v1::NodeStatus {
                conditions: Some(vec![k8s_openapi::api::core::v1::NodeCondition {
                    type_: "Ready".to_string(),
                    status: "True".to_string(),
                    ..Default::default()
                }]),
                capacity: Some({
                    let mut cap = BTreeMap::new();
                    cap.insert(
                        "cpu".to_string(),
                        k8s_openapi::apimachinery::pkg::api::resource::Quantity("32".to_string()),
                    );
                    cap.insert(
                        "memory".to_string(),
                        k8s_openapi::apimachinery::pkg::api::resource::Quantity(
                            "256Gi".to_string(),
                        ),
                    );
                    cap.insert(
                        "ephemeral-storage".to_string(),
                        k8s_openapi::apimachinery::pkg::api::resource::Quantity(
                            "2000Gi".to_string(),
                        ),
                    );
                    cap
                }),
                ..Default::default()
            }),
            ..Default::default()
        };

        let ctrl = NodeProfileController::new(client.clone());
        ctrl.reconcile(&node).await.unwrap();

        let profile = client
            .get_node_profile("basilica-system", "node-1")
            .await
            .unwrap();
        assert_eq!(profile.spec.provider, "datacenter");
        assert_eq!(profile.spec.region, "dc-1");
        assert_eq!(profile.spec.gpu.model, "A100");
        assert_eq!(profile.spec.gpu.count, 4);
        assert_eq!(profile.spec.gpu.memory_gb, 80);
        assert_eq!(profile.spec.cpu.cores, 32);
        assert_eq!(profile.spec.memory_gb, 256);
        assert_eq!(profile.spec.storage_gb, 2000);
    }

    #[tokio::test]
    async fn node_without_datacenter_label_skipped() {
        let client = MockK8sClient::default();
        let node = Node {
            metadata: ObjectMeta {
                name: Some("test-node".to_string()),
                labels: Some(BTreeMap::new()),
                ..Default::default()
            },
            status: Some(k8s_openapi::api::core::v1::NodeStatus {
                conditions: Some(vec![k8s_openapi::api::core::v1::NodeCondition {
                    type_: "Ready".to_string(),
                    status: "True".to_string(),
                    ..Default::default()
                }]),
                ..Default::default()
            }),
            ..Default::default()
        };

        let ctrl = NodeProfileController::new(client.clone());
        ctrl.reconcile(&node).await.unwrap();

        assert!(client
            .get_node_profile("basilica-system", "test-node")
            .await
            .is_err());
    }

    #[tokio::test]
    async fn node_not_ready_skipped() {
        let client = MockK8sClient::default();
        let mut labels = BTreeMap::new();
        labels.insert("basilica.ai/node-type".to_string(), "gpu".to_string());
        labels.insert("basilica.ai/datacenter".to_string(), "dc-1".to_string());
        labels.insert("basilica.ai/gpu-model".to_string(), "A100".to_string());
        labels.insert("basilica.ai/gpu-count".to_string(), "4".to_string());
        labels.insert("basilica.ai/gpu-memory-gb".to_string(), "80".to_string());
        labels.insert(
            "basilica.ai/driver-version".to_string(),
            "535.104.05".to_string(),
        );
        labels.insert("basilica.ai/cuda-version".to_string(), "12.2".to_string());

        let node = Node {
            metadata: ObjectMeta {
                name: Some("test-node".to_string()),
                uid: Some("test-node-uid-2".to_string()),
                labels: Some(labels),
                ..Default::default()
            },
            status: Some(k8s_openapi::api::core::v1::NodeStatus {
                conditions: Some(vec![k8s_openapi::api::core::v1::NodeCondition {
                    type_: "Ready".to_string(),
                    status: "False".to_string(),
                    ..Default::default()
                }]),
                ..Default::default()
            }),
            ..Default::default()
        };

        let ctrl = NodeProfileController::new(client.clone());
        ctrl.reconcile(&node).await.unwrap();

        assert!(client
            .get_node_profile("basilica-system", "test-node")
            .await
            .is_err());
    }
}
