use async_trait::async_trait;
use k8s_openapi::api::core::v1::{Node, Pod, Secret};
use std::collections::BTreeMap;

use crate::crd::{NodePool, NodePoolStatus, ScalingPolicy, ScalingPolicyStatus};
use crate::error::Result;

/// Kubernetes client trait for autoscaler operations
#[async_trait]
pub trait AutoscalerK8sClient: Send + Sync {
    // NodePool CRD operations
    async fn get_node_pool(&self, ns: &str, name: &str) -> Result<NodePool>;
    async fn list_node_pools(&self, ns: &str) -> Result<Vec<NodePool>>;
    async fn create_node_pool(&self, ns: &str, node_pool: NodePool) -> Result<NodePool>;
    async fn update_node_pool_status(
        &self,
        ns: &str,
        name: &str,
        status: NodePoolStatus,
    ) -> Result<()>;
    async fn add_node_pool_finalizer(&self, ns: &str, name: &str) -> Result<()>;
    async fn remove_node_pool_finalizer(&self, ns: &str, name: &str) -> Result<()>;

    // ScalingPolicy CRD operations
    async fn get_scaling_policy(&self, ns: &str, name: &str) -> Result<ScalingPolicy>;
    async fn list_scaling_policies(&self, ns: &str) -> Result<Vec<ScalingPolicy>>;
    async fn update_scaling_policy_status(
        &self,
        ns: &str,
        name: &str,
        status: ScalingPolicyStatus,
    ) -> Result<()>;

    /// Atomically increment pending_scale_up using optimistic locking.
    /// Returns Ok(true) if successful, Ok(false) if conflict (another reconcile won).
    async fn try_increment_pending_scale_up(
        &self,
        ns: &str,
        name: &str,
        resource_version: &str,
        current_value: u32,
        increment: u32,
    ) -> Result<bool>;

    // Node operations
    async fn get_node(&self, name: &str) -> Result<Node>;
    async fn list_nodes(&self) -> Result<Vec<Node>>;
    async fn list_nodes_with_label(&self, key: &str, value: &str) -> Result<Vec<Node>>;
    async fn find_node_by_node_id(&self, node_id: &str) -> Result<Option<Node>>;
    async fn cordon_node(&self, name: &str) -> Result<()>;
    async fn uncordon_node(&self, name: &str) -> Result<()>;
    async fn delete_node(&self, name: &str) -> Result<()>;
    async fn add_node_labels(&self, name: &str, labels: &BTreeMap<String, String>) -> Result<()>;
    async fn remove_node_labels(&self, name: &str, keys: &[String]) -> Result<()>;

    // Pod operations
    async fn list_pods_on_node(&self, node_name: &str) -> Result<Vec<Pod>>;
    async fn list_pending_pods(&self) -> Result<Vec<Pod>>;
    async fn evict_pod(&self, ns: &str, name: &str, grace_seconds: Option<i64>) -> Result<bool>;

    // Secret operations
    async fn get_secret(&self, ns: &str, name: &str) -> Result<Secret>;
}

/// Real Kubernetes client implementation
#[derive(Clone)]
pub struct KubeClient {
    client: kube::Client,
}

impl KubeClient {
    pub async fn try_default() -> Result<Self> {
        let client = kube::Client::try_default().await?;
        Ok(Self { client })
    }

    pub fn inner(&self) -> &kube::Client {
        &self.client
    }
}

#[async_trait]
impl AutoscalerK8sClient for KubeClient {
    async fn get_node_pool(&self, ns: &str, name: &str) -> Result<NodePool> {
        use kube::Api;
        let api: Api<NodePool> = Api::namespaced(self.client.clone(), ns);
        api.get(name).await.map_err(Into::into)
    }

    async fn list_node_pools(&self, ns: &str) -> Result<Vec<NodePool>> {
        use kube::api::ListParams;
        use kube::Api;
        let api: Api<NodePool> = Api::namespaced(self.client.clone(), ns);
        let list = api.list(&ListParams::default()).await?;
        Ok(list.items)
    }

    async fn create_node_pool(&self, ns: &str, node_pool: NodePool) -> Result<NodePool> {
        use kube::api::PostParams;
        use kube::Api;
        let api: Api<NodePool> = Api::namespaced(self.client.clone(), ns);
        api.create(&PostParams::default(), &node_pool)
            .await
            .map_err(Into::into)
    }

