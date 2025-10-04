//! API module for the Basilica API Gateway

pub mod auth;
pub mod extractors;
pub mod middleware;
pub mod routes;

use crate::server::AppState;
use axum::{
    routing::{delete, get, post},
    Router,
};
use crate::config::RentalBackend;

/// Create all API routes
pub fn routes(state: AppState) -> Router<AppState> {
    // Unprotected routes (for health checks, etc.)
    let public_routes = Router::new()
        // Health endpoint - no authentication required for ALB health checks
        .route("/health", get(routes::health::health_check));

    // Protected routes with unified authentication and scope validation
    let mut protected_routes = Router::new()
        .route("/jobs", post(routes::jobs::create_job))
        .route("/jobs/:id", get(routes::jobs::get_job_status).delete(routes::jobs::delete_job))
        .route("/jobs/:id/logs", get(routes::jobs::get_job_logs))
        // v2 rentals namespace is always available when k8s client exists
        .route("/v2/rentals", get(routes::rentals_v2::list_rentals).post(routes::rentals_v2::create_rental))
        .route(
            "/v2/rentals/:id",
            get(routes::rentals_v2::get_rental_status).delete(routes::rentals_v2::delete_rental),
        )
        .route(
            "/v2/rentals/:id/logs",
            get(routes::rentals_v2::stream_rental_logs),
        )
        .route(
            "/v2/rentals/:id/exec",
            post(routes::rentals_v2::exec_rental),
        )
        .route(
            "/v2/rentals/:id/extend",
            post(routes::rentals_v2::extend_rental),
        )
        .route("/nodes", get(routes::rentals::list_available_nodes))
        // API key management endpoints (JWT auth only)
        .route(
            "/api-keys",
            post(routes::api_keys::create_key).get(routes::api_keys::list_keys),
        )
        .route("/api-keys/:name", delete(routes::api_keys::revoke_key));

    // Conditionally map legacy vs k8s backend under /rentals
    let use_k8s_backend = matches!(state.config.rental_backend, RentalBackend::K8s) && state.k8s.is_some();
    if use_k8s_backend {
        protected_routes = protected_routes
            .route("/rentals", get(routes::rentals_v2::list_rentals).post(routes::rentals_v2::create_rental))
            .route(
                "/rentals/:id",
                get(routes::rentals_v2::get_rental_status).delete(routes::rentals_v2::delete_rental),
            )
            .route(
                "/rentals/:id/logs",
                get(routes::rentals_v2::stream_rental_logs),
            )
            .route(
                "/rentals/:id/exec",
                post(routes::rentals_v2::exec_rental),
            )
            .route(
                "/rentals/:id/extend",
                post(routes::rentals_v2::extend_rental),
            );
    } else {
        protected_routes = protected_routes
            .route("/rentals", get(routes::rentals::list_rentals_validator))
            .route("/rentals", post(routes::rentals::start_rental))
            .route("/rentals/:id", get(routes::rentals::get_rental_status))
            .route("/rentals/:id", delete(routes::rentals::stop_rental))
            .route(
                "/rentals/:id/logs",
                get(routes::rentals::stream_rental_logs),
            );
    }

    // Apply middleware layers
    let protected_routes = protected_routes
        .layer(axum::middleware::from_fn(
            middleware::scope_validation_middleware,
        ))
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            middleware::auth_middleware,
        ));

    // Build the router with both public and protected routes
    let router = Router::new()
        .merge(public_routes)
        .merge(protected_routes)
        .with_state(state.clone());

    // Apply general middleware
    middleware::apply_middleware(router, state)
}
