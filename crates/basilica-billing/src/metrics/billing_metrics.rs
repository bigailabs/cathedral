use std::sync::Arc;

use anyhow::Result;
use basilica_common::config::types::MetricsConfig;
use basilica_common::metrics::MetricsRecorder;

pub struct BillingMetricNames;

impl BillingMetricNames {
    pub const CREDITS_APPLIED: &'static str = "basilca_billing_credits_applied_total";
    pub const RENTALS_TRACKED: &'static str = "basilca_billing_rentals_tracked_total";
    pub const RENTALS_FINALIZED: &'static str = "basilca_billing_rentals_finalized_total";
    pub const RENTALS_ACTIVE: &'static str = "basilca_billing_rentals_active";
    pub const TOTAL_CREDITS_BALANCE: &'static str = "basilca_billing_total_credits_balance";

    pub const EVENTS_PROCESSED: &'static str = "basilca_billing_events_processed_total";
    pub const EVENTS_FAILED: &'static str = "basilca_billing_events_failed_total";
    pub const EVENT_QUEUE_SIZE: &'static str = "basilca_billing_event_queue_size";

    pub const TELEMETRY_RECEIVED: &'static str = "basilca_billing_telemetry_received_total";
    pub const TELEMETRY_DROPPED: &'static str = "basilca_billing_telemetry_dropped_total";
    pub const TELEMETRY_BUFFER_SIZE: &'static str = "basilca_billing_telemetry_buffer_size";

    pub const RULES_APPLIED: &'static str = "basilca_billing_rules_applied_total";
    pub const RULES_EVALUATED: &'static str = "basilca_billing_rules_evaluated_total";

    pub const PROCESSOR_RUNNING: &'static str = "basilca_billing_processor_running";

    pub const GRPC_REQUESTS: &'static str = "basilca_billing_grpc_requests_total";
    pub const GRPC_REQUEST_DURATION: &'static str = "basilca_billing_grpc_request_duration_seconds";

    pub const EVENT_PROCESSING_DURATION: &'static str =
        "basilca_billing_event_processing_duration_seconds";
    pub const AGGREGATION_DURATION: &'static str = "basilca_billing_aggregation_duration_seconds";
    pub const DATABASE_QUERY_DURATION: &'static str =
        "basilca_billing_database_query_duration_seconds";
    pub const DATABASE_ERRORS: &'static str = "basilca_billing_database_errors_total";

    pub const RESERVATIONS_CREATED: &'static str = "basilca_billing_reservations_created_total";
    pub const RESERVATIONS_RELEASED: &'static str = "basilca_billing_reservations_released_total";

    pub const HEALTH_STATUS: &'static str = "basilca_billing_health_status";

    pub const AGGREGATION_RUNS: &'static str = "basilca_billing_aggregation_runs_total";
    pub const AGGREGATION_FAILURES: &'static str = "basilca_billing_aggregation_failures_total";
    pub const BATCH_SIZE: &'static str = "basilca_billing_batch_size";

    pub const PROMO_CODE_VALIDATIONS: &'static str = "basilca_billing_promo_code_validations_total";
    pub const PROMO_CODE_APPLIED: &'static str = "basilca_billing_promo_code_applied_total";
    pub const PROMO_CODE_FAILURES: &'static str = "basilca_billing_promo_code_failures_total";
    pub const DISCOUNT_AMOUNT: &'static str = "basilca_billing_discount_amount_total";
    pub const TIER_DISCOUNT_APPLIED: &'static str = "basilca_billing_tier_discount_applied_total";
}

pub const BILLING_METRIC_NAMES: BillingMetricNames = BillingMetricNames;

pub struct BillingMetrics {
    recorder: Arc<dyn MetricsRecorder>,
}

impl BillingMetrics {
    pub fn new(recorder: Arc<dyn MetricsRecorder>) -> Self {
        Self { recorder }
    }

    pub async fn start_collection(&self, config: MetricsConfig) -> Result<()> {
        if !config.enabled {
            return Ok(());
        }

        tracing::debug!("Billing metrics collection started");
        Ok(())
    }

    pub fn start_grpc_timer(&self) -> basilica_common::metrics::MetricTimer {
        self.recorder
            .start_timer(BillingMetricNames::GRPC_REQUEST_DURATION, vec![])
    }