    async fn update_node_pool_status(
        &self,
        ns: &str,
        name: &str,
        status: NodePoolStatus,
    ) -> Result<()> {
        use kube::api::{Api, Patch, PatchParams};
        let api: Api<NodePool> = Api::namespaced(self.client.clone(), ns);
        let patch = serde_json::json!({ "status": status });
        api.patch_status(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await?;
        Ok(())
    }

    async fn add_node_pool_finalizer(&self, ns: &str, name: &str) -> Result<()> {
        use crate::crd::FINALIZER;
        use kube::api::{Api, Patch, PatchParams};
        let api: Api<NodePool> = Api::namespaced(self.client.clone(), ns);
        let patch = serde_json::json!({
            "metadata": {
                "finalizers": [FINALIZER]
            }
        });
        api.patch(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await?;
        Ok(())
    }

    async fn remove_node_pool_finalizer(&self, ns: &str, name: &str) -> Result<()> {
        use kube::api::{Api, Patch, PatchParams};
        let api: Api<NodePool> = Api::namespaced(self.client.clone(), ns);
        let patch = serde_json::json!({
            "metadata": {
                "finalizers": null
            }
        });
        api.patch(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await?;
        Ok(())
    }

    async fn get_scaling_policy(&self, ns: &str, name: &str) -> Result<ScalingPolicy> {
        use kube::Api;
        let api: Api<ScalingPolicy> = Api::namespaced(self.client.clone(), ns);
        api.get(name).await.map_err(Into::into)
    }

    async fn list_scaling_policies(&self, ns: &str) -> Result<Vec<ScalingPolicy>> {
        use kube::api::ListParams;
        use kube::Api;
        let api: Api<ScalingPolicy> = Api::namespaced(self.client.clone(), ns);
        let list = api.list(&ListParams::default()).await?;
        Ok(list.items)
    }

    async fn update_scaling_policy_status(
        &self,
        ns: &str,
        name: &str,
        status: ScalingPolicyStatus,
    ) -> Result<()> {
        use kube::api::{Api, Patch, PatchParams};
        let api: Api<ScalingPolicy> = Api::namespaced(self.client.clone(), ns);
        let patch = serde_json::json!({ "status": status });
        api.patch_status(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await?;
        Ok(())
    }

    async fn try_increment_pending_scale_up(
        &self,
        ns: &str,
        name: &str,
        resource_version: &str,
        current_value: u32,
        increment: u32,
    ) -> Result<bool> {
        use kube::api::{Api, Patch, PatchParams};

        let api: Api<ScalingPolicy> = Api::namespaced(self.client.clone(), ns);

        // Use Strategic Merge Patch with resourceVersion precondition
        let patch = serde_json::json!({
            "metadata": {
                "resourceVersion": resource_version
            },
            "status": {
                "pendingScaleUp": current_value + increment
            }
        });

        match api
            .patch_status(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await
        {
            Ok(_) => Ok(true),
            Err(kube::Error::Api(ae)) if ae.code == 409 => {
                // Conflict - another reconcile modified the resource
                Ok(false)
            }
            Err(e) => Err(e.into()),
        }
    }

    async fn get_node(&self, name: &str) -> Result<Node> {
        use kube::Api;
        let api: Api<Node> = Api::all(self.client.clone());
        api.get(name).await.map_err(Into::into)
    }

    async fn list_nodes(&self) -> Result<Vec<Node>> {
        use kube::api::ListParams;
        use kube::Api;
        let api: Api<Node> = Api::all(self.client.clone());
        let list = api.list(&ListParams::default()).await?;
        Ok(list.items)
    }

    async fn list_nodes_with_label(&self, key: &str, value: &str) -> Result<Vec<Node>> {
        use kube::api::ListParams;
        use kube::Api;
        let api: Api<Node> = Api::all(self.client.clone());
        let lp = ListParams::default().labels(&format!("{}={}", key, value));
        let list = api.list(&lp).await?;
        Ok(list.items)
    }

    async fn find_node_by_node_id(&self, node_id: &str) -> Result<Option<Node>> {
        let nodes = self
            .list_nodes_with_label("basilica.ai/node-id", node_id)
            .await?;
        Ok(nodes.into_iter().next())
    }

    async fn cordon_node(&self, name: &str) -> Result<()> {
        use kube::api::{Api, Patch, PatchParams};
        let api: Api<Node> = Api::all(self.client.clone());
        let patch = serde_json::json!({ "spec": { "unschedulable": true } });
        api.patch(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await?;
        Ok(())
    }

    async fn uncordon_node(&self, name: &str) -> Result<()> {
        use kube::api::{Api, Patch, PatchParams};
        let api: Api<Node> = Api::all(self.client.clone());
        let patch = serde_json::json!({ "spec": { "unschedulable": false } });
        api.patch(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await?;
        Ok(())
    }

    async fn delete_node(&self, name: &str) -> Result<()> {
        use kube::api::{Api, DeleteParams};
        let api: Api<Node> = Api::all(self.client.clone());
        api.delete(name, &DeleteParams::default()).await?;
        Ok(())
    }

    async fn add_node_labels(&self, name: &str, labels: &BTreeMap<String, String>) -> Result<()> {
        use kube::api::{Api, Patch, PatchParams};
        let api: Api<Node> = Api::all(self.client.clone());
        let labels_json: serde_json::Map<String, serde_json::Value> = labels
            .iter()
            .map(|(k, v)| (k.clone(), serde_json::Value::String(v.clone())))
            .collect();
        let patch = serde_json::json!({ "metadata": { "labels": labels_json } });
        api.patch(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await?;
        Ok(())
    }

    async fn remove_node_labels(&self, name: &str, keys: &[String]) -> Result<()> {
        use kube::api::{Api, Patch, PatchParams};
        let api: Api<Node> = Api::all(self.client.clone());
        let labels_json: serde_json::Map<String, serde_json::Value> = keys
            .iter()
            .map(|k| (k.clone(), serde_json::Value::Null))
            .collect();
        let patch = serde_json::json!({ "metadata": { "labels": labels_json } });
        api.patch(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await?;
        Ok(())
    }

    async fn list_pods_on_node(&self, node_name: &str) -> Result<Vec<Pod>> {
        use kube::api::ListParams;
        use kube::Api;
        let api: Api<Pod> = Api::all(self.client.clone());
        let lp = ListParams::default().fields(&format!("spec.nodeName={}", node_name));
        let list = api.list(&lp).await?;
        Ok(list.items)
    }

    async fn list_pending_pods(&self) -> Result<Vec<Pod>> {
        use kube::api::ListParams;
        use kube::Api;
        let api: Api<Pod> = Api::all(self.client.clone());
        let lp = ListParams::default().fields("status.phase=Pending");
        let list = api.list(&lp).await?;
        Ok(list.items)
    }

    async fn evict_pod(&self, ns: &str, name: &str, grace_seconds: Option<i64>) -> Result<bool> {
        use k8s_openapi::api::policy::v1::Eviction;
        use k8s_openapi::apimachinery::pkg::apis::meta::v1::{DeleteOptions, ObjectMeta};
        use kube::api::{Api, PostParams};

        let api: Api<Pod> = Api::namespaced(self.client.clone(), ns);
        let eviction = Eviction {
            metadata: ObjectMeta {
                name: Some(name.to_string()),
                namespace: Some(ns.to_string()),
                ..Default::default()
            },
            delete_options: Some(DeleteOptions {
                grace_period_seconds: grace_seconds,
                ..Default::default()
            }),
        };
        let body = serde_json::to_vec(&eviction)?;
        match api
            .create_subresource::<k8s_openapi::apimachinery::pkg::apis::meta::v1::Status>(
                "eviction",
                name,
                &PostParams::default(),
                body,
            )
            .await
        {
            Ok(_) => Ok(true),
            Err(kube::Error::Api(ae)) if ae.code == 429 => Ok(false),
            Err(e) => Err(e.into()),
        }
    }

    async fn get_secret(&self, ns: &str, name: &str) -> Result<Secret> {
        use kube::Api;
        let api: Api<Secret> = Api::namespaced(self.client.clone(), ns);
        api.get(name).await.map_err(Into::into)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kube_client_is_send_sync() {
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<KubeClient>();
    }
}
