use k8s_openapi::api::core::v1::{Node, Pod};
use std::collections::BTreeMap;
use tracing::debug;

use crate::api::GpuOffering;
use crate::crd::{NodePool, NodePoolPhase, WarmPoolConfig, WarmPoolStatus};
use crate::offering_matcher::node_labels;

/// Calculate hysteresis thresholds from target VRAM and config percentages.
/// Uses saturating conversion to prevent overflow on large VRAM values.
fn calculate_thresholds(target_vram: u32, config: &WarmPoolConfig) -> (u32, u32) {
    let scale_up = (target_vram as f64 * config.scale_up_threshold_percent as f64 / 100.0)
        .min(u32::MAX as f64) as u32;
    let scale_down = (target_vram as f64 * config.scale_down_threshold_percent as f64 / 100.0)
        .min(u32::MAX as f64) as u32;
    (scale_up, scale_down)
}

/// VRAM metrics for warm pool accounting
#[derive(Debug, Clone, Default)]
pub struct VramMetrics {
    pub total_vram_gb: u32,
    pub allocated_vram_gb: u32,
    pub idle_vram_gb: u32,
    pub idle_node_count: u32,
    pub idle_node_names: Vec<String>,
    pub estimated_hourly_cost: f64,
}

/// Target state for the warm pool
#[derive(Debug, Clone)]
pub struct WarmPoolTarget {
    pub nodes: u32,
    pub total_vram_gb: u32,
    pub estimated_hourly_cost: f64,
}

/// Warm pool node info for internal tracking
#[derive(Debug, Clone)]
pub struct WarmPoolNodeInfo {
    pub name: String,
    pub pool_name: String,
    pub vram_gb: u32,
    pub gpu_count: u32,
    pub gpu_type: String,
    pub hourly_rate: f64,
    pub is_idle: bool,
    pub allocated_gpu_count: u32,
}

/// Scaling decision from warm pool evaluation
#[derive(Debug, Clone, PartialEq)]
pub enum WarmPoolDecision {
    ScaleUp { count: u32, reason: String },
    ScaleDown { count: u32, reason: String },
    NoAction,
}

/// Calculate VRAM accounting for warm pool nodes.
/// A node is part of the warm pool if labeled with `basilica.ai/warm-pool=true`.
pub fn calculate_vram_metrics(
    node_pools: &[NodePool],
    pods_by_node: &BTreeMap<String, Vec<Pod>>,
    offering_rates: &BTreeMap<String, f64>,
) -> VramMetrics {
    let mut total_vram_gb = 0u32;
    let mut allocated_vram_gb = 0u32;
    let mut idle_node_count = 0u32;
    let mut idle_node_names = Vec::new();
    let mut estimated_hourly_cost = 0.0f64;

    for pool in node_pools {
        let status = match &pool.status {
            Some(s) => s,
            None => continue,
        };

        // Only count Ready nodes in the warm pool
        let phase = status.phase.as_ref();
        if phase != Some(&NodePoolPhase::Ready) {
            continue;
        }

        // Check if this pool is a warm pool node
        let is_warm_pool = pool
            .metadata
            .labels
            .as_ref()
            .and_then(|l| l.get("basilica.ai/warm-pool"))
            .map(|v| v == "true")
            .unwrap_or(false);

        if !is_warm_pool {
            continue;
        }

        // Get VRAM for this node
        let gpu_memory_gb = status.gpu_memory_gb.unwrap_or(0);
        let gpu_count = status.gpu_count.unwrap_or(1);
        let node_vram = gpu_memory_gb * gpu_count;
        total_vram_gb += node_vram;

        // Get hourly rate from offering
        let hourly_rate = status
            .offering_id
            .as_ref()
            .and_then(|id| offering_rates.get(id))
            .copied()
            .unwrap_or(0.0);
        estimated_hourly_cost += hourly_rate;

        // Calculate allocated VRAM from pods on this node
        let node_name = match &status.node_name {
            Some(n) => n,
            None => continue,
        };

        let pods = pods_by_node.get(node_name);
        let allocated_gpus = pods
            .map(|pod_list| pod_list.iter().map(count_pod_gpu_requests).sum::<u32>())
            .unwrap_or(0);

        let node_allocated_vram = allocated_gpus * gpu_memory_gb;
        allocated_vram_gb += node_allocated_vram;

        // Node is idle if no GPUs allocated (or very low utilization)
        if allocated_gpus == 0 {
            idle_node_count += 1;
            idle_node_names.push(node_name.clone());
        }
    }

    let idle_vram_gb = total_vram_gb.saturating_sub(allocated_vram_gb);

    VramMetrics {
        total_vram_gb,
        allocated_vram_gb,
        idle_vram_gb,
        idle_node_count,
        idle_node_names,
        estimated_hourly_cost,
    }
}

