use axum::{routing::get, Router};
use prometheus::{
    Histogram, HistogramOpts, HistogramVec, IntCounter, IntCounterVec, IntGauge, IntGaugeVec, Opts,
    Registry, TextEncoder,
};
use std::sync::Arc;
use tracing::info;

use crate::crd::NodePoolPhase;

/// Prometheus metrics for the autoscaler
pub struct AutoscalerMetrics {
    registry: Registry,

    // Node pool metrics
    node_pools_total: IntGauge,
    node_pools_by_phase: IntGaugeVec,
    phase_transitions_total: IntCounterVec,

    // Reconciliation metrics
    reconcile_total: IntCounterVec,
    reconcile_errors_total: IntCounterVec,
    reconcile_duration_seconds: HistogramVec,

    // Scaling metrics
    scale_events_total: IntCounterVec,
    scale_up_total: IntCounter,
    scale_down_total: IntCounter,

    // Rental metrics
    rentals_active: IntGauge,
    rentals_started_total: IntCounterVec,
    rentals_stopped_total: IntCounter,

    // SSH/provisioning metrics
    ssh_connections_total: IntCounter,
    ssh_errors_total: IntCounter,
    provisioning_duration_seconds: Histogram,

    // Leader election metrics
    is_leader: IntGauge,
    leader_transitions_total: IntCounter,
}

impl AutoscalerMetrics {
    pub fn new() -> Self {
        let registry = Registry::new();

        // Node pool metrics
        let node_pools_total = IntGauge::new(
            "autoscaler_node_pools_total",
            "Total number of NodePool resources",
        )
        .unwrap();

        let node_pools_by_phase = IntGaugeVec::new(
            Opts::new(
                "autoscaler_node_pools_by_phase",
                "Number of NodePools in each phase",
            ),
            &["phase"],
        )
        .unwrap();

        let phase_transitions_total = IntCounterVec::new(
            Opts::new(
                "autoscaler_phase_transitions_total",
                "Total number of phase transitions",
            ),
            &["pool", "to_phase"],
        )
        .unwrap();

        // Reconciliation metrics
        let reconcile_total = IntCounterVec::new(
            Opts::new(
                "autoscaler_reconcile_total",
                "Total number of reconciliations",
            ),
            &["controller"],
        )
        .unwrap();

        let reconcile_errors_total = IntCounterVec::new(
            Opts::new(
                "autoscaler_reconcile_errors_total",
                "Total number of reconciliation errors",
            ),
            &["controller", "error_type"],
        )
        .unwrap();

        let reconcile_duration_seconds = HistogramVec::new(
            HistogramOpts::new(
                "autoscaler_reconcile_duration_seconds",
                "Duration of reconciliation in seconds",
            )
            .buckets(vec![0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]),
            &["controller"],
        )
        .unwrap();

        // Scaling metrics
        let scale_events_total = IntCounterVec::new(
            Opts::new(
                "autoscaler_scale_events_total",
                "Total number of scaling events",
            ),
            &["policy", "direction"],
        )
        .unwrap();

        let scale_up_total = IntCounter::new(
            "autoscaler_scale_up_total",
            "Total number of scale up events",
        )
        .unwrap();

        let scale_down_total = IntCounter::new(
            "autoscaler_scale_down_total",
            "Total number of scale down events",
        )
        .unwrap();

        // Rental metrics
        let rentals_active = IntGauge::new(
            "autoscaler_rentals_active",
            "Number of currently active rentals",
        )
        .unwrap();

        let rentals_started_total = IntCounterVec::new(
            Opts::new(
                "autoscaler_rentals_started_total",
                "Total number of rentals started",
            ),
            &["pool", "provider"],
        )
        .unwrap();

        let rentals_stopped_total = IntCounter::new(
            "autoscaler_rentals_stopped_total",
            "Total number of rentals stopped",
        )
        .unwrap();

        // SSH metrics
        let ssh_connections_total = IntCounter::new(
            "autoscaler_ssh_connections_total",
            "Total number of SSH connections",
        )
        .unwrap();

        let ssh_errors_total =
            IntCounter::new("autoscaler_ssh_errors_total", "Total number of SSH errors").unwrap();

        let provisioning_duration_seconds = Histogram::with_opts(
            HistogramOpts::new(
                "autoscaler_provisioning_duration_seconds",
                "Duration of node provisioning in seconds",
            )
            .buckets(vec![30.0, 60.0, 120.0, 180.0, 300.0, 600.0]),
        )
        .unwrap();

        // Leader election metrics
        let is_leader = IntGauge::new(
            "autoscaler_is_leader",
            "Whether this instance is the leader (1) or not (0)",
        )
        .unwrap();

        let leader_transitions_total = IntCounter::new(
            "autoscaler_leader_transitions_total",
            "Total number of leader transitions",
        )
        .unwrap();

        // Register all metrics
        registry
            .register(Box::new(node_pools_total.clone()))
            .unwrap();
        registry
            .register(Box::new(node_pools_by_phase.clone()))
            .unwrap();
        registry
            .register(Box::new(phase_transitions_total.clone()))
            .unwrap();
        registry
            .register(Box::new(reconcile_total.clone()))
            .unwrap();
        registry
            .register(Box::new(reconcile_errors_total.clone()))
            .unwrap();
        registry
            .register(Box::new(reconcile_duration_seconds.clone()))
            .unwrap();
        registry
            .register(Box::new(scale_events_total.clone()))
            .unwrap();
        registry.register(Box::new(scale_up_total.clone())).unwrap();
        registry
            .register(Box::new(scale_down_total.clone()))
            .unwrap();
        registry.register(Box::new(rentals_active.clone())).unwrap();
        registry
            .register(Box::new(rentals_started_total.clone()))
            .unwrap();
        registry
            .register(Box::new(rentals_stopped_total.clone()))
            .unwrap();
        registry
            .register(Box::new(ssh_connections_total.clone()))
            .unwrap();
        registry
            .register(Box::new(ssh_errors_total.clone()))
            .unwrap();
        registry
            .register(Box::new(provisioning_duration_seconds.clone()))
            .unwrap();
        registry.register(Box::new(is_leader.clone())).unwrap();
        registry
            .register(Box::new(leader_transitions_total.clone()))
            .unwrap();

        Self {
            registry,
            node_pools_total,
            node_pools_by_phase,
            phase_transitions_total,
            reconcile_total,
            reconcile_errors_total,
            reconcile_duration_seconds,
            scale_events_total,
            scale_up_total,
            scale_down_total,
            rentals_active,
            rentals_started_total,
            rentals_stopped_total,
            ssh_connections_total,
            ssh_errors_total,
            provisioning_duration_seconds,
            is_leader,
            leader_transitions_total,
        }
    }

