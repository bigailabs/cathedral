use crate::server::AppState;
use axum::{body::Body, extract::State, http::Request, middleware::Next, response::Response};
use std::time::Instant;

pub async fn metrics_middleware(
    State(state): State<AppState>,
    req: Request<Body>,
    next: Next,
) -> Response {
    let method = req.method().to_string();
    let path = req.uri().path().to_string();
    let start = Instant::now();

    let response = next.run(req).await;

    if let Some(metrics) = &state.metrics {
        let duration = start.elapsed();
        let status = response.status().as_u16();

        let api_metrics = metrics.api_metrics();

        tokio::spawn(async move {
            api_metrics
                .record_request_duration(&method, &path, status, duration)
                .await;
            api_metrics
                .record_request_count(&method, &path, status)
                .await;
        });
    }

    response
}

impl crate::metrics::ApiMetrics {
    async fn record_request_duration(
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
        self.recorder()
            .record_histogram(
                crate::metrics::ApiMetricNames::REQUEST_DURATION,
                duration.as_secs_f64(),
                labels,
            )
            .await;
    }

    async fn record_request_count(&self, method: &str, path: &str, status: u16) {
        let status_str = status.to_string();
        let labels = &[
            ("method", method),
            ("path", path),
            ("status", status_str.as_str()),
        ];
        self.recorder()
            .increment_counter(crate::metrics::ApiMetricNames::REQUESTS_TOTAL, labels)
            .await;
    }
}