/// Count GPU requests from a pod's containers
fn count_pod_gpu_requests(pod: &Pod) -> u32 {
    let spec = match &pod.spec {
        Some(s) => s,
        None => return 0,
    };

    let mut total = 0u32;

    // Count from regular containers
    for container in &spec.containers {
        if let Some(resources) = &container.resources {
            if let Some(requests) = &resources.requests {
                if let Some(gpu_qty) = requests.get("nvidia.com/gpu") {
                    if let Ok(count) = gpu_qty.0.parse::<u32>() {
                        total += count;
                    }
                }
            }
        }
    }

    // Count from init containers
    if let Some(init_containers) = &spec.init_containers {
        for container in init_containers {
            if let Some(resources) = &container.resources {
                if let Some(requests) = &resources.requests {
                    if let Some(gpu_qty) = requests.get("nvidia.com/gpu") {
                        if let Ok(count) = gpu_qty.0.parse::<u32>() {
                            total = total.max(count); // Init containers run sequentially
                        }
                    }
                }
            }
        }
    }

    total
}

/// Calculate the target warm pool configuration.
/// Returns the number of nodes needed to meet VRAM target within cost constraints.
pub fn calculate_warm_pool_target(
    config: &WarmPoolConfig,
    available_offerings: &[GpuOffering],
) -> WarmPoolTarget {
    if !config.enabled {
        return WarmPoolTarget {
            nodes: 0,
            total_vram_gb: 0,
            estimated_hourly_cost: 0.0,
        };
    }

    let target_vram_gb = config.min_idle_vram_gb;
    let mut nodes_needed = 0u32;
    let mut total_vram = 0u32;
    let mut total_cost = 0.0f64;

    // Sort offerings by preference (preferred GPU types first, then by cost)
    let mut offerings: Vec<_> = available_offerings
        .iter()
        .filter(|o| o.availability)
        .collect();

    offerings.sort_by(|a, b| {
        let a_pref = preference_index(&config.preferred_gpu_types, &a.gpu_type);
        let b_pref = preference_index(&config.preferred_gpu_types, &b.gpu_type);
        // Use total hourly cost (per_gpu * gpu_count) for comparison
        let a_total_rate = a.hourly_rate_per_gpu * a.gpu_count as f64;
        let b_total_rate = b.hourly_rate_per_gpu * b.gpu_count as f64;
        a_pref
            .cmp(&b_pref)
            .then_with(|| a_total_rate.partial_cmp(&b_total_rate).unwrap())
    });

    // Add nodes until VRAM target is met or limits hit.
    // Use the best offering repeatedly until target is reached.
    if let Some(best_offering) = offerings.first() {
        let vram_per_node = best_offering.gpu_memory_gb() * best_offering.gpu_count;
        let total_hourly_rate = best_offering.hourly_rate_per_gpu * best_offering.gpu_count as f64;

        while total_vram < target_vram_gb
            && nodes_needed < config.max_idle_nodes
            && total_cost + total_hourly_rate <= config.max_idle_cost_per_hour
        {
            nodes_needed += 1;
            total_vram += vram_per_node;
            total_cost += total_hourly_rate;
        }
    }

    WarmPoolTarget {
        nodes: nodes_needed,
        total_vram_gb: total_vram,
        estimated_hourly_cost: total_cost,
    }
}

/// Get preference index for a GPU type (lower is better, usize::MAX if not in list)
fn preference_index(preferred: &[String], gpu_type: &str) -> usize {
    let normalized = normalize_gpu_type(gpu_type);
    preferred
        .iter()
        .position(|t| normalize_gpu_type(t) == normalized)
        .unwrap_or(usize::MAX)
}

/// Normalize GPU type for comparison (uppercase, alphanumeric only)
fn normalize_gpu_type(gpu_type: &str) -> String {
    gpu_type
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .collect::<String>()
        .to_uppercase()
}

