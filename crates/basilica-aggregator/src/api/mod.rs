pub mod handlers;
pub mod query;

use crate::service::AggregatorService;
use axum::{routing::get, Router};
use std::sync::Arc;
use tower_http::trace::TraceLayer;

pub fn create_router(service: Arc<AggregatorService>) -> Router {
    Router::new()
        .route("/gpu-prices", get(handlers::get_gpu_prices))
        .route("/health", get(handlers::health_check))
        .route("/providers", get(handlers::get_providers))
        .layer(TraceLayer::new_for_http())
        .with_state(service)
}
