use axum::{extract::State, Json};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use tracing::{error, info, warn};

use crate::{
    api::middleware::AuthContext,
    error::{ApiError, Result},
    server::AppState,
};

#[derive(Debug, Deserialize)]
pub struct GpuNodeRegistrationRequest {
    pub node_id: String,
    pub datacenter_id: String,
    pub gpu_specs: GpuSpecs,
}

#[derive(Debug, Deserialize)]
pub struct GpuSpecs {
    pub count: u32,
    pub model: String,
    pub memory_gb: u32,
    pub driver_version: String,
    pub cuda_version: String,
}

#[derive(Debug, Serialize)]
pub struct GpuNodeRegistrationResponse {
    pub node_id: String,
    pub k3s_url: String,
    pub k3s_token: String,
    pub node_password: Option<String>,
    pub node_labels: HashMap<String, String>,
    pub status: String,
}

pub async fn register_gpu_node(
    State(state): State<AppState>,
    axum::Extension(auth_context): axum::Extension<AuthContext>,
    Json(req): Json<GpuNodeRegistrationRequest>,
) -> Result<Json<GpuNodeRegistrationResponse>> {
    let user_id = &auth_context.user_id;

    info!(
        "GPU node registration request from user {}: node_id={}, datacenter={}",
        user_id, req.node_id, req.datacenter_id
    );

    if user_id != &req.datacenter_id {
        return Err(ApiError::Authorization {
            message: format!(
                "User {} is not authorized to register nodes for datacenter {}",
                user_id, req.datacenter_id
            ),
        });
    }

    crate::k8s::validate_node_id(&req.node_id).map_err(|e| ApiError::InvalidRequest {
        message: e.to_string(),
    })?;
    validate_gpu_specs(&req.gpu_specs)?;

    let k3s_url = crate::k8s::get_k3s_server_url()
        .map_err(|e| ApiError::ConfigError(format!("K3S_SERVER_URL not configured: {}", e)))?;

    let (k3s_token, node_password) = if state.ssh_client.is_enabled() {
        info!(
            node_id = %req.node_id,
            datacenter_id = %req.datacenter_id,
            "Creating K3s token via SSH"
        );

        let token_response = state
            .ssh_client
            .create_token(&req.node_id, &req.datacenter_id, "24h")
            .await
            .map_err(|e| {
                error!(
                    node_id = %req.node_id,
                    error = %e,
                    "Failed to create K3s token via SSH"
                );
                e
            })?;

        (token_response.token, token_response.node_password)
    } else {
        warn!("SSH token creation disabled, using fallback to database-stored token");
        let token = crate::k8s::get_or_create_cluster_token(
            &state.db,
            user_id,
            &req.node_id,
            &req.datacenter_id,
        )
        .await?;
        (token, None)
    };

    let node_labels = crate::k8s::build_node_labels(crate::k8s::NodeLabelParams {
        node_id: &req.node_id,
        datacenter_id: &req.datacenter_id,
        gpu_model: &req.gpu_specs.model,
        gpu_count: req.gpu_specs.count,
        gpu_memory_gb: req.gpu_specs.memory_gb,
        driver_version: &req.gpu_specs.driver_version,
        cuda_version: &req.gpu_specs.cuda_version,
    });

    info!(
        node_id = %req.node_id,
        datacenter_id = %req.datacenter_id,
        "GPU node registration approved"
    );

    Ok(Json(GpuNodeRegistrationResponse {
        node_id: req.node_id,
        k3s_url,
        k3s_token,
        node_password,
        node_labels,
        status: "ready".to_string(),
    }))
}

fn validate_gpu_specs(specs: &GpuSpecs) -> Result<()> {
    if specs.count == 0 {
        return Err(ApiError::InvalidRequest {
            message: "gpu_specs.count must be greater than 0".into(),
        });
    }

    if specs.model.is_empty() {
        return Err(ApiError::InvalidRequest {
            message: "gpu_specs.model cannot be empty".into(),
        });
    }

    if specs.memory_gb == 0 {
        return Err(ApiError::InvalidRequest {
            message: "gpu_specs.memory_gb must be greater than 0".into(),
        });
    }

    if specs.driver_version.is_empty() {
        return Err(ApiError::InvalidRequest {
            message: "gpu_specs.driver_version cannot be empty".into(),
        });
    }

    if specs.cuda_version.is_empty() {
        return Err(ApiError::InvalidRequest {
            message: "gpu_specs.cuda_version cannot be empty".into(),
        });
    }

    Ok(())
}

