use std::collections::BTreeMap;
use std::sync::Arc;

use chrono::Utc;
use k8s_openapi::api::core::v1::Pod;
use kube::ResourceExt;
use tracing::{debug, info, warn};

use crate::api::SecureCloudApi;
use crate::config::PendingPodFilterConfig;
use crate::crd::{
    HealthCheckConfig, MetricsSnapshot, NodePool, NodePoolMode, NodePoolPhase, NodePoolSpec,
    ScalingPolicy, ScalingPolicyCondition, ScalingPolicySpec, ScalingPolicyStatus,
    SecureCloudConfig, WarmPoolConfig, WireGuardConfig,
};
use crate::error::{AutoscalerError, Result};
use crate::metrics::AutoscalerMetrics;
use crate::offering_matcher::{
    calculate_nodes_needed, filter_pods_by_age, filter_pods_from_failing_deployments,
    filter_pods_with_stale_affinity, group_pending_pods_by_requirements, has_gpu_node_affinity,
    GpuRequirements, OfferingConstraints, OfferingSelector, PendingGpuPod,
};
use crate::warm_pool::{
    build_warm_pool_status, calculate_vram_metrics, calculate_warm_pool_target,
    evaluate_warm_pool_scaling, WarmPoolDecision,
};

use super::k8s_client::AutoscalerK8sClient;

/// ScalingPolicy controller manages automatic scaling decisions
pub struct ScalingPolicyController<K, A, S>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
    S: OfferingSelector,
{
    k8s: Arc<K>,
    api: Arc<A>,
    offering_selector: Arc<S>,
    metrics: Arc<AutoscalerMetrics>,
    pending_pod_filter: PendingPodFilterConfig,
}

impl<K, A, S> Clone for ScalingPolicyController<K, A, S>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
    S: OfferingSelector,
{
    fn clone(&self) -> Self {
        Self {
            k8s: Arc::clone(&self.k8s),
            api: Arc::clone(&self.api),
            offering_selector: Arc::clone(&self.offering_selector),
            metrics: Arc::clone(&self.metrics),
            pending_pod_filter: self.pending_pod_filter.clone(),
        }
    }
}