    pub fn record_reconcile(&self, controller: &str) {
        self.reconcile_total.with_label_values(&[controller]).inc();
    }

    pub fn record_reconcile_error(&self, controller: &str, error_type: &str) {
        self.reconcile_errors_total
            .with_label_values(&[controller, error_type])
            .inc();
    }

    pub fn observe_reconcile_duration(&self, controller: &str, duration_secs: f64) {
        self.reconcile_duration_seconds
            .with_label_values(&[controller])
            .observe(duration_secs);
    }

    pub fn record_phase_transition(&self, pool: &str, phase: &NodePoolPhase) {
        let phase_str = format!("{:?}", phase);
        self.phase_transitions_total
            .with_label_values(&[pool, &phase_str])
            .inc();
    }

    pub fn record_scale_event(&self, policy: &str, direction: &str, count: u32) {
        self.scale_events_total
            .with_label_values(&[policy, direction])
            .inc_by(count as u64);

        if direction == "scale_up" {
            self.scale_up_total.inc_by(count as u64);
        } else {
            self.scale_down_total.inc_by(count as u64);
        }
    }

    pub fn record_rental_started(&self, pool: &str, provider: &str) {
        self.rentals_started_total
            .with_label_values(&[pool, provider])
            .inc();
        self.rentals_active.inc();
    }

    pub fn record_rental_stopped(&self, _pool: &str) {
        self.rentals_stopped_total.inc();
        self.rentals_active.dec();
    }

    pub fn record_node_pool_deleted(&self, _pool: &str) {
        self.node_pools_total.dec();
    }

    pub fn set_node_pool_count(&self, count: i64) {
        self.node_pools_total.set(count);
    }

    pub fn set_node_pools_by_phase(&self, phase: &str, count: i64) {
        self.node_pools_by_phase
            .with_label_values(&[phase])
            .set(count);
    }

    pub fn set_is_leader(&self, leader: bool) {
        self.is_leader.set(if leader { 1 } else { 0 });
    }

    pub fn record_leader_transition(&self) {
        self.leader_transitions_total.inc();
    }

    pub fn record_ssh_connection(&self) {
        self.ssh_connections_total.inc();
    }

    pub fn record_ssh_error(&self) {
        self.ssh_errors_total.inc();
    }

    pub fn observe_provisioning_duration(&self, duration_secs: f64) {
        self.provisioning_duration_seconds.observe(duration_secs);
    }

    /// Export metrics in Prometheus text format
    pub fn export(&self) -> String {
        let encoder = TextEncoder::new();
        let metric_families = self.registry.gather();
        encoder
            .encode_to_string(&metric_families)
            .unwrap_or_default()
    }
}

impl Default for AutoscalerMetrics {
    fn default() -> Self {
        Self::new()
    }
}

/// Metrics endpoint handler
async fn metrics_handler(
    axum::extract::State(metrics): axum::extract::State<Arc<AutoscalerMetrics>>,
) -> String {
    metrics.export()
}

/// Create metrics router
pub fn metrics_router(metrics: Arc<AutoscalerMetrics>) -> Router {
    Router::new()
        .route("/metrics", get(metrics_handler))
        .with_state(metrics)
}

/// Start metrics server
pub async fn start_metrics_server(
    host: &str,
    port: u16,
    metrics: Arc<AutoscalerMetrics>,
) -> Result<(), std::io::Error> {
    let addr = format!("{}:{}", host, port);
    info!(addr = %addr, "Starting metrics server");

    let app = metrics_router(metrics);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn metrics_creation() {
        let metrics = AutoscalerMetrics::new();
        metrics.record_reconcile("node_pool");
        metrics.set_is_leader(true);

        let output = metrics.export();
        assert!(output.contains("autoscaler_reconcile_total"));
        assert!(output.contains("autoscaler_is_leader"));
    }

    #[test]
    fn metrics_export_format() {
        let metrics = AutoscalerMetrics::new();
        let output = metrics.export();
        // Should be valid prometheus text format
        assert!(output.contains("# HELP"));
        assert!(output.contains("# TYPE"));
    }
}
