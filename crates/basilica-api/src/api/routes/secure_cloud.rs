//! Secure cloud (GPU aggregator) route handlers
//! These routes proxy requests to the aggregator service

use crate::server::AppState;
use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use basilica_aggregator::api::query::GpuPriceQuery;
use serde_json::json;

/// List GPU prices from aggregator service
/// This is a thin proxy to the aggregator's get_gpu_prices handler
pub async fn list_gpu_prices(
    State(state): State<AppState>,
    Query(query): Query<GpuPriceQuery>,
) -> impl IntoResponse {
    match state.aggregator_service.get_offerings().await {
        Ok(mut offerings) => {
            // Apply filters (same logic as aggregator handler)
            if let Some(gpu_type) = query.gpu_type() {
                offerings.retain(|o| o.gpu_type == gpu_type);
            }

            if let Some(region) = &query.region {
                let region_lower = region.to_lowercase();
                offerings.retain(|o| o.region.contains(&region_lower));
            }

            if let Some(provider) = query.provider() {
                offerings.retain(|o| o.provider == provider);
            }

            if let Some(min_price) = query.min_price() {
                offerings.retain(|o| o.hourly_rate >= min_price);
            }

            if let Some(max_price) = query.max_price() {
                offerings.retain(|o| o.hourly_rate <= max_price);
            }

            if query.available_only.unwrap_or(false) {
                offerings.retain(|o| o.availability);
            }

            // Sort results
            match query.sort_by.as_deref() {
                Some("price") => offerings.sort_by_key(|o| o.hourly_rate),
                Some("gpu_type") => offerings.sort_by_key(|o| o.gpu_type.as_str().to_string()),
                Some("region") => offerings.sort_by(|a, b| a.region.cmp(&b.region)),
                _ => {}
            }

            // raw_metadata is automatically excluded via #[serde(skip)]
            let total_count = offerings.len();

            (
                StatusCode::OK,
                Json(json!({
                    "nodes": offerings,
                    "count": total_count,
                })),
            )
        }
        Err(e) => {
            tracing::error!("Failed to get offerings: {}", e);
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "Failed to fetch GPU prices",
                    "message": e.to_string()
                })),
            )
        }
    }
}