    pub async fn record_grpc_request(
        &self,
        timer: basilica_common::metrics::MetricTimer,
        method: &str,
        status: &str,
    ) {
        let labels = &[("method", method), ("status", status)];

        timer.finish(&*self.recorder).await;
        self.recorder
            .increment_counter(BillingMetricNames::GRPC_REQUESTS, labels)
            .await;
    }

    pub fn start_event_processing_timer(&self) -> basilica_common::metrics::MetricTimer {
        self.recorder
            .start_timer(BillingMetricNames::EVENT_PROCESSING_DURATION, vec![])
    }

    pub async fn record_event_processed(
        &self,
        timer: basilica_common::metrics::MetricTimer,
        event_type: &str,
        success: bool,
    ) {
        let status = if success { "success" } else { "failure" };
        let labels = &[("event_type", event_type), ("status", status)];

        timer.finish(&*self.recorder).await;

        if success {
            self.recorder
                .increment_counter(BillingMetricNames::EVENTS_PROCESSED, labels)
                .await;
        } else {
            self.recorder
                .increment_counter(BillingMetricNames::EVENTS_FAILED, labels)
                .await;
        }
    }

    pub fn start_aggregation_timer(
        &self,
        aggregation_type: &str,
    ) -> basilica_common::metrics::MetricTimer {
        self.recorder.start_timer(
            BillingMetricNames::AGGREGATION_DURATION,
            vec![("type", aggregation_type)],
        )
    }

    pub async fn record_aggregation_complete(
        &self,
        timer: basilica_common::metrics::MetricTimer,
        aggregation_type: &str,
        success: bool,
        events_aggregated: u64,
    ) {
        let status = if success { "success" } else { "failure" };
        let agg_labels = &[("type", aggregation_type), ("status", status)];
        let event_labels = &[("event_type", aggregation_type), ("status", status)];

        timer.finish(&*self.recorder).await;

        if success {
            self.recorder
                .increment_counter(BillingMetricNames::AGGREGATION_RUNS, agg_labels)
                .await;
            self.recorder
                .record_counter(
                    BillingMetricNames::EVENTS_PROCESSED,
                    events_aggregated,
                    event_labels,
                )
                .await;
        } else {
            self.recorder
                .increment_counter(BillingMetricNames::AGGREGATION_FAILURES, agg_labels)
                .await;
        }
    }

    pub fn start_database_timer(&self, operation: &str) -> basilica_common::metrics::MetricTimer {
        self.recorder.start_timer(
            BillingMetricNames::DATABASE_QUERY_DURATION,
            vec![("operation", operation)],
        )
    }

    pub async fn record_database_operation(
        &self,
        timer: basilica_common::metrics::MetricTimer,
        operation: &str,
        success: bool,
    ) {
        let status = if success { "success" } else { "failure" };
        let labels = &[("operation", operation), ("status", status)];

        timer.finish(&*self.recorder).await;

        if !success {
            self.recorder
                .increment_counter(BillingMetricNames::DATABASE_ERRORS, labels)
                .await;
        }
    }

    pub async fn record_credit_applied(&self, amount: f64, user_id: &str) {
        let labels = &[("user_id", user_id)];
        let amount_units = (amount * 1000.0) as u64;
        self.recorder
            .record_counter(BillingMetricNames::CREDITS_APPLIED, amount_units, labels)
            .await;
    }

    pub async fn record_rental_tracked(&self, rental_id: &str, package_id: &str) {
        let labels = &[("rental_id", rental_id), ("package_id", package_id)];
        self.recorder
            .increment_counter(BillingMetricNames::RENTALS_TRACKED, labels)
            .await;
    }

    pub async fn record_rental_finalized(&self, rental_id: &str, total_cost: f64) {
        let labels = &[("rental_id", rental_id)];
        let cost_units = (total_cost * 1000.0) as u64;
        self.recorder
            .record_counter(BillingMetricNames::RENTALS_FINALIZED, cost_units, labels)
            .await;
    }

    pub async fn record_reservation_created(&self, reservation_id: &str, amount: f64) {
        let labels = &[("reservation_id", reservation_id)];
        let amount_units = (amount * 1000.0) as u64;
        self.recorder
            .record_counter(
                BillingMetricNames::RESERVATIONS_CREATED,
                amount_units,
                labels,
            )
            .await;
    }

