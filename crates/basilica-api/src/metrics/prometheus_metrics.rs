use anyhow::Result;
use basilica_common::metrics::{MetricTimer, MetricsRecorder};
use metrics::{counter, describe_counter, describe_histogram, gauge, histogram, Unit};
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
            "basilca_api_requests_total",
            Unit::Count,
            "Total HTTP requests"
        );

        describe_histogram!(
            "basilca_api_request_duration_seconds",
            Unit::Seconds,
            "HTTP request duration"
        );

        describe_counter!("basilca_api_errors_total", Unit::Count, "Total API errors");

        describe_counter!(
            "basilca_api_auth_attempts_total",
            Unit::Count,
            "Total authentication attempts"
        );

        describe_counter!(
            "basilca_api_auth_failures_total",
            Unit::Count,
            "Total authentication failures"
        );

        describe_counter!(
            "basilca_api_billing_requests_total",
            Unit::Count,
            "Total billing API requests"
        );

        describe_histogram!(
            "basilca_api_billing_duration_seconds",
            Unit::Seconds,
            "Billing request duration"
        );

        describe_counter!(
            "basilca_api_rate_limited_total",
            Unit::Count,
            "Total rate limited requests"
        );

        describe_counter!(
            "basilca_api_validator_requests_total",
            Unit::Count,
            "Total validator requests"
        );

        describe_counter!(
            "basilca_api_validator_errors_total",
            Unit::Count,
            "Total validator errors"
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
