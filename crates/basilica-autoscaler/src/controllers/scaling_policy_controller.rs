use std::sync::Arc;

use chrono::Utc;
use kube::ResourceExt;
use tracing::{debug, info};

use crate::api::SecureCloudApi;
use crate::crd::{
    HealthCheckConfig, MetricsSnapshot, NodePool, NodePoolMode, NodePoolPhase, NodePoolSpec,
    ScalingPolicy, ScalingPolicyCondition, ScalingPolicySpec, ScalingPolicyStatus,
    SecureCloudConfig, WireGuardConfig,
};
use crate::error::{AutoscalerError, Result};
use crate::metrics::AutoscalerMetrics;

use super::k8s_client::AutoscalerK8sClient;

/// ScalingPolicy controller manages automatic scaling decisions
pub struct ScalingPolicyController<K, A>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
{
    k8s: Arc<K>,
    #[allow(dead_code)]
    api: Arc<A>,
    metrics: Arc<AutoscalerMetrics>,
}

impl<K, A> Clone for ScalingPolicyController<K, A>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
{
    fn clone(&self) -> Self {
        Self {
            k8s: Arc::clone(&self.k8s),
            api: Arc::clone(&self.api),
            metrics: Arc::clone(&self.metrics),
        }
    }
}

impl<K, A> ScalingPolicyController<K, A>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
{
    pub fn new(k8s: Arc<K>, api: Arc<A>, metrics: Arc<AutoscalerMetrics>) -> Self {
        Self { k8s, api, metrics }
    }

    /// Main reconciliation entry point
    pub async fn reconcile(&self, ns: &str, policy: &ScalingPolicy) -> Result<()> {
        let name = policy.name_any();
        info!(namespace = %ns, policy = %name, "Reconciling ScalingPolicy");

        if !policy.spec.enabled {
            debug!(policy = %name, "Policy is disabled, skipping");
            return Ok(());
        }

        let mut status = policy.status.clone().unwrap_or_default();

        // Collect metrics
        let metrics_snapshot = self.collect_metrics(ns).await?;
        status.metrics = Some(metrics_snapshot.clone());
        status.last_evaluation_time = Some(Utc::now());

        // Get current node pools managed by this policy
        let node_pools = self.get_managed_node_pools(ns, &name).await?;
        let current_nodes = node_pools.len() as u32;
        status.current_nodes = current_nodes;

        // Evaluate scaling decision
        let decision =
            self.evaluate_scaling(&policy.spec, &metrics_snapshot, current_nodes, &status);

        match decision {
            ScalingDecision::ScaleUp(count) => {
                if self.can_scale_up(&status, &policy.spec.scale_up) {
                    // Get resourceVersion for optimistic locking
                    let resource_version = policy
                        .metadata
                        .resource_version
                        .as_deref()
                        .unwrap_or_default();

                    // Atomically increment pending_scale_up to prevent race conditions
                    // If another reconcile beat us, this returns false and we skip
                    let acquired = self
                        .k8s
                        .try_increment_pending_scale_up(
                            ns,
                            &name,
                            resource_version,
                            status.pending_scale_up,
                            count,
                        )
                        .await?;

                    if !acquired {
                        debug!(policy = %name, "Scale-up conflict, another reconcile is handling it");
                        return Ok(());
                    }

                    info!(policy = %name, count = %count, "Scaling up");
                    // Note: If scale_up fails, pending_scale_up remains incremented.
                    // This is by design - the counter is decremented when NodePools
                    // reach Ready or Failed state. Partial failures are handled
                    // by the NodePool controller, not here.
                    self.scale_up(ns, &name, policy, count).await?;

                    status.last_scale_up_time = Some(Utc::now());
                    status.pending_scale_up += count;
                    add_condition(
                        &mut status,
                        "Scaling",
                        "True",
                        "ScaleUp",
                        &format!("Scaling up by {} nodes", count),
                    );
                    self.metrics.record_scale_event(&name, "scale_up", count);
                } else {
                    debug!(policy = %name, "Scale up blocked by cooldown");
                }
            }
            ScalingDecision::ScaleDown(count) => {
                if self.can_scale_down(&status, &policy.spec.scale_down) {
                    // Get resourceVersion for optimistic locking
                    let resource_version = policy
                        .metadata
                        .resource_version
                        .as_deref()
                        .unwrap_or_default();

                    // Atomically increment pending_scale_down to prevent race conditions
                    // If another reconcile beat us, this returns false and we skip
                    let acquired = self
                        .k8s
                        .try_increment_pending_scale_down(
                            ns,
                            &name,
                            resource_version,
                            status.pending_scale_down,
                            count,
                        )
                        .await?;

                    if !acquired {
                        debug!(policy = %name, "Scale-down conflict, another reconcile is handling it");
                        return Ok(());
                    }

                    info!(policy = %name, count = %count, "Scaling down");
                    self.scale_down(ns, &node_pools, count).await?;
                    status.last_scale_down_time = Some(Utc::now());
                    status.pending_scale_down = status.pending_scale_down.saturating_add(count);
                    add_condition(
                        &mut status,
                        "Scaling",
                        "True",
                        "ScaleDown",
                        &format!("Scaling down by {} nodes", count),
                    );
                    self.metrics.record_scale_event(&name, "scale_down", count);
                } else {
                    debug!(policy = %name, "Scale down blocked by cooldown");
                }
            }
            ScalingDecision::NoAction => {
                debug!(policy = %name, "No scaling action needed");
                // Note: Do NOT reset pending_scale_up and pending_scale_down here.
                // These counters track in-flight scaling operations initiated in previous
                // reconciliation cycles. They should only be decremented when NodePools
                // reach Ready or Failed state, not when no new scaling is needed.
                add_condition(
                    &mut status,
                    "Scaling",
                    "False",
                    "Stable",
                    "Cluster is operating within desired parameters",
                );
            }
        }

        self.k8s
            .update_scaling_policy_status(ns, &name, status)
            .await
    }