#[derive(Debug, Deserialize)]
pub struct RevokeGpuNodeRequest {
    pub node_id: String,
    pub datacenter_id: String,
}

pub async fn revoke_gpu_node(
    State(state): State<AppState>,
    axum::Extension(auth_context): axum::Extension<AuthContext>,
    Json(req): Json<RevokeGpuNodeRequest>,
) -> Result<()> {
    let user_id = &auth_context.user_id;

    if user_id != &req.datacenter_id {
        return Err(ApiError::Authorization {
            message: format!(
                "User {} cannot revoke nodes for datacenter {}",
                user_id, req.datacenter_id
            ),
        });
    }

    crate::k8s::validate_node_id(&req.node_id).map_err(|e| ApiError::InvalidRequest {
        message: e.to_string(),
    })?;

    crate::k8s::revoke_cluster_token(&state.db, user_id, &req.node_id).await?;

    info!(
        node_id = %req.node_id,
        datacenter_id = %req.datacenter_id,
        "GPU node token revoked"
    );

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_node_id_valid() {
        assert!(crate::k8s::validate_node_id("node-1").is_ok());
        assert!(crate::k8s::validate_node_id("gpu-node-123").is_ok());
        assert!(crate::k8s::validate_node_id("dc1.node.001").is_ok());
    }

    #[test]
    fn test_validate_node_id_empty() {
        assert!(crate::k8s::validate_node_id("").is_err());
    }

    #[test]
    fn test_validate_node_id_too_long() {
        let long_id = "a".repeat(254);
        assert!(crate::k8s::validate_node_id(&long_id).is_err());
    }

    #[test]
    fn test_validate_node_id_invalid_chars() {
        assert!(crate::k8s::validate_node_id("node_1").is_err());
        assert!(crate::k8s::validate_node_id("node@1").is_err());
        assert!(crate::k8s::validate_node_id("node 1").is_err());
    }

    #[test]
    fn test_validate_node_id_invalid_start_end() {
        assert!(crate::k8s::validate_node_id("-node").is_err());
        assert!(crate::k8s::validate_node_id("node-").is_err());
    }

    #[test]
    fn test_validate_gpu_specs_valid() {
        let specs = GpuSpecs {
            count: 4,
            model: "A100".to_string(),
            memory_gb: 80,
            driver_version: "535.104.05".to_string(),
            cuda_version: "12.2".to_string(),
        };
        assert!(validate_gpu_specs(&specs).is_ok());
    }

    #[test]
    fn test_validate_gpu_specs_zero_count() {
        let specs = GpuSpecs {
            count: 0,
            model: "A100".to_string(),
            memory_gb: 80,
            driver_version: "535.104.05".to_string(),
            cuda_version: "12.2".to_string(),
        };
        assert!(validate_gpu_specs(&specs).is_err());
    }

    #[test]
    fn test_build_node_labels() {
        let labels = crate::k8s::build_node_labels(crate::k8s::NodeLabelParams {
            node_id: "node-1",
            datacenter_id: "dc-1",
            gpu_model: "A100",
            gpu_count: 4,
            gpu_memory_gb: 80,
            driver_version: "535.104.05",
            cuda_version: "12.2",
        });
        assert_eq!(
            labels.get("basilica.ai/node-type"),
            Some(&"gpu".to_string())
        );
        assert_eq!(
            labels.get("basilica.ai/datacenter"),
            Some(&"dc-1".to_string())
        );
        assert_eq!(
            labels.get("basilica.ai/node-id"),
            Some(&"node-1".to_string())
        );
        assert_eq!(
            labels.get("basilica.ai/gpu-model"),
            Some(&"A100".to_string())
        );
        assert_eq!(labels.get("basilica.ai/gpu-count"), Some(&"4".to_string()));
        assert_eq!(
            labels.get("basilica.ai/gpu-memory-gb"),
            Some(&"80".to_string())
        );
        assert_eq!(
            labels.get("basilica.ai/workloads-only"),
            Some(&"true".to_string())
        );
    }
}
