use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use anyhow::Result;
use basilica_common::config::types::MetricsConfig;
use basilica_common::metrics::MetricsRecorder;
use tokio::sync::RwLock;

#[derive(Default)]
struct MetricsStats {
    credits_applied: AtomicU64,
    rentals_tracked: AtomicU64,
    rentals_finalized: AtomicU64,
    events_processed: AtomicU64,
    events_failed: AtomicU64,
    telemetry_received: AtomicU64,
    telemetry_dropped: AtomicU64,
    rules_applied: AtomicU64,
    aggregation_runs: AtomicU64,
    database_errors: AtomicU64,
}

pub struct BillingBusinessMetrics {
    recorder: Arc<dyn MetricsRecorder>,
    stats: Arc<MetricsStats>,
    active_rentals_count: Arc<RwLock<usize>>,
    total_credits_balance: Arc<RwLock<f64>>,
    event_queue_size: Arc<RwLock<usize>>,
    telemetry_buffer_size: Arc<RwLock<usize>>,
    processor_running: Arc<RwLock<bool>>,
}

impl BillingBusinessMetrics {
    pub fn new(recorder: Arc<dyn MetricsRecorder>) -> Self {
        Self {
            recorder,
            stats: Arc::new(MetricsStats::default()),
            active_rentals_count: Arc::new(RwLock::new(0)),
            total_credits_balance: Arc::new(RwLock::new(0.0)),
            event_queue_size: Arc::new(RwLock::new(0)),
            telemetry_buffer_size: Arc::new(RwLock::new(0)),
            processor_running: Arc::new(RwLock::new(false)),
        }
    }

