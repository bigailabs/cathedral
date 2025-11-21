//! Health check route handler

use crate::server::AppState;
use axum::{extract::State, http::StatusCode, Json};
use basilica_sdk::types::HealthCheckResponse;
use serde_json::json;

/// Health check endpoint
pub async fn health_check(State(_state): State<AppState>) -> Json<HealthCheckResponse> {
    // We always have one configured validator
    // Health status is monitored in background but doesn't affect API availability
    Json(HealthCheckResponse {
        status: "healthy".to_string(),
        version: crate::VERSION.to_string(),
        timestamp: chrono::Utc::now(),
        healthy_validators: 1,
        total_validators: 1,
    })
}

/// K3s connectivity health check endpoint
pub async fn k3s_health_check(
) -> Result<Json<serde_json::Value>, (StatusCode, Json<serde_json::Value>)> {
    match crate::k8s::check_k3s_connectivity().await {
        Ok(()) => Ok(Json(json!({
            "status": "healthy",
            "service": "k3s",
            "timestamp": chrono::Utc::now()
        }))),
        Err(e) => Err((
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "status": "unhealthy",
                "service": "k3s",
                "error": e.to_string(),
                "timestamp": chrono::Utc::now()
            })),
        )),
    }
}
