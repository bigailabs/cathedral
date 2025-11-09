pub mod handlers;
pub mod query;

use crate::service::AggregatorService;
use axum::{
    routing::{delete, get, post},
    Router,
};
use std::sync::Arc;
use tower_http::trace::TraceLayer;

pub fn create_router(service: Arc<AggregatorService>) -> Router {
    Router::new()
        // GPU pricing endpoints
        .route("/gpu-prices", get(handlers::get_gpu_prices))
        .route("/health", get(handlers::health_check))
        .route("/providers", get(handlers::get_providers))
        // Deployment endpoints
        .route("/deployments", post(handlers::create_deployment))
        .route("/deployments", get(handlers::list_deployments))
        .route("/deployments/:id", get(handlers::get_deployment))
        .route("/deployments/:id", delete(handlers::delete_deployment))
        .route(
            "/deployments/:id/instance",
            get(handlers::get_instance_details),
        )
        // SSH key endpoints (user-centric)
        .route("/users/:user_id/ssh-key", post(handlers::register_ssh_key))
        .route("/users/:user_id/ssh-key", get(handlers::get_ssh_key))
        .route("/users/:user_id/ssh-key", delete(handlers::delete_ssh_key))
        // OS images endpoint
        .route("/images", get(handlers::list_images))
        .layer(TraceLayer::new_for_http())
        .with_state(service)
}
