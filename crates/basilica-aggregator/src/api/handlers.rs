use crate::api::query::GpuPriceQuery;
use crate::service::AggregatorService;
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use serde::Deserialize;
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
            let nodes: Vec<_> = offerings
                .into_iter()
                .map(|mut o| {
                    o.raw_metadata = json!(null);
                    o
                })
                .collect();

            let total_count = nodes.len();

            (
                StatusCode::OK,
                Json(json!({
                    "nodes": nodes,
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

// ============================================================================
// Deployment Handlers
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct CreateDeploymentRequest {
    pub offering_id: String,
    pub ssh_public_key: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ssh_key_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub location_code: Option<String>,
}

pub async fn create_deployment(
    State(service): State<Arc<AggregatorService>>,
    Json(req): Json<CreateDeploymentRequest>,
) -> impl IntoResponse {
    match service
        .deploy_instance(
            req.offering_id,
            req.ssh_public_key,
            req.ssh_key_name,
            req.location_code,
        )
        .await
    {
        Ok(deployment) => (StatusCode::CREATED, Json(deployment)).into_response(),
        Err(e) => {
            tracing::error!("Failed to create deployment: {}", e);
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({
                    "error": "Failed to create deployment",
                    "message": e.to_string()
                })),
            )
                .into_response()
        }
    }
}

pub async fn get_deployment(
    State(service): State<Arc<AggregatorService>>,
    Path(deployment_id): Path<String>,
) -> impl IntoResponse {
    match service.get_deployment(&deployment_id).await {
        Ok(deployment) => (StatusCode::OK, Json(deployment)).into_response(),
        Err(e) => {
            let status = if e.to_string().contains("not found") {
                StatusCode::NOT_FOUND
            } else {
                StatusCode::INTERNAL_SERVER_ERROR
            };

            (
                status,
                Json(json!({
                    "error": "Failed to get deployment",
                    "message": e.to_string()
                })),
            )
                .into_response()
        }
    }
}

pub async fn get_instance_details(
    State(service): State<Arc<AggregatorService>>,
    Path(deployment_id): Path<String>,
) -> impl IntoResponse {
    match service.get_instance_details(&deployment_id).await {
        Ok(instance) => (StatusCode::OK, Json(instance)).into_response(),
        Err(e) => {
            let status = if e.to_string().contains("not found") {
                StatusCode::NOT_FOUND
            } else {
                StatusCode::INTERNAL_SERVER_ERROR
            };

            (
                status,
                Json(json!({
                    "error": "Failed to get instance details",
                    "message": e.to_string()
                })),
            )
                .into_response()
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct ListDeploymentsQuery {
    pub provider: Option<String>,
    pub status: Option<String>,
}

pub async fn list_deployments(
    State(service): State<Arc<AggregatorService>>,
    Query(query): Query<ListDeploymentsQuery>,
) -> impl IntoResponse {
    let provider = query.provider.and_then(|p| p.parse().ok());
    let status = query.status.and_then(|s| s.parse().ok());

    match service.list_deployments(provider, status).await {
        Ok(deployments) => (StatusCode::OK, Json(json!({ "deployments": deployments }))),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "Failed to list deployments",
                "message": e.to_string()
            })),
        ),
    }
}

pub async fn delete_deployment(
    State(service): State<Arc<AggregatorService>>,
    Path(deployment_id): Path<String>,
) -> impl IntoResponse {
    match service.delete_deployment(&deployment_id).await {
        Ok(_) => (
            StatusCode::OK,
            Json(json!({ "message": "Deployment deleted successfully" })),
        ),
        Err(e) => {
            let status = if e.to_string().contains("not found") {
                StatusCode::NOT_FOUND
            } else {
                StatusCode::INTERNAL_SERVER_ERROR
            };

            (
                status,
                Json(json!({
                    "error": "Failed to delete deployment",
                    "message": e.to_string()
                })),
            )
        }
    }
}

// ============================================================================
// SSH Key Handlers
// ============================================================================

pub async fn list_ssh_keys(State(service): State<Arc<AggregatorService>>) -> impl IntoResponse {
    match service.list_ssh_keys().await {
        Ok(keys) => (StatusCode::OK, Json(json!({ "ssh_keys": keys }))),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "Failed to list SSH keys",
                "message": e.to_string()
            })),
        ),
    }
}