impl<K, A, S> ScalingPolicyController<K, A, S>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
    S: OfferingSelector,
{
    pub fn new(
        k8s: Arc<K>,
        api: Arc<A>,
        offering_selector: Arc<S>,
        metrics: Arc<AutoscalerMetrics>,
    ) -> Self {
        Self {
            k8s,
            api,
            offering_selector,
            metrics,
            pending_pod_filter: PendingPodFilterConfig::default(),
        }
    }

    pub fn with_pending_pod_filter(
        k8s: Arc<K>,
        api: Arc<A>,
        offering_selector: Arc<S>,
        metrics: Arc<AutoscalerMetrics>,
        pending_pod_filter: PendingPodFilterConfig,
    ) -> Self {
        Self {
            k8s,
            api,
            offering_selector,
            metrics,
            pending_pod_filter,
        }
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

        // Collect metrics and pending pods
        let (metrics_snapshot, pending_gpu_pods) = self.collect_metrics(ns).await?;
        status.metrics = Some(metrics_snapshot.clone());
        status.last_evaluation_time = Some(Utc::now());

        // Get current node pools managed by this policy
        let node_pools = self.get_managed_node_pools(ns, &name).await?;
        let current_nodes = node_pools.len() as u32;
        status.current_nodes = current_nodes;

        // Protect against stuck pending_scale_up: count NodePools in provisioning phases
        let provisioning_count = node_pools
            .iter()
            .filter(|p| {
                p.status
                    .as_ref()
                    .and_then(|s| s.phase.as_ref())
                    .map(|phase| {
                        matches!(
                            phase,
                            NodePoolPhase::Pending
                                | NodePoolPhase::Provisioning
                                | NodePoolPhase::Configuring
                                | NodePoolPhase::InstallingWireGuard
                                | NodePoolPhase::ValidatingNetwork
                                | NodePoolPhase::JoiningCluster
                                | NodePoolPhase::WaitingForNode
                        )
                    })
                    .unwrap_or(false)
            })
            .count() as u32;

        // Reset pending_scale_up if it's higher than actual provisioning count
        if status.pending_scale_up > provisioning_count {
            warn!(
                policy = %name,
                pending = status.pending_scale_up,
                actual = provisioning_count,
                "Correcting stuck pending_scale_up counter"
            );
            status.pending_scale_up = provisioning_count;
        }

        // Evaluate scaling decision
        let decision =
            self.evaluate_scaling(&policy.spec, &metrics_snapshot, current_nodes, &status);

        match decision {
            ScalingDecision::ScaleUp(count) => {
                // Block scale-up if there are NodePools already provisioning
                // This prevents creating duplicate VMs while waiting for node registration
                if provisioning_count > 0 {
                    debug!(
                        policy = %name,
                        provisioning = provisioning_count,
                        "Scale-up blocked: waiting for {} NodePool(s) to finish provisioning",
                        provisioning_count
                    );
                    add_condition(
                        &mut status,
                        "Scaling",
                        "False",
                        "WaitingForProvisioning",
                        &format!(
                            "Waiting for {} NodePool(s) to finish provisioning",
                            provisioning_count
                        ),
                    );
                    self.k8s
                        .update_scaling_policy_status(ns, &name, status)
                        .await?;
                    return Ok(());
                }

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
                    match self
                        .scale_up(ns, &name, policy, count, &pending_gpu_pods)
                        .await
                    {
                        Ok(()) => {
                            status.last_scale_up_time = Some(Utc::now());
                            status.pending_scale_up = status.pending_scale_up.saturating_add(count);
                            add_condition(
                                &mut status,
                                "Scaling",
                                "True",
                                "ScaleUp",
                                &format!("Scaling up by {} nodes", count),
                            );
                            // Clear OfferingAvailability condition on success
                            add_condition(
                                &mut status,
                                "OfferingAvailability",
                                "True",
                                "OfferingFound",
                                "GPU offering available for pending workloads",
                            );
                            self.metrics.record_scale_event(&name, "scale_up", count);
                        }
                        Err(AutoscalerError::NoMatchingOffering {
                            gpu_count,
                            ref models,
                            min_memory_gb,
                        }) => {
                            // Add OfferingAvailability condition for diagnostics
                            add_condition(
                                &mut status,
                                "OfferingAvailability",
                                "False",
                                "NoMatchingOffering",
                                &format!(
                                    "No GPU offering found matching {} GPU(s){}{}",
                                    gpu_count,
                                    if models.is_empty() {
                                        String::new()
                                    } else {
                                        format!(", models: {:?}", models)
                                    },
                                    min_memory_gb
                                        .map(|m| format!(", min memory: {}GB", m))
                                        .unwrap_or_default()
                                ),
                            );
                            // Update status before returning error
                            self.k8s
                                .update_scaling_policy_status(ns, &name, status)
                                .await?;
                            return Err(AutoscalerError::NoMatchingOffering {
                                gpu_count,
                                models: models.clone(),
                                min_memory_gb,
                            });
                        }
                        Err(e) => return Err(e),
                    }
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
                debug!(policy = %name, "No reactive scaling action needed");

                // Clear stale OfferingAvailability condition when no pending GPU pods
                if metrics_snapshot.pending_gpu_pods == 0 {
                    add_condition(
                        &mut status,
                        "OfferingAvailability",
                        "True",
                        "NoGpuDemand",
                        "No pending GPU workloads",
                    );
                }

                // Evaluate warm pool scaling when no reactive scaling is needed
                if let Some(warm_pool_config) = &policy.spec.warm_pool {
                    if warm_pool_config.enabled {
                        let warm_pool_result = self
                            .evaluate_and_apply_warm_pool(
                                ns,
                                &name,
                                policy,
                                warm_pool_config,
                                &node_pools,
                                metrics_snapshot.pending_gpu_pods,
                                &mut status,
                            )
                            .await;

                        if let Err(e) = warm_pool_result {
                            warn!(policy = %name, error = %e, "Warm pool evaluation failed");
                            add_condition(
                                &mut status,
                                "WarmPool",
                                "False",
                                "EvaluationFailed",
                                &format!("Warm pool evaluation failed: {}", e),
                            );
                        }
                    }
                }

                // Evaluate idle-time-based scale-down for non-warm-pool nodes
                if self.can_scale_down(&status, &policy.spec.scale_down) {
                    let pods_by_node = self.collect_pods_by_node(&node_pools).await;
                    let idle_nodes = Self::find_idle_nodes_for_scale_down(
                        &node_pools,
                        &pods_by_node,
                        policy.spec.scale_down.idle_timeout_seconds,
                    );

                    if !idle_nodes.is_empty() {
                        let count = idle_nodes
                            .len()
                            .min(policy.spec.scale_down.decrement as usize)
                            as u32;
                        info!(
                            policy = %name,
                            idle_nodes = ?idle_nodes,
                            count = count,
                            "Idle-time scale-down triggered"
                        );

                        // Get non-warm-pool node pools for scale-down
                        let non_warm_pools: Vec<_> = node_pools
                            .iter()
                            .filter(|p| {
                                !p.metadata
                                    .labels
                                    .as_ref()
                                    .and_then(|l| l.get("basilica.ai/warm-pool"))
                                    .map(|v| v == "true")
                                    .unwrap_or(false)
                            })
                            .cloned()
                            .collect();

                        self.scale_down(ns, &non_warm_pools, count).await?;
                        status.last_scale_down_time = Some(Utc::now());
                        add_condition(
                            &mut status,
                            "Scaling",
                            "True",
                            "IdleScaleDown",
                            &format!("Scaling down {} idle node(s)", count),
                        );
                        self.metrics
                            .record_scale_event(&name, "idle_scale_down", count);
                    } else {
                        add_condition(
                            &mut status,
                            "Scaling",
                            "False",
                            "Stable",
                            "Cluster is operating within desired parameters",
                        );
                    }
                } else {
                    add_condition(
                        &mut status,
                        "Scaling",
                        "False",
                        "Stable",
                        "Cluster is operating within desired parameters",
                    );
                }
            }
        }

        self.k8s
            .update_scaling_policy_status(ns, &name, status)
            .await
    }

    /// Evaluate warm pool state and apply scaling decisions.
    #[allow(clippy::too_many_arguments)]
    async fn evaluate_and_apply_warm_pool(
        &self,
        ns: &str,
        policy_name: &str,
        policy: &ScalingPolicy,
        config: &WarmPoolConfig,
        node_pools: &[NodePool],
        pending_gpu_pods: u32,
        status: &mut ScalingPolicyStatus,
    ) -> Result<()> {
        // Collect data for VRAM metrics
        let pods_by_node = self.collect_pods_by_node(node_pools).await;
        let offering_rates = self.get_offering_rates().await;
        let available_offerings = self.get_available_offerings().await;

        // Calculate VRAM metrics
        let vram_metrics = calculate_vram_metrics(node_pools, &pods_by_node, &offering_rates);

        // Calculate target
        let target = calculate_warm_pool_target(config, &available_offerings);

        // Update warm pool status
        let warm_pool_status = build_warm_pool_status(config, &vram_metrics, &target);
        status.warm_pool = Some(warm_pool_status.clone());

        // Record Prometheus metrics
        self.metrics
            .set_warm_pool_metrics(policy_name, &warm_pool_status);

        // Count current warm pool nodes (Ready and Pending)
        let current_warm_nodes = Self::count_warm_pool_nodes(node_pools);
        let pending_warm_nodes = Self::count_pending_warm_pool_nodes(node_pools);

        // Skip warm pool scale-up if there are already pending warm pool nodes
        if pending_warm_nodes > 0 {
            debug!(
                policy = %policy_name,
                pending = pending_warm_nodes,
                ready = current_warm_nodes,
                "Warm pool: skipping evaluation, {} nodes still provisioning",
                pending_warm_nodes
            );
            add_condition(
                status,
                "WarmPool",
                "True",
                "Provisioning",
                &format!(
                    "Warm pool: {} node(s) provisioning, {} ready",
                    pending_warm_nodes, current_warm_nodes
                ),
            );
            return Ok(());
        }

        // Evaluate scaling decision
        let decision = evaluate_warm_pool_scaling(
            config,
            &vram_metrics,
            current_warm_nodes,
            &available_offerings,
            pending_gpu_pods,
        );

        match decision {
            WarmPoolDecision::ScaleUp { count, reason } => {
                info!(
                    policy = %policy_name,
                    count = count,
                    reason = %reason,
                    "Warm pool scale-up triggered"
                );

                // Select offering for warm pool
                let offering = Self::select_warm_pool_offering(&available_offerings, config)
                    .ok_or_else(|| {
                        AutoscalerError::InvalidConfiguration(
                            "No suitable offering for warm pool".to_string(),
                        )
                    })?;

                // Create warm pool nodes
                self.scale_up_with_offering(
                    ns,
                    policy_name,
                    policy,
                    count,
                    &offering.id,
                    true, // is_warm_pool = true
                )
                .await?;

                add_condition(
                    status,
                    "WarmPool",
                    "True",
                    "ScalingUp",
                    &format!("Adding {} warm pool node(s): {}", count, reason),
                );

                self.metrics
                    .record_warm_pool_scale_decision(policy_name, "scale_up");
                self.metrics
                    .record_scale_event(policy_name, "warm_pool_scale_up", count);
            }
            WarmPoolDecision::ScaleDown { count, reason } => {
                info!(
                    policy = %policy_name,
                    count = count,
                    reason = %reason,
                    "Warm pool scale-down triggered"
                );

                // Select warm pool nodes for removal (prefer idle warm pool nodes)
                let warm_nodes: Vec<_> = node_pools
                    .iter()
                    .filter(|p| {
                        p.metadata
                            .labels
                            .as_ref()
                            .and_then(|l| l.get("basilica.ai/warm-pool"))
                            .map(|v| v == "true")
                            .unwrap_or(false)
                    })
                    .collect();

                self.scale_down(
                    ns,
                    &warm_nodes.iter().map(|p| (*p).clone()).collect::<Vec<_>>(),
                    count,
                )
                .await?;

                add_condition(
                    status,
                    "WarmPool",
                    "True",
                    "ScalingDown",
                    &format!("Removing {} warm pool node(s): {}", count, reason),
                );

                self.metrics
                    .record_warm_pool_scale_decision(policy_name, "scale_down");
                self.metrics
                    .record_scale_event(policy_name, "warm_pool_scale_down", count);
            }
            WarmPoolDecision::NoAction => {
                debug!(policy = %policy_name, "Warm pool: no scaling action needed");
                add_condition(
                    status,
                    "WarmPool",
                    "True",
                    "Stable",
                    &format!(
                        "Warm pool stable: {}GB idle / {}GB target",
                        vram_metrics.idle_vram_gb, config.min_idle_vram_gb
                    ),
                );
                add_condition(
                    status,
                    "Scaling",
                    "False",
                    "Stable",
                    "Cluster is operating within desired parameters",
                );
            }
        }

        Ok(())
    }

    async fn collect_metrics(&self, ns: &str) -> Result<(MetricsSnapshot, Vec<Pod>)> {
        let pending_pods = self.k8s.list_pending_pods().await?;
        let pending_gpu_pods: Vec<Pod> = pending_pods.into_iter().filter(requests_gpu).collect();

        let nodes = self
            .k8s
            .list_nodes_with_label("nvidia.com/gpu", "true")
            .await
            .unwrap_or_default();

        // Collect pods from namespaces with pending GPU pods for failing deployment check
        let all_namespace_pods = if self.pending_pod_filter.skip_failing_deployments {
            let namespaces: std::collections::HashSet<_> = pending_gpu_pods
                .iter()
                .filter_map(|p| p.metadata.namespace.as_deref())
                .collect();
            let mut all_pods = Vec::new();
            for ns in namespaces {
                if let Ok(pods) = self.k8s.list_pods_in_namespace(ns).await {
                    all_pods.extend(pods);
                }
            }
            all_pods
        } else {
            Vec::new()
        };

        // Apply pending pod filters to prevent provisioning for stale pods
        let filtered_gpu_pods =
            self.apply_pending_pod_filters(pending_gpu_pods, &nodes, &all_namespace_pods);
        let pending_gpu_count = filtered_gpu_pods.len() as u32;

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

        let metrics = MetricsSnapshot {
            pending_gpu_pods: pending_gpu_count,
            total_gpu_nodes,
            healthy_gpu_nodes,
            average_gpu_utilization: None, // Requires prometheus/DCGM integration
            idle_nodes,
        };

        Ok((metrics, filtered_gpu_pods))
    }

    /// Apply filters to pending GPU pods to exclude:
    /// 1. Pods that have been pending too long (max_pending_age_seconds)
    /// 2. Pods whose normalized GPU model matches an existing node (stale nodeAffinity)
    /// 3. Pods from deployments that have sibling pods in a failing state
    fn apply_pending_pod_filters(
        &self,
        pods: Vec<Pod>,
        gpu_nodes: &[k8s_openapi::api::core::v1::Node],
        all_namespace_pods: &[Pod],
    ) -> Vec<Pod> {
        let initial_count = pods.len();

        // Filter by age
        let after_age_filter =
            filter_pods_by_age(pods, self.pending_pod_filter.max_pending_age_seconds);
        let age_filtered = initial_count - after_age_filter.len();

        // Filter by stale node affinity
        let after_stale_filter = if self.pending_pod_filter.skip_stale_node_affinity {
            filter_pods_with_stale_affinity(after_age_filter, gpu_nodes)
        } else {
            after_age_filter
        };
        let stale_filtered = initial_count - age_filtered - after_stale_filter.len();

        // Filter by failing deployments
        let final_pods = if self.pending_pod_filter.skip_failing_deployments {
            filter_pods_from_failing_deployments(after_stale_filter, all_namespace_pods)
        } else {
            after_stale_filter
        };
        let failing_filtered = initial_count - age_filtered - stale_filtered - final_pods.len();

        if age_filtered > 0 || stale_filtered > 0 || failing_filtered > 0 {
            debug!(
                initial = initial_count,
                age_filtered = age_filtered,
                stale_filtered = stale_filtered,
                failing_filtered = failing_filtered,
                remaining = final_pods.len(),
                "Filtered pending GPU pods"
            );
        }

        final_pods
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
        max_count: u32,
        pending_pods: &[Pod],
    ) -> Result<()> {
        let template = policy.spec.node_template.as_ref().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration("Missing node_template in policy".to_string())
        })?;

        let secure_cloud = template.secure_cloud.as_ref().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration(
                "Missing secure_cloud in node_template".to_string(),
            )
        })?;

        // Static offering mode (reactive scaling - not warm pool)
        if let Some(ref static_offering_id) = secure_cloud.offering_id {
            info!(policy = %policy_name, offering_id = %static_offering_id, count = %max_count, "Using static offering_id");
            return self
                .scale_up_with_offering(
                    ns,
                    policy_name,
                    policy,
                    max_count,
                    static_offering_id,
                    false,
                )
                .await;
        }

        // Dynamic offering selection
        let pod_groups = group_pending_pods_by_requirements(pending_pods);
        if pod_groups.is_empty() {
            debug!(policy = %policy_name, "No pending GPU pods to scale for");
            return Ok(());
        }

        self.ensure_cache_fresh(policy_name).await;
        let effective_constraints = self.build_effective_constraints(policy);

        let (total_created, unmatched) = self
            .process_pod_groups(
                ns,
                policy_name,
                policy,
                max_count,
                &pod_groups,
                effective_constraints.as_ref(),
                pending_pods,
            )
            .await?;

        if total_created == 0 {
            if let Some(req) = unmatched {
                return Err(AutoscalerError::NoMatchingOffering {
                    gpu_count: req.gpu_count,
                    models: req.gpu_models.iter().cloned().collect(),
                    min_memory_gb: req.min_gpu_memory_gb,
                });
            }
        }
        Ok(())
    }

    async fn ensure_cache_fresh(&self, policy_name: &str) {
        if self.offering_selector.is_cache_stale() {
            debug!(policy = %policy_name, "Offering cache is stale, refreshing before scale-up");
            if let Err(e) = self.offering_selector.refresh_cache().await {
                warn!(policy = %policy_name, error = %e, "Failed to refresh offering cache, proceeding with stale data");
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn process_pod_groups(
        &self,
        ns: &str,
        policy_name: &str,
        policy: &ScalingPolicy,
        max_count: u32,
        pod_groups: &std::collections::HashMap<GpuRequirements, Vec<PendingGpuPod>>,
        constraints: Option<&OfferingConstraints>,
        pending_pods: &[Pod],
    ) -> Result<(u32, Option<GpuRequirements>)> {
        let mut total_nodes_created = 0u32;
        let mut last_unmatched: Option<GpuRequirements> = None;

        for (requirements, pods) in pod_groups {
            if total_nodes_created >= max_count {
                break;
            }

            let offering = match self
                .find_offering_with_fallback(
                    ns,
                    policy_name,
                    policy,
                    requirements,
                    pods,
                    constraints,
                    max_count - total_nodes_created,
                )
                .await?
            {
                OfferingResult::Found(o) => o,
                OfferingResult::UsedFallback(count) => {
                    total_nodes_created += count;
                    continue;
                }
                OfferingResult::NotFound => {
                    last_unmatched = Some(requirements.clone());
                    continue;
                }
            };

            let nodes_to_create = self
                .calculate_and_create_nodes(
                    ns,
                    policy_name,
                    policy,
                    requirements,
                    pods,
                    &offering,
                    max_count - total_nodes_created,
                    pending_pods,
                )
                .await?;
            total_nodes_created += nodes_to_create;
        }

        Ok((total_nodes_created, last_unmatched))
    }

    #[allow(clippy::too_many_arguments)]
    async fn find_offering_with_fallback(
        &self,
        ns: &str,
        policy_name: &str,
        policy: &ScalingPolicy,
        requirements: &GpuRequirements,
        pods: &[PendingGpuPod],
        constraints: Option<&OfferingConstraints>,
        remaining: u32,
    ) -> Result<OfferingResult> {
        // Step 1: Try exact match with model requirements
        if let Some(offering) = self
            .offering_selector
            .find_best_offering(requirements, constraints)
            .await?
        {
            return Ok(OfferingResult::Found(offering));
        }

        // Step 2: If allow_model_fallback is enabled, try without model restriction
        let allow_fallback = constraints.map(|c| c.allow_model_fallback).unwrap_or(false);
        if allow_fallback && !requirements.gpu_models.is_empty() {
            let relaxed_requirements = GpuRequirements {
                gpu_count: requirements.gpu_count,
                gpu_models: std::collections::BTreeSet::new(), // No model restriction
                min_gpu_memory_gb: requirements.min_gpu_memory_gb,
            };

            if let Some(offering) = self
                .offering_selector
                .find_best_offering(&relaxed_requirements, constraints)
                .await?
            {
                warn!(
                    policy = %policy_name,
                    requested_models = ?requirements.gpu_models,
                    selected_model = %offering.gpu_type,
                    "No exact model match, using fallback offering"
                );
                return Ok(OfferingResult::Found(offering));
            }
        }

        // Step 3: Try explicit fallback offering ID (reactive scaling - not warm pool)
        if let Some(fallback_id) = constraints.and_then(|c| c.fallback_offering_id.as_ref()) {
            match self.api.get_offering(fallback_id).await {
                Ok(Some(_)) => {
                    warn!(policy = %policy_name, fallback = %fallback_id, "Using fallback offering");
                    self.scale_up_with_offering(
                        ns,
                        policy_name,
                        policy,
                        remaining,
                        fallback_id,
                        false,
                    )
                    .await?;
                    return Ok(OfferingResult::UsedFallback(remaining));
                }
                Ok(None) => {
                    warn!(policy = %policy_name, fallback = %fallback_id, "Fallback offering does not exist")
                }
                Err(e) => {
                    warn!(policy = %policy_name, fallback = %fallback_id, error = %e, "Failed to validate fallback")
                }
            }
        }

        self.emit_no_offering_events(ns, requirements, pods).await;
        self.metrics.record_no_matching_offering(policy_name);
        warn!(policy = %policy_name, gpu_count = requirements.gpu_count, models = ?requirements.gpu_models, "No matching offering found");
        Ok(OfferingResult::NotFound)
    }

    #[allow(clippy::too_many_arguments)]
    async fn calculate_and_create_nodes(
        &self,
        ns: &str,
        policy_name: &str,
        policy: &ScalingPolicy,
        requirements: &GpuRequirements,
        pods: &[PendingGpuPod],
        offering: &crate::api::GpuOffering,
        remaining: u32,
        pending_pods: &[Pod],
    ) -> Result<u32> {
        let nodes_needed = calculate_nodes_needed(pods, offering, requirements.gpu_count);
        let nodes_to_create = nodes_needed.min(remaining);
        if nodes_to_create == 0 {
            return Ok(0);
        }

        info!(
            policy = %policy_name, offering_id = %offering.id, gpu_type = %offering.gpu_type,
            nodes = nodes_to_create, pending_pods = pods.len(), "Creating nodes with dynamically selected offering"
        );

        self.emit_missing_affinity_warnings(pods, pending_pods)
            .await;
        self.metrics
            .record_offering_selection(policy_name, &offering.id, &offering.gpu_type);
        // Reactive scaling - not warm pool
        self.scale_up_with_offering(
            ns,
            policy_name,
            policy,
            nodes_to_create,
            &offering.id,
            false,
        )
        .await?;
        Ok(nodes_to_create)
    }

    async fn scale_up_with_offering(
        &self,
        ns: &str,
        policy_name: &str,
        policy: &ScalingPolicy,
        count: u32,
        offering_id: &str,
        is_warm_pool: bool,
    ) -> Result<()> {
        let template = policy.spec.node_template.as_ref().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration("Missing node_template in policy".to_string())
        })?;

        for i in 0..count {
            let pool_name = format!(
                "{}-{}-{}",
                policy_name,
                Utc::now().format("%Y%m%d%H%M%S%3f"),
                i
            );

            let node_pool = self.create_node_pool_from_template(
                ns,
                &pool_name,
                policy_name,
                template,
                offering_id,
                is_warm_pool,
            )?;
            info!(
                pool = %pool_name,
                offering_id = %offering_id,
                is_warm_pool = %is_warm_pool,
                "Creating NodePool for scale-up"
            );

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

    /// Emit warning events for pods that request specific GPU models but lack nodeAffinity.
    /// This helps users understand why their pods might not schedule to the correct nodes.
    async fn emit_missing_affinity_warnings(
        &self,
        pods: &[PendingGpuPod],
        pending_pods_raw: &[Pod],
    ) {
        for pending_pod in pods {
            // Skip if no specific GPU model requested
            if pending_pod.requirements.gpu_models.is_empty() {
                continue;
            }

            // Find the raw pod to check nodeAffinity
            let raw_pod = pending_pods_raw.iter().find(|p| {
                p.metadata.name.as_deref() == Some(&pending_pod.pod_name)
                    && p.metadata.namespace.as_deref().unwrap_or("default") == pending_pod.namespace
            });

            if let Some(pod) = raw_pod {
                if !has_gpu_node_affinity(pod) {
                    let message = format!(
                        "Pod requests specific GPU model(s) {:?} but has no nodeAffinity for GPU labels (basilica.ai/gpu-model). \
                        Pod may schedule to any available GPU node. Add nodeAffinity to target specific GPU types.",
                        pending_pod.requirements.gpu_models
                    );

                    if let Err(e) = self
                        .k8s
                        .create_pod_event(
                            &pending_pod.namespace,
                            &pending_pod.pod_name,
                            pending_pod.pod_uid.as_deref(),
                            "Warning",
                            "MissingNodeAffinity",
                            &message,
                        )
                        .await
                    {
                        warn!(
                            pod = %pending_pod.pod_name,
                            error = %e,
                            "Failed to emit MissingNodeAffinity warning"
                        );
                    }
                }
            }
        }
    }

    /// Emit K8s events to pending pods when no matching offering is found.
    /// This provides user visibility into why their pods remain pending.
    async fn emit_no_offering_events(
        &self,
        _ns: &str,
        requirements: &GpuRequirements,
        pods: &[PendingGpuPod],
    ) {
        let message = format!(
            "Autoscaler could not find GPU offering matching {} GPU(s){}{}",
            requirements.gpu_count,
            if requirements.gpu_models.is_empty() {
                String::new()
            } else {
                format!(", models: {:?}", requirements.gpu_models)
            },
            requirements
                .min_gpu_memory_gb
                .map(|m| format!(", min memory: {}GB", m))
                .unwrap_or_default()
        );

        for pod in pods {
            if let Err(e) = self
                .k8s
                .create_pod_event(
                    &pod.namespace,
                    &pod.pod_name,
                    pod.pod_uid.as_deref(),
                    "Warning",
                    "OfferingUnavailable",
                    &message,
                )
                .await
            {
                warn!(
                    pod = %pod.pod_name,
                    namespace = %pod.namespace,
                    error = %e,
                    "Failed to emit OfferingUnavailable event"
                );
            }
        }
    }

    /// Build effective constraints by merging policy-level constraints with template-level fields.
    /// Policy-level constraints take precedence when both are set.
    fn build_effective_constraints(&self, policy: &ScalingPolicy) -> Option<OfferingConstraints> {
        let template = policy.spec.node_template.as_ref()?;
        let secure_cloud = template.secure_cloud.as_ref()?;

        // Create constraints from template fields
        let template_constraints = OfferingConstraints::from_template(
            secure_cloud.preferred_provider.as_deref(),
            secure_cloud.region.as_deref(),
            secure_cloud.max_hourly_rate,
        );

        // If policy has explicit constraints, merge them (policy takes precedence)
        match &policy.spec.offering_constraints {
            Some(policy_constraints) => Some(policy_constraints.merge_with(&template_constraints)),
            None if !template_constraints.providers.is_empty()
                || template_constraints.regions.is_some()
                || template_constraints.max_hourly_rate.is_some() =>
            {
                Some(template_constraints)
            }
            None => None,
        }
    }

    /// Collect pods grouped by node name for VRAM accounting.
    async fn collect_pods_by_node(&self, node_pools: &[NodePool]) -> BTreeMap<String, Vec<Pod>> {
        let mut pods_by_node = BTreeMap::new();

        for pool in node_pools {
            let node_name = pool.status.as_ref().and_then(|s| s.node_name.as_ref());

            if let Some(name) = node_name {
                match self.k8s.list_pods_on_node(name).await {
                    Ok(pods) => {
                        pods_by_node.insert(name.clone(), pods);
                    }
                    Err(e) => {
                        debug!(node = %name, error = %e, "Failed to list pods on node");
                    }
                }
            }
        }

        pods_by_node
    }

    /// Get offering hourly rates keyed by offering ID.
    async fn get_offering_rates(&self) -> BTreeMap<String, f64> {
        match self.api.list_offerings().await {
            Ok(offerings) => offerings
                .into_iter()
                .map(|o| (o.id, o.hourly_rate_per_gpu * o.gpu_count as f64))
                .collect(),
            Err(e) => {
                debug!(error = %e, "Failed to fetch offering rates for warm pool");
                BTreeMap::new()
            }
        }
    }

    /// Get available offerings for warm pool selection.
    async fn get_available_offerings(&self) -> Vec<crate::api::GpuOffering> {
        match self.api.list_offerings().await {
            Ok(offerings) => offerings.into_iter().filter(|o| o.availability).collect(),
            Err(e) => {
                debug!(error = %e, "Failed to fetch offerings for warm pool");
                Vec::new()
            }
        }
    }

    /// Count warm pool nodes from node pools (Ready only).
    fn count_warm_pool_nodes(node_pools: &[NodePool]) -> u32 {
        node_pools
            .iter()
            .filter(|p| {
                let is_warm = p
                    .metadata
                    .labels
                    .as_ref()
                    .and_then(|l| l.get("basilica.ai/warm-pool"))
                    .map(|v| v == "true")
                    .unwrap_or(false);

                let is_ready = p
                    .status
                    .as_ref()
                    .and_then(|s| s.phase.as_ref())
                    .map(|phase| *phase == NodePoolPhase::Ready)
                    .unwrap_or(false);

                is_warm && is_ready
            })
            .count() as u32
    }

    /// Find non-warm-pool nodes that are idle and eligible for scale-down.
    /// Returns nodes that have been Ready for longer than idle_timeout_seconds
    /// and have no GPU pods running on them.
    fn find_idle_nodes_for_scale_down(
        node_pools: &[NodePool],
        pods_by_node: &BTreeMap<String, Vec<Pod>>,
        idle_timeout_seconds: u32,
    ) -> Vec<String> {
        let now = Utc::now();

        node_pools
            .iter()
            .filter_map(|pool| {
                let status = pool.status.as_ref()?;

                // Only consider Ready nodes
                if status.phase.as_ref() != Some(&NodePoolPhase::Ready) {
                    return None;
                }

                // Skip warm pool nodes (they have their own scale-down logic)
                let is_warm_pool = pool
                    .metadata
                    .labels
                    .as_ref()
                    .and_then(|l| l.get("basilica.ai/warm-pool"))
                    .map(|v| v == "true")
                    .unwrap_or(false);

                if is_warm_pool {
                    return None;
                }

                // Check if node has been Ready long enough
                let phase_entered = status.phase_entered_at.as_ref()?;
                let idle_duration = now.signed_duration_since(*phase_entered);
                if idle_duration.num_seconds() < idle_timeout_seconds as i64 {
                    return None;
                }

                // Check if node has any GPU pods
                let node_name = status.node_name.as_ref()?;
                let has_gpu_pods = pods_by_node
                    .get(node_name)
                    .map(|pods| {
                        pods.iter().any(|pod| {
                            pod.spec
                                .as_ref()
                                .map(|spec| {
                                    spec.containers.iter().any(|c| {
                                        c.resources
                                            .as_ref()
                                            .and_then(|r| r.requests.as_ref())
                                            .and_then(|req| req.get("nvidia.com/gpu"))
                                            .is_some()
                                    })
                                })
                                .unwrap_or(false)
                        })
                    })
                    .unwrap_or(false);

                if has_gpu_pods {
                    return None;
                }

                Some(pool.metadata.name.clone().unwrap_or_default())
            })
            .collect()
    }

    /// Count pending warm pool nodes (Provisioning, Configuring, etc.).
    /// These are nodes that have been created but are not yet Ready.
    fn count_pending_warm_pool_nodes(node_pools: &[NodePool]) -> u32 {
        node_pools
            .iter()
            .filter(|p| {
                let is_warm = p
                    .metadata
                    .labels
                    .as_ref()
                    .and_then(|l| l.get("basilica.ai/warm-pool"))
                    .map(|v| v == "true")
                    .unwrap_or(false);

                let phase = p.status.as_ref().and_then(|s| s.phase.as_ref());

                let is_pending = matches!(
                    phase,
                    Some(NodePoolPhase::Pending)
                        | Some(NodePoolPhase::Provisioning)
                        | Some(NodePoolPhase::Configuring)
                        | Some(NodePoolPhase::InstallingWireGuard)
                        | Some(NodePoolPhase::ValidatingNetwork)
                        | Some(NodePoolPhase::JoiningCluster)
                        | Some(NodePoolPhase::WaitingForNode)
                );

                is_warm && is_pending
            })
            .count() as u32
    }

    /// Select the best offering for warm pool based on preferences.
    fn select_warm_pool_offering<'a>(
        offerings: &'a [crate::api::GpuOffering],
        config: &WarmPoolConfig,
    ) -> Option<&'a crate::api::GpuOffering> {
        if offerings.is_empty() {
            return None;
        }

        // Sort by preference (preferred GPU types first, then by cost)
        let mut sorted: Vec<_> = offerings.iter().collect();
        sorted.sort_by(|a, b| {
            let a_pref = config
                .preferred_gpu_types
                .iter()
                .position(|t| t.eq_ignore_ascii_case(&a.gpu_type))
                .unwrap_or(usize::MAX);
            let b_pref = config
                .preferred_gpu_types
                .iter()
                .position(|t| t.eq_ignore_ascii_case(&b.gpu_type))
                .unwrap_or(usize::MAX);
            let a_cost = a.hourly_rate_per_gpu * a.gpu_count as f64;
            let b_cost = b.hourly_rate_per_gpu * b.gpu_count as f64;
            a_pref
                .cmp(&b_pref)
                .then_with(|| a_cost.partial_cmp(&b_cost).unwrap())
        });

        sorted.first().copied()
    }

    fn create_node_pool_from_template(
        &self,
        _ns: &str,
        pool_name: &str,
        policy_name: &str,
        template: &crate::crd::NodeTemplate,
        offering_id: &str,
        is_warm_pool: bool,
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
        if is_warm_pool {
            labels.insert("basilica.ai/warm-pool".to_string(), "true".to_string());
        }

        // Build SecureCloudConfig from template with the specific offering_id
        let (secure_cloud, datacenter_id) = match template.secure_cloud.as_ref() {
            Some(sc) => (
                Some(SecureCloudConfig {
                    offering_id: offering_id.to_string(),
                    ssh_key_id: sc.ssh_key_id.clone(),
                    ssh_key_secret_ref: sc.ssh_key_secret_ref.clone(),
                    ssh_user: sc.ssh_user.clone(),
                }),
                Some(sc.datacenter_id.clone()),
            ),
            None => (None, None),
        };

        let spec = NodePoolSpec {
            mode: NodePoolMode::Dynamic,
            ssh: None, // Dynamic mode uses secure_cloud
            secure_cloud,
            k3s: template.k3s.clone(),
            wireguard: WireGuardConfig::default(),
            health_check: HealthCheckConfig::default(),
            lifecycle: template.lifecycle.clone(),
            node_id: None,
            datacenter_id,
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

/// Result of offering lookup with fallback handling
enum OfferingResult {
    Found(crate::api::GpuOffering),
    UsedFallback(u32),
    NotFound,
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