    pub async fn start_collection(&self, config: MetricsConfig) -> Result<()> {
        if !config.enabled {
            return Ok(());
        }

        let metrics = self.clone();
        let interval = config.collection_interval;

        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(interval);
            ticker.tick().await;

            loop {
                ticker.tick().await;
                if let Err(e) = metrics.collect_and_publish().await {
                    tracing::warn!("Failed to collect and publish billing metrics: {}", e);
                }
            }
        });

        Ok(())
    }

    async fn collect_and_publish(&self) -> Result<()> {
        let active_rentals = *self.active_rentals_count.read().await as f64;
        self.recorder
            .record_gauge("basilica_billing_rentals_active", active_rentals, &[])
            .await;

        let total_balance = *self.total_credits_balance.read().await;
        self.recorder
            .record_gauge("basilica_billing_total_credits_balance", total_balance, &[])
            .await;

        let queue_size = *self.event_queue_size.read().await as f64;
        self.recorder
            .record_gauge("basilica_billing_event_queue_size", queue_size, &[])
            .await;

        let buffer_size = *self.telemetry_buffer_size.read().await as f64;
        self.recorder
            .record_gauge("basilica_billing_telemetry_buffer_size", buffer_size, &[])
            .await;

        let processor_status = if *self.processor_running.read().await {
            1.0
        } else {
            0.0
        };
        self.recorder
            .record_gauge("basilica_billing_processor_running", processor_status, &[])
            .await;

        self.recorder
            .record_gauge("basilica_billing_health_status", 1.0, &[])
            .await;

        Ok(())
    }

    pub async fn record_credit_applied(&self, amount: f64, labels: &[(&str, &str)]) {
        self.stats.credits_applied.fetch_add(1, Ordering::Relaxed);
        let amount_units = (amount * 1000.0) as u64;
        self.recorder
            .record_counter(
                "basilica_billing_credits_applied_total",
                amount_units,
                labels,
            )
            .await;
    }

    pub async fn record_rental_tracked(&self, labels: &[(&str, &str)]) {
        self.stats.rentals_tracked.fetch_add(1, Ordering::Relaxed);
        self.recorder
            .increment_counter("basilica_billing_rentals_tracked_total", labels)
            .await;
    }

    pub async fn record_rental_finalized(&self, total_cost: f64, labels: &[(&str, &str)]) {
        self.stats.rentals_finalized.fetch_add(1, Ordering::Relaxed);
        let cost_units = (total_cost * 1000.0) as u64;
        self.recorder
            .record_counter(
                "basilica_billing_rentals_finalized_total",
                cost_units,
                labels,
            )
            .await;
    }

    pub async fn record_event_processed(&self, event_type: &str) {
        self.stats.events_processed.fetch_add(1, Ordering::Relaxed);
        self.recorder
            .increment_counter(
                "basilica_billing_events_processed_total",
                &[("event_type", event_type)],
            )
            .await;
    }

    pub async fn record_event_failed(&self, event_type: &str, reason: &str) {
        self.stats.events_failed.fetch_add(1, Ordering::Relaxed);
        self.recorder
            .increment_counter(
                "basilica_billing_events_failed_total",
                &[("event_type", event_type), ("reason", reason)],
            )
            .await;
    }

    pub async fn record_telemetry_received(&self, labels: &[(&str, &str)]) {
        self.stats
            .telemetry_received
            .fetch_add(1, Ordering::Relaxed);
        self.recorder
            .increment_counter("basilica_billing_telemetry_received_total", labels)
            .await;
    }

    pub async fn record_telemetry_dropped(&self, reason: &str) {
        self.stats.telemetry_dropped.fetch_add(1, Ordering::Relaxed);
        self.recorder
            .increment_counter(
                "basilica_billing_telemetry_dropped_total",
                &[("reason", reason)],
            )
            .await;
    }

    pub async fn record_rule_applied(&self, rule_type: &str) {
        self.stats.rules_applied.fetch_add(1, Ordering::Relaxed);
        self.recorder
            .increment_counter(
                "basilica_billing_rules_applied_total",
                &[("rule_type", rule_type)],
            )
            .await;
    }

    pub async fn record_aggregation_run(&self, aggregation_type: &str, success: bool) {
        self.stats.aggregation_runs.fetch_add(1, Ordering::Relaxed);
        let status = if success { "success" } else { "failure" };
        let labels = &[("type", aggregation_type), ("status", status)];
        self.recorder
            .increment_counter("basilica_billing_aggregation_runs_total", labels)
            .await;

        if !success {
            self.recorder
                .increment_counter(
                    "basilica_billing_aggregation_failures_total",
                    &[("type", aggregation_type)],
                )
                .await;
        }
    }

    pub async fn record_database_error(&self, operation: &str) {
        self.stats.database_errors.fetch_add(1, Ordering::Relaxed);
        self.recorder
            .increment_counter(
                "basilica_billing_database_errors_total",
                &[("operation", operation)],
            )
            .await;
    }

    pub async fn set_active_rentals_count(&self, count: usize) {
        *self.active_rentals_count.write().await = count;
    }

    pub async fn set_total_credits_balance(&self, balance: f64) {
        *self.total_credits_balance.write().await = balance;
    }

    pub async fn set_event_queue_size(&self, size: usize) {
        *self.event_queue_size.write().await = size;
    }

    pub async fn set_telemetry_buffer_size(&self, size: usize) {
        *self.telemetry_buffer_size.write().await = size;
    }

    pub async fn set_processor_running(&self, running: bool) {
        *self.processor_running.write().await = running;
    }

    pub fn get_stats(&self) -> BillingStats {
        BillingStats {
            credits_applied: self.stats.credits_applied.load(Ordering::Relaxed),
            rentals_tracked: self.stats.rentals_tracked.load(Ordering::Relaxed),
            rentals_finalized: self.stats.rentals_finalized.load(Ordering::Relaxed),
            events_processed: self.stats.events_processed.load(Ordering::Relaxed),
            events_failed: self.stats.events_failed.load(Ordering::Relaxed),
            telemetry_received: self.stats.telemetry_received.load(Ordering::Relaxed),
            telemetry_dropped: self.stats.telemetry_dropped.load(Ordering::Relaxed),
            rules_applied: self.stats.rules_applied.load(Ordering::Relaxed),
            aggregation_runs: self.stats.aggregation_runs.load(Ordering::Relaxed),
            database_errors: self.stats.database_errors.load(Ordering::Relaxed),
        }
    }
}

impl Clone for BillingBusinessMetrics {
    fn clone(&self) -> Self {
        Self {
            recorder: self.recorder.clone(),
            stats: self.stats.clone(),
            active_rentals_count: self.active_rentals_count.clone(),
            total_credits_balance: self.total_credits_balance.clone(),
            event_queue_size: self.event_queue_size.clone(),
            telemetry_buffer_size: self.telemetry_buffer_size.clone(),
            processor_running: self.processor_running.clone(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct BillingStats {
    pub credits_applied: u64,
    pub rentals_tracked: u64,
    pub rentals_finalized: u64,
    pub events_processed: u64,
    pub events_failed: u64,
    pub telemetry_received: u64,
    pub telemetry_dropped: u64,
    pub rules_applied: u64,
    pub aggregation_runs: u64,
    pub database_errors: u64,
}