/// Evaluate warm pool scaling decision with hysteresis.
/// Uses thresholds to prevent scaling flapping.
pub fn evaluate_warm_pool_scaling(
    config: &WarmPoolConfig,
    metrics: &VramMetrics,
    current_warm_nodes: u32,
    available_offerings: &[GpuOffering],
    pending_gpu_pods: u32,
) -> WarmPoolDecision {
    if !config.enabled {
        return WarmPoolDecision::NoAction;
    }

    // Priority 1: Reactive scaling for pending pods (handled by main controller)
    // The warm pool logic only handles proactive capacity
    if pending_gpu_pods > 0 {
        debug!(
            pending_pods = pending_gpu_pods,
            "Pending GPU pods detected, deferring to reactive scaling"
        );
        return WarmPoolDecision::NoAction;
    }

    let target_vram = config.min_idle_vram_gb;
    let (scale_up_threshold, scale_down_threshold) = calculate_thresholds(target_vram, config);
    let current_idle_vram = metrics.idle_vram_gb;

    // Scale UP: Below lower hysteresis threshold
    if current_idle_vram < scale_up_threshold {
        let target = calculate_warm_pool_target(config, available_offerings);
        let deficit = target.nodes.saturating_sub(current_warm_nodes);

        if deficit > 0 && target.estimated_hourly_cost <= config.max_idle_cost_per_hour {
            return WarmPoolDecision::ScaleUp {
                count: deficit.min(config.max_idle_nodes - current_warm_nodes),
                reason: format!(
                    "warm pool: idle VRAM {}GB < {}GB ({}% of target {}GB)",
                    current_idle_vram,
                    scale_up_threshold,
                    config.scale_up_threshold_percent,
                    target_vram
                ),
            };
        }
    }

    // Scale DOWN: Above upper hysteresis threshold
    if current_idle_vram > scale_down_threshold && metrics.idle_node_count > 0 {
        let target = calculate_warm_pool_target(config, available_offerings);
        let excess = current_warm_nodes.saturating_sub(target.nodes);

        if excess > 0 {
            return WarmPoolDecision::ScaleDown {
                count: 1, // Scale down one at a time for safety
                reason: format!(
                    "cost-saving: idle VRAM {}GB > {}GB ({}% of target {}GB)",
                    current_idle_vram,
                    scale_down_threshold,
                    config.scale_down_threshold_percent,
                    target_vram
                ),
            };
        }
    }

    WarmPoolDecision::NoAction
}

/// Build WarmPoolStatus from current metrics, config, and target.
pub fn build_warm_pool_status(
    config: &WarmPoolConfig,
    metrics: &VramMetrics,
    target: &WarmPoolTarget,
) -> WarmPoolStatus {
    let target_vram = if config.enabled {
        config.min_idle_vram_gb
    } else {
        0
    };
    let (scale_up_threshold, scale_down_threshold) = calculate_thresholds(target_vram, config);

    WarmPoolStatus {
        total_vram_gb: metrics.total_vram_gb,
        allocated_vram_gb: metrics.allocated_vram_gb,
        idle_vram_gb: metrics.idle_vram_gb,
        target_vram_gb: target_vram,
        scale_up_threshold_gb: scale_up_threshold,
        scale_down_threshold_gb: scale_down_threshold,
        target_nodes: target.nodes,
        idle_nodes: metrics.idle_node_count,
        estimated_hourly_cost: metrics.estimated_hourly_cost,
        idle_node_names: metrics.idle_node_names.clone(),
    }
}

/// Select an idle warm pool node for removal (returns first found).
pub fn select_node_for_removal(metrics: &VramMetrics) -> Option<String> {
    metrics.idle_node_names.first().cloned()
}

/// Get GPU memory in GB for a node based on labels or defaults
pub fn get_gpu_memory_gb(node: &Node) -> u32 {
    let labels = match &node.metadata.labels {
        Some(l) => l,
        None => return 24, // Default
    };

    // Check explicit label first
    if let Some(mem) = labels.get(node_labels::GPU_MEMORY_GB) {
        if let Ok(v) = mem.parse::<u32>() {
            return v;
        }
    }

    // Fall back to GPU type lookup
    if let Some(gpu_type) = labels.get(node_labels::GPU_MODEL) {
        return gpu_type_to_memory(gpu_type);
    }

    24 // Conservative default
}

