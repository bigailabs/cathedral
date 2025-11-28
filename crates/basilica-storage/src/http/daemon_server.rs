//! HTTP API server for multi-tenant FUSE daemon.
//!
//! Provides endpoints for mount lifecycle management, health checks, and metrics.

use crate::credentials::CredentialProvider;
use crate::daemon::{MountError, MountManager, MountStatus};
use crate::metrics::StorageMetrics;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tokio::net::TcpListener;
use tracing::info;

/// State shared across all HTTP handlers.
pub struct DaemonHttpState<P: CredentialProvider + 'static> {
    pub mount_manager: Arc<MountManager<P>>,
    pub metrics: Arc<StorageMetrics>,
}

/// HTTP server for multi-tenant FUSE daemon.
pub struct DaemonHttpServer<P: CredentialProvider + 'static> {
    state: Arc<DaemonHttpState<P>>,
}

impl<P: CredentialProvider + Send + Sync + 'static> DaemonHttpServer<P> {
    pub fn new(mount_manager: Arc<MountManager<P>>, metrics: Arc<StorageMetrics>) -> Self {
        Self {
            state: Arc::new(DaemonHttpState {
                mount_manager,
                metrics,
            }),
        }
    }

    pub async fn serve(self, addr: &str) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let app = Self::router(self.state);

        let listener = TcpListener::bind(addr).await?;
        info!("Daemon HTTP server listening on {}", addr);

        axum::serve(listener, app).await?;
        Ok(())
    }

    /// Build the router (useful for testing).
    pub fn router(state: Arc<DaemonHttpState<P>>) -> Router {
        Router::new()
            .route("/health", get(health_handler))
            .route("/ready", get(ready_handler::<P>))
            .route("/metrics", get(metrics_handler::<P>))
            .route("/mounts", get(list_mounts_handler::<P>))
            .route("/mounts/{namespace}", get(get_mount_handler::<P>))
            .route("/mounts/{namespace}", post(mount_handler::<P>))
            .route("/mounts/{namespace}", delete(unmount_handler::<P>))
            .with_state(state)
    }
}

// Response types

#[derive(Debug, Serialize)]
struct HealthResponse {
    status: &'static str,
    timestamp: String,
}

#[derive(Debug, Serialize)]
struct ReadyResponse {
    status: &'static str,
    active_mounts: u64,
    timestamp: String,
}

#[derive(Debug, Serialize)]
struct MountResponse {
    namespace: String,
    status: String,
    mount_path: String,
}

#[derive(Debug, Serialize)]
struct MountListResponse {
    mounts: Vec<MountResponse>,
    total: usize,
}

#[derive(Debug, Serialize)]
struct MountDetailResponse {
    namespace: String,
    mount_path: String,
    bucket: String,
    status: String,
    created_at: String,
    is_healthy: bool,
}

#[derive(Debug, Deserialize)]
pub struct MountRequest {
    #[serde(default)]
    pub force: bool,
}

#[derive(Debug, Serialize)]
struct ErrorResponse {
    error: String,
    code: &'static str,
}

#[derive(Debug, Serialize)]
struct SuccessResponse {
    message: String,
}

// Handlers

async fn health_handler() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "healthy",
        timestamp: chrono::Utc::now().to_rfc3339(),
    })
}

async fn ready_handler<P: CredentialProvider + Send + Sync + 'static>(
    State(state): State<Arc<DaemonHttpState<P>>>,
) -> Json<ReadyResponse> {
    let active_mounts = state.metrics.get_active_mounts();
    Json(ReadyResponse {
        status: "ready",
        active_mounts,
        timestamp: chrono::Utc::now().to_rfc3339(),
    })
}

async fn metrics_handler<P: CredentialProvider + Send + Sync + 'static>(
    State(state): State<Arc<DaemonHttpState<P>>>,
) -> String {
    let m = &state.metrics;

    format!(
        "# HELP storage_reads_total Total read operations\n\
         # TYPE storage_reads_total counter\n\
         storage_reads_total {}\n\
         \n\
         # HELP storage_writes_total Total write operations\n\
         # TYPE storage_writes_total counter\n\
         storage_writes_total {}\n\
         \n\
         # HELP storage_bytes_read_total Total bytes read\n\
         # TYPE storage_bytes_read_total counter\n\
         storage_bytes_read_total {}\n\
         \n\
         # HELP storage_bytes_written_total Total bytes written\n\
         # TYPE storage_bytes_written_total counter\n\
         storage_bytes_written_total {}\n\
         \n\
         # HELP storage_cache_hits_total Cache hit count\n\
         # TYPE storage_cache_hits_total counter\n\
         storage_cache_hits_total {}\n\
         \n\
         # HELP storage_cache_misses_total Cache miss count\n\
         # TYPE storage_cache_misses_total counter\n\
         storage_cache_misses_total {}\n\
         \n\
         # HELP storage_mounts_created_total Total mounts created\n\
         # TYPE storage_mounts_created_total counter\n\
         storage_mounts_created_total {}\n\
         \n\
         # HELP storage_mounts_destroyed_total Total mounts destroyed\n\
         # TYPE storage_mounts_destroyed_total counter\n\
         storage_mounts_destroyed_total {}\n\
         \n\
         # HELP storage_active_mounts Current active mounts\n\
         # TYPE storage_active_mounts gauge\n\
         storage_active_mounts {}\n",
        m.reads.load(Ordering::Relaxed),
        m.writes.load(Ordering::Relaxed),
        m.bytes_read.load(Ordering::Relaxed),
        m.bytes_written.load(Ordering::Relaxed),
        m.cache_hits.load(Ordering::Relaxed),
        m.cache_misses.load(Ordering::Relaxed),
        m.mounts_created.load(Ordering::Relaxed),
        m.mounts_destroyed.load(Ordering::Relaxed),
        m.active_mounts.load(Ordering::Relaxed),
    )
}

