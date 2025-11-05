use crate::api::query::GpuPriceQuery;
use crate::service::AggregatorService;
use axum::{extract::Query, extract::State, http::StatusCode, response::IntoResponse, Json};
use serde_json::json;
use std::sync::Arc;

pub async fn get_gpu_prices(
    State(service): State<Arc<AggregatorService>>,
    Query(query): Query<GpuPriceQuery>,
) -> impl IntoResponse {
    match service.get_offerings().await {
        Ok(mut offerings) => {
            // Apply filters
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
                _ => {} // No sorting
            }

            // Remove raw_metadata before returning
            let response: Vec<_> = offerings
                .into_iter()
                .map(|mut o| {
                    o.raw_metadata = json!(null);
                    o
                })
                .collect();

            let total_count = response.len();

            (
                StatusCode::OK,
                Json(json!({
                    "offerings": response,
                    "total_count": total_count,
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

pub async fn health_check(State(service): State<Arc<AggregatorService>>) -> impl IntoResponse {
    match service.get_provider_health().await {
        Ok(statuses) => (StatusCode::OK, Json(json!({ "providers": statuses }))),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "Health check failed",
                "message": e.to_string()
            })),
        ),
    }
}

pub async fn get_providers(State(service): State<Arc<AggregatorService>>) -> impl IntoResponse {
    match service.get_provider_health().await {
        Ok(statuses) => (StatusCode::OK, Json(json!({ "providers": statuses }))),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "Failed to get provider status",
                "message": e.to_string()
            })),
        ),
    }
}