/// Map GPU type to memory in GB
fn gpu_type_to_memory(gpu_type: &str) -> u32 {
    let normalized = normalize_gpu_type(gpu_type);
    match normalized.as_str() {
        "H100" | "A100" => 80,
        "H100SXM" | "A100SXM" => 80,
        "H100PCIE" | "A100PCIE" => 80,
        "L40S" => 48,
        "L40" => 48,
        "RTX4090" => 24,
        "RTX3090" => 24,
        "A6000" => 48,
        "A5000" => 24,
        "V100" => 32,
        _ => 24, // Conservative default
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_preference_index() {
        let preferred = vec![
            "H100".to_string(),
            "A100".to_string(),
            "RTX4090".to_string(),
        ];
        assert_eq!(preference_index(&preferred, "H100"), 0);
        assert_eq!(preference_index(&preferred, "h100"), 0);
        assert_eq!(preference_index(&preferred, "A100"), 1);
        assert_eq!(preference_index(&preferred, "RTX-4090"), 2);
        assert_eq!(preference_index(&preferred, "Unknown"), usize::MAX);
    }

    #[test]
    fn test_normalize_gpu_type() {
        assert_eq!(normalize_gpu_type("H100"), "H100");
        assert_eq!(normalize_gpu_type("h100"), "H100");
        assert_eq!(normalize_gpu_type("RTX-4090"), "RTX4090");
        assert_eq!(normalize_gpu_type("a100_80gb"), "A10080GB");
    }

    #[test]
    fn test_gpu_type_to_memory() {
        assert_eq!(gpu_type_to_memory("H100"), 80);
        assert_eq!(gpu_type_to_memory("A100"), 80);
        assert_eq!(gpu_type_to_memory("RTX4090"), 24);
        assert_eq!(gpu_type_to_memory("RTX-4090"), 24);
        assert_eq!(gpu_type_to_memory("L40S"), 48);
        assert_eq!(gpu_type_to_memory("Unknown"), 24);
    }

    #[test]
    fn test_warm_pool_disabled() {
        let config = WarmPoolConfig {
            enabled: false,
            ..Default::default()
        };

        let target = calculate_warm_pool_target(&config, &[]);
        assert_eq!(target.nodes, 0);
        assert_eq!(target.total_vram_gb, 0);
    }

    #[test]
    fn test_evaluate_scaling_disabled() {
        let config = WarmPoolConfig {
            enabled: false,
            ..Default::default()
        };
        let metrics = VramMetrics::default();

        let decision = evaluate_warm_pool_scaling(&config, &metrics, 0, &[], 0);
        assert_eq!(decision, WarmPoolDecision::NoAction);
    }

    #[test]
    fn test_evaluate_scaling_defers_to_reactive() {
        let config = WarmPoolConfig {
            enabled: true,
            min_idle_vram_gb: 160,
            ..Default::default()
        };
        let metrics = VramMetrics::default();

        // Should not scale proactively when there are pending pods
        let decision = evaluate_warm_pool_scaling(&config, &metrics, 0, &[], 5);
        assert_eq!(decision, WarmPoolDecision::NoAction);
    }

    #[test]
    fn test_scale_up_below_threshold() {
        let config = WarmPoolConfig {
            enabled: true,
            min_idle_vram_gb: 160,
            max_idle_nodes: 5,
            max_idle_cost_per_hour: 100.0,
            scale_up_threshold_percent: 80,
            scale_down_threshold_percent: 120,
            preferred_gpu_types: vec!["H100".to_string()],
        };

        let metrics = VramMetrics {
            total_vram_gb: 80,
            allocated_vram_gb: 0,
            idle_vram_gb: 80, // Below 80% of 160 = 128
            idle_node_count: 1,
            idle_node_names: vec!["node-1".to_string()],
            estimated_hourly_cost: 3.0,
        };

        let offerings = vec![GpuOffering {
            id: "test-offering".to_string(),
            provider: "test".to_string(),
            gpu_type: "H100".to_string(),
            gpu_count: 1,
            gpu_memory_gb_per_gpu: Some(80),
            hourly_rate_per_gpu: 3.0,
            region: "us-east-1".to_string(),
            availability: true,
        }];

        let decision = evaluate_warm_pool_scaling(&config, &metrics, 1, &offerings, 0);
        match decision {
            WarmPoolDecision::ScaleUp { count, reason } => {
                assert_eq!(count, 1);
                assert!(reason.contains("warm pool"));
            }
            _ => panic!("Expected ScaleUp decision"),
        }
    }

    #[test]
    fn test_no_scale_within_hysteresis_band() {
        let config = WarmPoolConfig {
            enabled: true,
            min_idle_vram_gb: 160,
            max_idle_nodes: 5,
            max_idle_cost_per_hour: 100.0,
            scale_up_threshold_percent: 80,
            scale_down_threshold_percent: 120,
            preferred_gpu_types: vec!["H100".to_string()],
        };

        let metrics = VramMetrics {
            total_vram_gb: 160,
            allocated_vram_gb: 0,
            idle_vram_gb: 160, // At target (between 128 and 192)
            idle_node_count: 2,
            idle_node_names: vec!["node-1".to_string(), "node-2".to_string()],
            estimated_hourly_cost: 6.0,
        };

        let offerings = vec![GpuOffering {
            id: "test-offering".to_string(),
            provider: "test".to_string(),
            gpu_type: "H100".to_string(),
            gpu_count: 1,
            gpu_memory_gb_per_gpu: Some(80),
            hourly_rate_per_gpu: 3.0,
            region: "us-east-1".to_string(),
            availability: true,
        }];

        let decision = evaluate_warm_pool_scaling(&config, &metrics, 2, &offerings, 0);
        assert_eq!(decision, WarmPoolDecision::NoAction);
    }

    #[test]
    fn test_scale_down_above_threshold() {
        let config = WarmPoolConfig {
            enabled: true,
            min_idle_vram_gb: 160,
            max_idle_nodes: 5,
            max_idle_cost_per_hour: 100.0,
            scale_up_threshold_percent: 80,
            scale_down_threshold_percent: 120,
            preferred_gpu_types: vec!["H100".to_string()],
        };

        let metrics = VramMetrics {
            total_vram_gb: 240,
            allocated_vram_gb: 0,
            idle_vram_gb: 240, // Above 120% of 160 = 192
            idle_node_count: 3,
            idle_node_names: vec![
                "node-1".to_string(),
                "node-2".to_string(),
                "node-3".to_string(),
            ],
            estimated_hourly_cost: 9.0,
        };

        let offerings = vec![GpuOffering {
            id: "test-offering".to_string(),
            provider: "test".to_string(),
            gpu_type: "H100".to_string(),
            gpu_count: 1,
            gpu_memory_gb_per_gpu: Some(80),
            hourly_rate_per_gpu: 3.0,
            region: "us-east-1".to_string(),
            availability: true,
        }];

        let decision = evaluate_warm_pool_scaling(&config, &metrics, 3, &offerings, 0);
        match decision {
            WarmPoolDecision::ScaleDown { count, reason } => {
                assert_eq!(count, 1);
                assert!(reason.contains("cost-saving"));
            }
            _ => panic!("Expected ScaleDown decision"),
        }
    }

    #[test]
    fn test_build_warm_pool_status() {
        let config = WarmPoolConfig {
            enabled: true,
            min_idle_vram_gb: 160,
            scale_up_threshold_percent: 80,
            scale_down_threshold_percent: 120,
            ..Default::default()
        };

        let metrics = VramMetrics {
            total_vram_gb: 160,
            allocated_vram_gb: 40,
            idle_vram_gb: 120,
            idle_node_count: 2,
            idle_node_names: vec!["node-1".to_string()],
            estimated_hourly_cost: 6.0,
        };

        let target = WarmPoolTarget {
            nodes: 2,
            total_vram_gb: 160,
            estimated_hourly_cost: 6.0,
        };

        let status = build_warm_pool_status(&config, &metrics, &target);
        assert_eq!(status.total_vram_gb, 160);
        assert_eq!(status.allocated_vram_gb, 40);
        assert_eq!(status.idle_vram_gb, 120);
        assert_eq!(status.target_vram_gb, 160);
        assert_eq!(status.scale_up_threshold_gb, 128);
        assert_eq!(status.scale_down_threshold_gb, 192);
        assert_eq!(status.target_nodes, 2);
    }
}