#[derive(Debug, Deserialize)]
pub struct CreateSshKeyRequest {
    pub name: String,
    pub public_key: String,
}

pub async fn create_ssh_key(
    State(service): State<Arc<AggregatorService>>,
    Json(req): Json<CreateSshKeyRequest>,
) -> impl IntoResponse {
    match service.create_ssh_key(req.name, req.public_key).await {
        Ok(key) => (StatusCode::CREATED, Json(key)).into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "Failed to create SSH key",
                "message": e.to_string()
            })),
        )
            .into_response(),
    }
}

// ============================================================================
// OS Images Handler
// ============================================================================

pub async fn list_images(State(service): State<Arc<AggregatorService>>) -> impl IntoResponse {
    match service.list_images().await {
        Ok(images) => (StatusCode::OK, Json(json!({ "images": images }))),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "Failed to list images",
                "message": e.to_string()
            })),
        ),
    }
}

// ============================================================================
// SSH Key Management Handlers
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct RegisterSshKeyRequest {
    pub name: String,
    pub public_key: String,
}

#[derive(Debug, Deserialize)]
pub struct UpdateSshKeyRequest {
    pub name: String,
    pub public_key: String,
}

/// POST /users/:user_id/ssh-key
pub async fn register_ssh_key(
    Path(user_id): Path<String>,
    State(service): State<Arc<AggregatorService>>,
    Json(req): Json<RegisterSshKeyRequest>,
) -> impl IntoResponse {
    match service
        .register_ssh_key(user_id, req.name, req.public_key)
        .await
    {
        Ok(ssh_key) => {
            let response = crate::models::SshKeyResponse::from(ssh_key);
            (StatusCode::CREATED, Json(json!(response)))
        }
        Err(e) => {
            let status = if e.to_string().contains("already has") {
                StatusCode::CONFLICT
            } else {
                StatusCode::INTERNAL_SERVER_ERROR
            };
            (
                status,
                Json(json!({
                    "error": "Failed to register SSH key",
                    "message": e.to_string()
                })),
            )
        }
    }
}

/// GET /users/:user_id/ssh-key
pub async fn get_ssh_key(
    Path(user_id): Path<String>,
    State(service): State<Arc<AggregatorService>>,
) -> impl IntoResponse {
    match service.get_ssh_key(&user_id).await {
        Ok(Some(ssh_key)) => {
            let response = crate::models::SshKeyResponse::from(ssh_key);
            (StatusCode::OK, Json(json!(response)))
        }
        Ok(None) => (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "SSH key not found"
            })),
        ),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "error": "Failed to get SSH key",
                "message": e.to_string()
            })),
        ),
    }
}

/// PUT /users/:user_id/ssh-key
pub async fn update_ssh_key(
    Path(user_id): Path<String>,
    State(service): State<Arc<AggregatorService>>,
    Json(req): Json<UpdateSshKeyRequest>,
) -> impl IntoResponse {
    match service
        .update_ssh_key(user_id, req.name, req.public_key)
        .await
    {
        Ok(ssh_key) => {
            let response = crate::models::SshKeyResponse::from(ssh_key);
            (StatusCode::OK, Json(json!(response)))
        }
        Err(e) => {
            let status = if e.to_string().contains("not found") {
                StatusCode::NOT_FOUND
            } else {
                StatusCode::INTERNAL_SERVER_ERROR
            };
            (
                status,
                Json(json!({
                    "error": "Failed to update SSH key",
                    "message": e.to_string()
                })),
            )
        }
    }
}

/// DELETE /users/:user_id/ssh-key
pub async fn delete_ssh_key(
    Path(user_id): Path<String>,
    State(service): State<Arc<AggregatorService>>,
) -> impl IntoResponse {
    match service.delete_ssh_key(&user_id).await {
        Ok(()) => (StatusCode::NO_CONTENT, Json(json!({}))),
        Err(e) => {
            let status = if e.to_string().contains("not found") {
                StatusCode::NOT_FOUND
            } else {
                StatusCode::INTERNAL_SERVER_ERROR
            };
            (
                status,
                Json(json!({
                    "error": "Failed to delete SSH key",
                    "message": e.to_string()
                })),
            )
        }
    }
}
