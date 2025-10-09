use std::sync::Arc;

use basilica_common::metrics::MetricsRecorder;

pub struct ApiMetricNames;

impl ApiMetricNames {
    pub const REQUESTS_TOTAL: &'static str = "basilca_api_requests_total";
    pub const REQUEST_DURATION: &'static str = "basilca_api_request_duration_seconds";
    pub const ERRORS_TOTAL: &'static str = "basilca_api_errors_total";
    pub const AUTH_ATTEMPTS: &'static str = "basilca_api_auth_attempts_total";
    pub const AUTH_FAILURES: &'static str = "basilca_api_auth_failures_total";
    pub const BILLING_REQUESTS: &'static str = "basilca_api_billing_requests_total";
    pub const BILLING_DURATION: &'static str = "basilca_api_billing_duration_seconds";
    pub const RATE_LIMITED: &'static str = "basilca_api_rate_limited_total";
    pub const VALIDATOR_REQUESTS: &'static str = "basilca_api_validator_requests_total";
    pub const VALIDATOR_ERRORS: &'static str = "basilca_api_validator_errors_total";
}

pub const API_METRIC_NAMES: ApiMetricNames = ApiMetricNames;

pub struct ApiMetrics {
    recorder: Arc<dyn MetricsRecorder>,
}

impl ApiMetrics {
    pub fn new(recorder: Arc<dyn MetricsRecorder>) -> Self {
        Self { recorder }
    }

    pub fn recorder(&self) -> &Arc<dyn MetricsRecorder> {
        &self.recorder
    }

    pub fn start_request_timer(
        &self,
        method: &str,
        path: &str,
    ) -> basilica_common::metrics::MetricTimer {
        self.recorder.start_timer(
            ApiMetricNames::REQUEST_DURATION,
            vec![("method", method), ("path", path)],
        )
    }

    pub async fn record_request(
        &self,
        timer: basilica_common::metrics::MetricTimer,
        method: &str,
        path: &str,
        status: u16,
    ) {
        let status_str = status.to_string();
        let labels = &[
            ("method", method),
            ("path", path),
            ("status", status_str.as_str()),
        ];

        timer.finish(&*self.recorder).await;
        self.recorder
            .increment_counter(ApiMetricNames::REQUESTS_TOTAL, labels)
            .await;
    }

    pub async fn record_error(&self, method: &str, path: &str, error_type: &str) {
        let labels = &[
            ("method", method),
            ("path", path),
            ("error_type", error_type),
        ];
        self.recorder
            .increment_counter(ApiMetricNames::ERRORS_TOTAL, labels)
            .await;
    }

    pub async fn record_auth_attempt(&self, auth_type: &str, success: bool) {
        let status = if success { "success" } else { "failure" };
        let labels = &[("auth_type", auth_type), ("status", status)];

        self.recorder
            .increment_counter(ApiMetricNames::AUTH_ATTEMPTS, labels)
            .await;

        if !success {
            self.recorder
                .increment_counter(ApiMetricNames::AUTH_FAILURES, labels)
                .await;
        }
    }

    pub fn start_billing_timer(&self, operation: &str) -> basilica_common::metrics::MetricTimer {
        self.recorder.start_timer(
            ApiMetricNames::BILLING_DURATION,
            vec![("operation", operation)],
        )
    }

    pub async fn record_billing_request(
        &self,
        timer: basilica_common::metrics::MetricTimer,
        operation: &str,
        success: bool,
    ) {
        let status = if success { "success" } else { "failure" };
        let labels = &[("operation", operation), ("status", status)];

        timer.finish(&*self.recorder).await;
        self.recorder
            .increment_counter(ApiMetricNames::BILLING_REQUESTS, labels)
            .await;
    }

    pub async fn record_rate_limited(&self, key_type: &str) {
        let labels = &[("key_type", key_type)];
        self.recorder
            .increment_counter(ApiMetricNames::RATE_LIMITED, labels)
            .await;
    }

    pub async fn record_validator_request(&self, validator_id: &str, method: &str, success: bool) {
        let status = if success { "success" } else { "failure" };
        let labels = &[
            ("validator_id", validator_id),
            ("method", method),
            ("status", status),
        ];

        self.recorder
            .increment_counter(ApiMetricNames::VALIDATOR_REQUESTS, labels)
            .await;

        if !success {
            self.recorder
                .increment_counter(ApiMetricNames::VALIDATOR_ERRORS, labels)
                .await;
        }
    }

    pub(crate) async fn record_request_duration(
        &self,
        method: &str,
        path: &str,
        status: u16,
        duration: std::time::Duration,
    ) {
        let status_str = status.to_string();
        let labels = &[
            ("method", method),
            ("path", path),
            ("status", status_str.as_str()),
        ];
        self.recorder
            .record_histogram(
                ApiMetricNames::REQUEST_DURATION,
                duration.as_secs_f64(),
                labels,
            )
            .await;
    }

    pub(crate) async fn record_request_count(&self, method: &str, path: &str, status: u16) {
        let status_str = status.to_string();
        let labels = &[
            ("method", method),
            ("path", path),
            ("status", status_str.as_str()),
        ];
        self.recorder
            .increment_counter(ApiMetricNames::REQUESTS_TOTAL, labels)
            .await;
    }
}

impl Clone for ApiMetrics {
    fn clone(&self) -> Self {
        Self {
            recorder: self.recorder.clone(),
        }
    }
}
