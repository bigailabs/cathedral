use anyhow::Result;
use basilica_common::metrics::{MetricTimer, MetricsRecorder};
use metrics::{
    counter, describe_counter, describe_gauge, describe_histogram, gauge, histogram, Unit,
};
use metrics_exporter_prometheus::PrometheusBuilder;

pub struct PrometheusMetricsRecorder {
    handle: metrics_exporter_prometheus::PrometheusHandle,
}

impl PrometheusMetricsRecorder {
    pub fn new() -> Result<Self> {
        let builder = PrometheusBuilder::new();
        let handle = builder
            .install_recorder()
            .map_err(|e| anyhow::anyhow!("Failed to install Prometheus recorder: {}", e))?;

        Self::register_standard_metrics();

        Ok(Self { handle })
    }

    fn register_standard_metrics() {
        describe_counter!(
            "basilica_billing_credits_applied_total",
            Unit::Count,
            "Total credits applied to user accounts"
        );

        describe_counter!(
            "basilica_billing_rentals_tracked_total",
            Unit::Count,
            "Total rentals tracked"
        );

        describe_counter!(
            "basilica_billing_rentals_finalized_total",
            Unit::Count,
            "Total rentals finalized"
        );

        describe_gauge!(
            "basilica_billing_rentals_active",
            Unit::Count,
            "Currently active rentals"
        );

        describe_gauge!(
            "basilica_billing_total_credits_balance",
            Unit::Count,
            "Total credits balance across all users"
        );

        describe_counter!(
            "basilica_billing_events_processed_total",
            Unit::Count,
            "Total billing events processed"
        );

        describe_counter!(
            "basilica_billing_events_failed_total",
            Unit::Count,
            "Total billing events that failed processing"
        );

        describe_gauge!(
            "basilica_billing_event_queue_size",
            Unit::Count,
            "Current size of unprocessed event queue"
        );

        describe_counter!(
            "basilica_billing_telemetry_received_total",
            Unit::Count,
            "Total telemetry data points received"
        );

        describe_counter!(
            "basilica_billing_telemetry_dropped_total",
            Unit::Count,
            "Total telemetry data points dropped"
        );

        describe_gauge!(
            "basilica_billing_telemetry_buffer_size",
            Unit::Count,
            "Current telemetry buffer size"
        );

        describe_counter!(
            "basilica_billing_rules_applied_total",
            Unit::Count,
            "Total billing rules applied"
        );

        describe_counter!(
            "basilica_billing_rules_evaluated_total",
            Unit::Count,
            "Total billing rules evaluated"
        );

        describe_gauge!(
            "basilica_billing_processor_running",
            Unit::Count,
            "Event processor running status (1 = running, 0 = stopped)"
        );

        describe_histogram!(
            "basilica_billing_grpc_request_duration_seconds",
            Unit::Seconds,
            "Duration of gRPC request handling"
        );

        describe_counter!(
            "basilica_billing_grpc_requests_total",
            Unit::Count,
            "Total number of gRPC requests"
        );

        describe_histogram!(
            "basilica_billing_event_processing_duration_seconds",
            Unit::Seconds,
            "Duration of event processing"
        );

        describe_histogram!(
            "basilica_billing_aggregation_duration_seconds",
            Unit::Seconds,
            "Duration of aggregation jobs"
        );

        describe_histogram!(
            "basilica_billing_database_query_duration_seconds",
            Unit::Seconds,
            "Duration of database queries"
        );

        describe_counter!(
            "basilica_billing_database_errors_total",
            Unit::Count,
            "Total database errors encountered"
        );

        describe_gauge!(
            "basilica_billing_health_status",
            Unit::Count,
            "Health status of the billing service"
        );

        describe_counter!(
            "basilica_billing_aggregation_runs_total",
            Unit::Count,
            "Total aggregation job runs"
        );

        describe_counter!(
            "basilica_billing_aggregation_failures_total",
            Unit::Count,
            "Total aggregation job failures"
        );

        describe_gauge!(
            "basilica_billing_batch_size",
            Unit::Count,
            "Current batch size for event processing"
        );
    }

    pub fn render(&self) -> String {
        self.handle.render()
    }

    fn convert_labels(labels: &[(&str, &str)]) -> Vec<(String, String)> {
        labels
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }
}

#[async_trait::async_trait]
impl MetricsRecorder for PrometheusMetricsRecorder {
    async fn record_counter(&self, name: &str, value: u64, labels: &[(&str, &str)]) {
        let converted_labels = Self::convert_labels(labels);
        let name_owned = name.to_string();
        counter!(name_owned, &converted_labels).increment(value);
    }

    async fn record_gauge(&self, name: &str, value: f64, labels: &[(&str, &str)]) {
        let converted_labels = Self::convert_labels(labels);
        let name_owned = name.to_string();
        gauge!(name_owned, &converted_labels).set(value);
    }

    async fn record_histogram(&self, name: &str, value: f64, labels: &[(&str, &str)]) {
        let converted_labels = Self::convert_labels(labels);
        let name_owned = name.to_string();
        histogram!(name_owned, &converted_labels).record(value);
    }

    async fn increment_counter(&self, name: &str, labels: &[(&str, &str)]) {
        self.record_counter(name, 1, labels).await;
    }

    fn start_timer(&self, name: &str, labels: Vec<(&str, &str)>) -> MetricTimer {
        MetricTimer::new(name.to_string(), labels)
    }

    async fn record_timing(
        &self,
        name: &str,
        duration: std::time::Duration,
        labels: &[(&str, &str)],
    ) {
        self.record_histogram(name, duration.as_secs_f64(), labels)
            .await;
    }
}