    async fn collect_metrics(&self, ns: &str) -> Result<MetricsSnapshot> {
        let pending_pods = self.k8s.list_pending_pods().await?;
        let pending_gpu_pods = pending_pods.iter().filter(|p| requests_gpu(p)).count() as u32;

        let nodes = self
            .k8s
            .list_nodes_with_label("nvidia.com/gpu", "true")
            .await
            .unwrap_or_default();

        let total_gpu_nodes = nodes.len() as u32;
        let healthy_gpu_nodes = nodes
            .iter()
            .filter(|n| {
                n.status
                    .as_ref()
                    .and_then(|s| s.conditions.as_ref())
                    .map(|conds| {
                        conds
                            .iter()
                            .any(|c| c.type_ == "Ready" && c.status == "True")
                    })
                    .unwrap_or(false)
            })
            .count() as u32;

        // Get managed node pools to determine idle count
        let node_pools = self.k8s.list_node_pools(ns).await.unwrap_or_default();
        let idle_nodes = node_pools
            .iter()
            .filter(|p| {
                p.status
                    .as_ref()
                    .and_then(|s| s.phase.as_ref())
                    .map(|phase| *phase == NodePoolPhase::Ready)
                    .unwrap_or(false)
            })
            .count() as u32;

        Ok(MetricsSnapshot {
            pending_gpu_pods,
            total_gpu_nodes,
            healthy_gpu_nodes,
            average_gpu_utilization: None, // Requires prometheus/DCGM integration
            idle_nodes,
        })
    }

    fn evaluate_scaling(
        &self,
        spec: &ScalingPolicySpec,
        metrics: &MetricsSnapshot,
        current_nodes: u32,
        _status: &ScalingPolicyStatus,
    ) -> ScalingDecision {
        // Check scale up: pending GPU pods exceed threshold
        if metrics.pending_gpu_pods >= spec.scale_up.pending_pod_threshold {
            let desired = (current_nodes + spec.scale_up.increment).min(spec.max_nodes);
            if desired > current_nodes {
                return ScalingDecision::ScaleUp(desired - current_nodes);
            }
        }

        // Check scale down: low utilization and above minimum
        // Only trigger utilization-based scale-down when metrics are available
        if current_nodes > spec.min_nodes {
            if let Some(utilization) = metrics.average_gpu_utilization {
                if utilization < spec.scale_down.gpu_utilization_threshold {
                    let desired = current_nodes
                        .saturating_sub(spec.scale_down.decrement)
                        .max(spec.min_nodes);
                    if desired < current_nodes {
                        return ScalingDecision::ScaleDown(current_nodes - desired);
                    }
                }
            }
            // Note: scale-down is disabled when GPU utilization metrics are unavailable
        }

        ScalingDecision::NoAction
    }

    fn can_scale_up(
        &self,
        status: &ScalingPolicyStatus,
        config: &crate::crd::ScaleUpConfig,
    ) -> bool {
        if let Some(last_scale) = &status.last_scale_up_time {
            let elapsed = Utc::now().signed_duration_since(*last_scale);
            if elapsed.num_seconds() < config.cooldown_seconds as i64 {
                return false;
            }
        }
        true
    }

    fn can_scale_down(
        &self,
        status: &ScalingPolicyStatus,
        config: &crate::crd::ScaleDownConfig,
    ) -> bool {
        if let Some(last_scale) = &status.last_scale_down_time {
            let elapsed = Utc::now().signed_duration_since(*last_scale);
            if elapsed.num_seconds() < config.cooldown_seconds as i64 {
                return false;
            }
        }
        true
    }

    async fn get_managed_node_pools(&self, ns: &str, policy_name: &str) -> Result<Vec<NodePool>> {
        let all_pools = self.k8s.list_node_pools(ns).await?;
        Ok(all_pools
            .into_iter()
            .filter(|p| {
                p.metadata
                    .labels
                    .as_ref()
                    .and_then(|l| l.get("basilica.ai/scaling-policy"))
                    .map(|v| v == policy_name)
                    .unwrap_or(false)
            })
            .collect())
    }

