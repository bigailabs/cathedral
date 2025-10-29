use crate::server::AppState;
use axum::{extract::State, http::StatusCode, response::IntoResponse};

pub async fn metrics_handler(State(state): State<AppState>) -> impl IntoResponse {
    match state.metrics {
        Some(metrics) => (StatusCode::OK, metrics.render_prometheus()).into_response(),
        None => (
            StatusCode::OK,
            "# Metrics collection disabled\n".to_string(),
        )
            .into_response(),
    }
}