async fn list_mounts_handler<P: CredentialProvider + Send + Sync + 'static>(
    State(state): State<Arc<DaemonHttpState<P>>>,
) -> Json<MountListResponse> {
    let mounts = state.mount_manager.list_mounts().await;
    let responses: Vec<MountResponse> = mounts
        .into_iter()
        .map(|(ns, status, path)| MountResponse {
            namespace: ns,
            status: format_status(status),
            mount_path: path.display().to_string(),
        })
        .collect();
    let total = responses.len();
    Json(MountListResponse {
        mounts: responses,
        total,
    })
}

async fn get_mount_handler<P: CredentialProvider + Send + Sync + 'static>(
    State(state): State<Arc<DaemonHttpState<P>>>,
    Path(namespace): Path<String>,
) -> Result<Json<MountDetailResponse>, (StatusCode, Json<ErrorResponse>)> {
    let info = state.mount_manager.get_mount_info(&namespace).await;

    match info {
        Some(info) => Ok(Json(MountDetailResponse {
            namespace: info.namespace,
            mount_path: info.mount_path.display().to_string(),
            bucket: info.bucket,
            status: format_status(info.status),
            created_at: info.created_at.to_rfc3339(),
            is_healthy: info.is_healthy,
        })),
        None => Err((
            StatusCode::NOT_FOUND,
            Json(ErrorResponse {
                error: format!("Mount not found for namespace '{}'", namespace),
                code: "MOUNT_NOT_FOUND",
            }),
        )),
    }
}

async fn mount_handler<P: CredentialProvider + Send + Sync + 'static>(
    State(state): State<Arc<DaemonHttpState<P>>>,
    Path(namespace): Path<String>,
) -> Result<(StatusCode, Json<SuccessResponse>), (StatusCode, Json<ErrorResponse>)> {
    tracing::info!(
        target: "security_audit",
        event_type = "mount_api_request",
        severity = "info",
        namespace = %namespace,
        "Received mount request via HTTP API"
    );

    match state.mount_manager.mount_namespace(&namespace).await {
        Ok(()) => Ok((
            StatusCode::CREATED,
            Json(SuccessResponse {
                message: format!("Mount created for namespace '{}'", namespace),
            }),
        )),
        Err(MountError::AlreadyMounted(_)) => Err((
            StatusCode::CONFLICT,
            Json(ErrorResponse {
                error: format!("Mount already exists for namespace '{}'", namespace),
                code: "MOUNT_EXISTS",
            }),
        )),
        Err(MountError::SecurityViolation(msg)) => {
            tracing::warn!(
                target: "security_audit",
                event_type = "mount_api_denied",
                severity = "warning",
                namespace = %namespace,
                reason = %msg,
                "Mount request denied due to security violation"
            );
            Err((
                StatusCode::FORBIDDEN,
                Json(ErrorResponse {
                    error: msg,
                    code: "SECURITY_VIOLATION",
                }),
            ))
        }
        Err(MountError::CredentialError(e)) => Err((
            StatusCode::UNAUTHORIZED,
            Json(ErrorResponse {
                error: format!("Failed to get credentials: {}", e),
                code: "CREDENTIAL_ERROR",
            }),
        )),
        Err(e) => Err((
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(ErrorResponse {
                error: format!("Failed to create mount: {}", e),
                code: "MOUNT_FAILED",
            }),
        )),
    }
}

async fn unmount_handler<P: CredentialProvider + Send + Sync + 'static>(
    State(state): State<Arc<DaemonHttpState<P>>>,
    Path(namespace): Path<String>,
) -> Result<Json<SuccessResponse>, (StatusCode, Json<ErrorResponse>)> {
    tracing::info!(
        target: "security_audit",
        event_type = "unmount_api_request",
        severity = "info",
        namespace = %namespace,
        "Received unmount request via HTTP API"
    );

    match state.mount_manager.unmount_namespace(&namespace).await {
        Ok(()) => Ok(Json(SuccessResponse {
            message: format!("Mount destroyed for namespace '{}'", namespace),
        })),
        Err(MountError::NotFound(_)) => Err((
            StatusCode::NOT_FOUND,
            Json(ErrorResponse {
                error: format!("Mount not found for namespace '{}'", namespace),
                code: "MOUNT_NOT_FOUND",
            }),
        )),
        Err(MountError::SecurityViolation(msg)) => {
            tracing::warn!(
                target: "security_audit",
                event_type = "unmount_api_denied",
                severity = "warning",
                namespace = %namespace,
                reason = %msg,
                "Unmount request denied due to security violation"
            );
            Err((
                StatusCode::FORBIDDEN,
                Json(ErrorResponse {
                    error: msg,
                    code: "SECURITY_VIOLATION",
                }),
            ))
        }
        Err(e) => Err((
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(ErrorResponse {
                error: format!("Failed to destroy mount: {}", e),
                code: "UNMOUNT_FAILED",
            }),
        )),
    }
}

fn format_status(status: MountStatus) -> String {
    match status {
        MountStatus::Creating => "creating".to_string(),
        MountStatus::Active => "active".to_string(),
        MountStatus::Unhealthy => "unhealthy".to_string(),
        MountStatus::Destroying => "destroying".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_format_status() {
        assert_eq!(format_status(MountStatus::Creating), "creating");
        assert_eq!(format_status(MountStatus::Active), "active");
        assert_eq!(format_status(MountStatus::Unhealthy), "unhealthy");
        assert_eq!(format_status(MountStatus::Destroying), "destroying");
    }

    #[tokio::test]
    async fn test_health_handler() {
        let response = health_handler().await;
        assert_eq!(response.0.status, "healthy");
    }
}