    pub async fn record_reservation_released(&self, reservation_id: &str) {
        let labels = &[("reservation_id", reservation_id)];
        self.recorder
            .increment_counter(BillingMetricNames::RESERVATIONS_RELEASED, labels)
            .await;
    }

    pub async fn record_telemetry_received(&self, rental_id: &str) {
        let labels = &[("rental_id", rental_id)];
        self.recorder
            .increment_counter(BillingMetricNames::TELEMETRY_RECEIVED, labels)
            .await;
    }

    pub async fn record_telemetry_dropped(&self, reason: &str) {
        let labels = &[("reason", reason)];
        self.recorder
            .increment_counter(BillingMetricNames::TELEMETRY_DROPPED, labels)
            .await;
    }

    pub async fn record_rule_applied(&self, rule_id: &str, rule_type: &str) {
        let labels = &[("rule_id", rule_id), ("rule_type", rule_type)];
        self.recorder
            .increment_counter(BillingMetricNames::RULES_APPLIED, labels)
            .await;
    }

    pub async fn record_rule_evaluated(&self, rule_id: &str, matched: bool) {
        let status = if matched { "matched" } else { "not_matched" };
        let labels = &[("rule_id", rule_id), ("status", status)];
        self.recorder
            .increment_counter(BillingMetricNames::RULES_EVALUATED, labels)
            .await;
    }

    pub async fn set_processor_running(&self, running: bool) {
        let value = if running { 1.0 } else { 0.0 };
        self.recorder
            .record_gauge(BillingMetricNames::PROCESSOR_RUNNING, value, &[])
            .await;
    }

    pub async fn set_event_queue_size(&self, size: usize) {
        self.recorder
            .record_gauge(BillingMetricNames::EVENT_QUEUE_SIZE, size as f64, &[])
            .await;
    }

    pub async fn set_telemetry_buffer_size(&self, size: usize) {
        self.recorder
            .record_gauge(BillingMetricNames::TELEMETRY_BUFFER_SIZE, size as f64, &[])
            .await;
    }

    pub async fn set_active_rentals(&self, count: usize) {
        self.recorder
            .record_gauge(BillingMetricNames::RENTALS_ACTIVE, count as f64, &[])
            .await;
    }

    pub async fn set_health_status(&self, healthy: bool) {
        let value = if healthy { 1.0 } else { 0.0 };
        self.recorder
            .record_gauge(BillingMetricNames::HEALTH_STATUS, value, &[])
            .await;
    }

    pub async fn record_promo_code_validation(&self, code: &str, success: bool, reason: &str) {
        let status = if success { "success" } else { "failure" };
        let labels = &[("code", code), ("status", status), ("reason", reason)];
        self.recorder
            .increment_counter(BillingMetricNames::PROMO_CODE_VALIDATIONS, labels)
            .await;

        if !success {
            self.recorder
                .increment_counter(BillingMetricNames::PROMO_CODE_FAILURES, labels)
                .await;
        }
    }

    pub async fn record_promo_code_applied(&self, code: &str, discount_type: &str, amount: f64) {
        let labels = &[("code", code), ("discount_type", discount_type)];
        self.recorder
            .increment_counter(BillingMetricNames::PROMO_CODE_APPLIED, labels)
            .await;

        let amount_units = (amount * 1000.0) as u64;
        let discount_labels = &[("type", "promo_code"), ("code", code)];
        self.recorder
            .record_counter(
                BillingMetricNames::DISCOUNT_AMOUNT,
                amount_units,
                discount_labels,
            )
            .await;
    }

    pub async fn record_tier_discount_applied(&self, tier: &str, amount: f64) {
        let labels = &[("tier", tier)];
        self.recorder
            .increment_counter(BillingMetricNames::TIER_DISCOUNT_APPLIED, labels)
            .await;

        let amount_units = (amount * 1000.0) as u64;
        let discount_labels = &[("type", "tier"), ("tier", tier)];
        self.recorder
            .record_counter(
                BillingMetricNames::DISCOUNT_AMOUNT,
                amount_units,
                discount_labels,
            )
            .await;
    }
}

impl Clone for BillingMetrics {
    fn clone(&self) -> Self {
        Self {
            recorder: self.recorder.clone(),
        }
    }
}
