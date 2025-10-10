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