    async fn scale_up(
        &self,
        ns: &str,
        policy_name: &str,
        policy: &ScalingPolicy,
        count: u32,
    ) -> Result<()> {
        let template = policy.spec.node_template.as_ref().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration("Missing node_template in policy".to_string())
        })?;

        for i in 0..count {
            let pool_name = format!(
                "{}-{}-{}",
                policy_name,
                Utc::now().format("%Y%m%d%H%M%S"),
                i
            );

            let node_pool =
                self.create_node_pool_from_template(ns, &pool_name, policy_name, template)?;
            info!(pool = %pool_name, "Creating NodePool for scale-up");

            self.k8s.create_node_pool(ns, node_pool).await?;
        }

        Ok(())
    }

    async fn scale_down(&self, ns: &str, node_pools: &[NodePool], count: u32) -> Result<()> {
        // Select nodes to remove (prefer newest, Ready nodes first)
        let mut candidates: Vec<_> = node_pools
            .iter()
            .filter(|p| {
                p.status
                    .as_ref()
                    .and_then(|s| s.phase.as_ref())
                    .map(|phase| *phase == NodePoolPhase::Ready)
                    .unwrap_or(false)
            })
            .collect();

        // Sort by creation time (newest first for removal)
        candidates.sort_by(|a, b| {
            let a_time = a.metadata.creation_timestamp.as_ref();
            let b_time = b.metadata.creation_timestamp.as_ref();
            b_time.cmp(&a_time)
        });

        for pool in candidates.into_iter().take(count as usize) {
            let pool_name = pool.name_any();
            info!(pool = %pool_name, "Initiating drain for scale-down");

            if let Some(mut status) = pool.status.clone() {
                status.phase = Some(NodePoolPhase::Draining);
                status.phase_entered_at = Some(Utc::now());
                self.k8s
                    .update_node_pool_status(ns, &pool_name, status)
                    .await?;
            }
        }

        Ok(())
    }

    fn create_node_pool_from_template(
        &self,
        _ns: &str,
        pool_name: &str,
        policy_name: &str,
        template: &crate::crd::NodeTemplate,
    ) -> Result<NodePool> {
        // Build labels for the new NodePool
        let mut labels = std::collections::BTreeMap::new();
        labels.insert(
            "basilica.ai/scaling-policy".to_string(),
            policy_name.to_string(),
        );
        labels.insert(
            "basilica.ai/managed-by".to_string(),
            "autoscaler".to_string(),
        );

        // Build SecureCloudConfig from template
        let secure_cloud = template.secure_cloud.as_ref().map(|sc| SecureCloudConfig {
            offering_id: sc.offering_id.clone(),
            ssh_key_id: sc.ssh_key_id.clone(),
            ssh_key_secret_ref: sc.ssh_key_secret_ref.clone(),
        });

        let spec = NodePoolSpec {
            mode: NodePoolMode::Dynamic,
            ssh: None, // Dynamic mode uses secure_cloud
            secure_cloud,
            k3s: template.k3s.clone(),
            wireguard: WireGuardConfig::default(),
            health_check: HealthCheckConfig::default(),
            lifecycle: template.lifecycle.clone(),
            node_id: None,
            datacenter_id: None,
            node_password: None,
            adopt_existing: false,
        };

        let mut node_pool = NodePool::new(pool_name, spec);
        node_pool.metadata.labels = Some(labels);

        Ok(node_pool)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ScalingDecision {
    ScaleUp(u32),
    ScaleDown(u32),
    NoAction,
}

fn requests_gpu(pod: &k8s_openapi::api::core::v1::Pod) -> bool {
    let spec = match &pod.spec {
        Some(s) => s,
        None => return false,
    };

    // Check all regular containers
    let has_gpu_container = spec.containers.iter().any(|c| {
        c.resources
            .as_ref()
            .and_then(|r| r.requests.as_ref())
            .map(|req| req.contains_key("nvidia.com/gpu"))
            .unwrap_or(false)
    });

    if has_gpu_container {
        return true;
    }

    // Also check init containers
    if let Some(init_containers) = &spec.init_containers {
        return init_containers.iter().any(|c| {
            c.resources
                .as_ref()
                .and_then(|r| r.requests.as_ref())
                .map(|req| req.contains_key("nvidia.com/gpu"))
                .unwrap_or(false)
        });
    }

    false
}

fn add_condition(
    status: &mut ScalingPolicyStatus,
    type_: &str,
    status_val: &str,
    reason: &str,
    message: &str,
) {
    let condition = ScalingPolicyCondition {
        type_: type_.to_string(),
        status: status_val.to_string(),
        reason: Some(reason.to_string()),
        message: Some(message.to_string()),
        last_transition_time: Some(Utc::now()),
    };

    if let Some(existing) = status.conditions.iter_mut().find(|c| c.type_ == type_) {
        *existing = condition;
    } else {
        status.conditions.push(condition);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scaling_decision_equality() {
        assert_eq!(ScalingDecision::NoAction, ScalingDecision::NoAction);
        assert_eq!(ScalingDecision::ScaleUp(1), ScalingDecision::ScaleUp(1));
        assert_ne!(ScalingDecision::ScaleUp(1), ScalingDecision::ScaleDown(1));
    }
}
