use axum::{extract::State, http::StatusCode, response::IntoResponse, routing::get, Json, Router};
use serde::Serialize;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::info;

/// Health server state
#[derive(Clone)]
pub struct HealthState {
    inner: Arc<RwLock<HealthStatus>>,
}

#[derive(Clone, Default)]
struct HealthStatus {
    ready: bool,
    leader: bool,
    last_reconcile: Option<String>,
}

impl Default for HealthState {
    fn default() -> Self {
        Self::new()
    }
}

impl HealthState {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(RwLock::new(HealthStatus::default())),
        }
    }

    pub async fn set_ready(&self, ready: bool) {
        let mut status = self.inner.write().await;
        status.ready = ready;
    }

    pub async fn set_leader(&self, leader: bool) {
        let mut status = self.inner.write().await;
        status.leader = leader;
    }

    pub async fn set_last_reconcile(&self, time: String) {
        let mut status = self.inner.write().await;
        status.last_reconcile = Some(time);
    }
}

#[derive(Serialize)]
struct HealthResponse {
    status: &'static str,
}

#[derive(Serialize)]
struct ReadyResponse {
    ready: bool,
    leader: bool,
    last_reconcile: Option<String>,
}

async fn liveness() -> impl IntoResponse {
    (StatusCode::OK, Json(HealthResponse { status: "ok" }))
}

async fn readiness(State(state): State<HealthState>) -> impl IntoResponse {
    let status = state.inner.read().await;
    let response = ReadyResponse {
        ready: status.ready,
        leader: status.leader,
        last_reconcile: status.last_reconcile.clone(),
    };

    if status.ready {
        (StatusCode::OK, Json(response))
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, Json(response))
    }
}

/// Create health router
pub fn health_router(state: HealthState) -> Router {
    Router::new()
        .route("/healthz", get(liveness))
        .route("/readyz", get(readiness))
        .with_state(state)
}

/// Start health server
pub async fn start_health_server(
    host: &str,
    port: u16,
    state: HealthState,
) -> Result<(), std::io::Error> {
    let addr = format!("{}:{}", host, port);
    info!(addr = %addr, "Starting health server");

    let app = health_router(state);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    #[tokio::test]
    async fn liveness_returns_ok() {
        let state = HealthState::new();
        let app = health_router(state);

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/healthz")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn readiness_returns_unavailable_when_not_ready() {
        let state = HealthState::new();
        let app = health_router(state);

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/readyz")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn readiness_returns_ok_when_ready() {
        let state = HealthState::new();
        state.set_ready(true).await;
        let app = health_router(state);

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/readyz")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
    }
}
