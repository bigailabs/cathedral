use crate::api::middleware::AuthContext;
use crate::apimetrics;
use crate::db;
use crate::envoy::{EnvoyConfigManager, EnvoyRoute};
use crate::error::{ApiError, Result};
use crate::server::AppState;
use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    Extension, Json,
};
use serde::Serialize;

use super::types::{
    validate_image, validate_instance_name, validate_port, validate_replicas,
    CreateDeploymentRequest, DeploymentResponse,
};

fn user_namespace(user_id: &str) -> String {
    format!("u-{}", user_id.replace(['/', '.', '@'], "-"))
}

fn generate_cr_name(instance_name: &str) -> String {
    format!("{}-deployment", instance_name)
}

pub async fn create_deployment(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Json(req): Json<CreateDeploymentRequest>,
) -> Result<impl IntoResponse> {
    let start = std::time::Instant::now();

    validate_instance_name(&req.instance_name)?;
    validate_image(&req.image)?;
    validate_port(req.port)?;
    validate_replicas(req.replicas, state.config.deployment.max_replicas)?;

    if let Some(ref resources) = req.resources {
        super::types::validate_resources(resources)?;
    }

    let existing = db::get_deployment(&state.db, &auth.user_id, &req.instance_name).await?;
    if let Some(existing) = existing {
        if existing.state == "Active" || existing.state == "Pending" {
            apimetrics::record_request("POST /deployments", "200", start, true);
            return Ok((
                StatusCode::OK,
                Json(DeploymentResponse::from_record(&existing)),
            ));
        }
    }

    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;

    let namespace = user_namespace(&auth.user_id);
    let cr_name = generate_cr_name(&req.instance_name);
    let path_prefix = format!("/deployments/{}", req.instance_name);
    let public_url = state.config.deployment.generate_public_url(&path_prefix);

    k8s_client.create_namespace(&namespace).await?;

    let user_deployment_spec = serde_json::json!({
        "apiVersion": "basilica.ai/v1",
        "kind": "UserDeployment",
        "metadata": {
            "name": cr_name.clone(),
            "namespace": namespace.clone(),
        },
        "spec": {
            "userId": auth.user_id,
            "instanceName": req.instance_name.clone(),
            "image": req.image.clone(),
            "replicas": req.replicas,
            "port": req.port,
            "command": req.command.clone(),
            "args": req.args.clone(),
            "env": req.env.iter().map(|(k, v)| {
                serde_json::json!({
                    "name": k,
                    "value": v,
                })
            }).collect::<Vec<_>>(),
            "resources": {
                "cpu": req.resources.as_ref().map(|r| r.cpu.clone()).unwrap_or_else(|| "500m".to_string()),
                "memory": req.resources.as_ref().map(|r| r.memory.clone()).unwrap_or_else(|| "512Mi".to_string()),
            },
            "pathPrefix": path_prefix.clone(),
            "ttlSeconds": req.ttl_seconds,
        }
    });

    let api_client = reqwest::Client::new();
    let k8s_server = std::env::var("KUBERNETES_SERVICE_HOST")
        .unwrap_or_else(|_| "kubernetes.default.svc".to_string());
    let k8s_port = std::env::var("KUBERNETES_SERVICE_PORT").unwrap_or_else(|_| "443".to_string());
    let token = tokio::fs::read_to_string("/var/run/secrets/kubernetes.io/serviceaccount/token")
        .await
        .ok();

    if let Some(token) = token {
        let url = format!(
            "https://{}:{}/apis/basilica.ai/v1/namespaces/{}/userdeployments",
            k8s_server, k8s_port, namespace
        );

        let _ = api_client
            .post(&url)
            .header("Authorization", format!("Bearer {}", token))
            .json(&user_deployment_spec)
            .send()
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("Failed to create UserDeployment CR: {e}"),
            })?;
    }

    let envoy_manager = EnvoyConfigManager::new(
        k8s_client.clone(),
        state.config.deployment.envoy_namespace.clone(),
        state.config.deployment.envoy_configmap_name.clone(),
    );

    let service_name = format!("{}-service", req.instance_name);
    let target_service = format!("{}.{}:{}", service_name, namespace, req.port);

    envoy_manager
        .add_route(EnvoyRoute {
            prefix: path_prefix.clone(),
            cluster_name: format!("user_deployment_{}", req.instance_name),
            target_service,
            rewrite: "/".to_string(),
        })
        .await?;

    k8s_client
        .restart_deployment(
            &state.config.deployment.envoy_namespace,
            &state.config.deployment.envoy_deployment_name,
        )
        .await?;

    let record = db::create_deployment(
        &state.db,
        db::CreateDeploymentParams {
            user_id: &auth.user_id,
            instance_name: &req.instance_name,
            namespace: &namespace,
            cr_name: &cr_name,
            image: &req.image,
            replicas: req.replicas as i32,
            port: req.port as i32,
            path_prefix: &path_prefix,
            public_url: &public_url,
        },
    )
    .await?;

    apimetrics::record_request("POST /deployments", "201", start, true);

    Ok((
        StatusCode::CREATED,
        Json(DeploymentResponse::from_record(&record)),
    ))
}

pub async fn get_deployment(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path(instance_name): Path<String>,
) -> Result<impl IntoResponse> {
    let start = std::time::Instant::now();

    let deployment = db::get_deployment(&state.db, &auth.user_id, &instance_name)
        .await?
        .ok_or(ApiError::NotFound {
            message: "Deployment not found".to_string(),
        })?;

    apimetrics::record_request("GET /deployments/:name", "200", start, true);

    Ok((
        StatusCode::OK,
        Json(DeploymentResponse::from_record(&deployment)),
    ))
}

pub async fn delete_deployment(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path(instance_name): Path<String>,
) -> Result<impl IntoResponse> {
    let start = std::time::Instant::now();

    let deployment = db::get_deployment(&state.db, &auth.user_id, &instance_name)
        .await?
        .ok_or(ApiError::NotFound {
            message: "Deployment not found".to_string(),
        })?;

    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;

    let api_client = reqwest::Client::new();
    let k8s_server = std::env::var("KUBERNETES_SERVICE_HOST")
        .unwrap_or_else(|_| "kubernetes.default.svc".to_string());
    let k8s_port = std::env::var("KUBERNETES_SERVICE_PORT").unwrap_or_else(|_| "443".to_string());
    let token = tokio::fs::read_to_string("/var/run/secrets/kubernetes.io/serviceaccount/token")
        .await
        .ok();

    if let Some(token) = token {
        let url = format!(
            "https://{}:{}/apis/basilica.ai/v1/namespaces/{}/userdeployments/{}",
            k8s_server, k8s_port, deployment.namespace, deployment.cr_name
        );

        let _ = api_client
            .delete(&url)
            .header("Authorization", format!("Bearer {}", token))
            .send()
            .await;
    }

    let envoy_manager = EnvoyConfigManager::new(
        k8s_client.clone(),
        state.config.deployment.envoy_namespace.clone(),
        state.config.deployment.envoy_configmap_name.clone(),
    );

    let cluster_name = format!("user_deployment_{}", instance_name);
    envoy_manager
        .remove_route(&deployment.path_prefix, &cluster_name)
        .await?;

    k8s_client
        .restart_deployment(
            &state.config.deployment.envoy_namespace,
            &state.config.deployment.envoy_deployment_name,
        )
        .await?;

    db::mark_deployment_deleted(&state.db, deployment.id).await?;

    apimetrics::record_request("DELETE /deployments/:name", "200", start, true);

    #[derive(Serialize)]
    struct DeleteResponse {
        instance_name: String,
        state: String,
        message: String,
    }

    Ok((
        StatusCode::OK,
        Json(DeleteResponse {
            instance_name,
            state: "Terminating".to_string(),
            message: "Deployment deletion initiated".to_string(),
        }),
    ))
}

pub async fn list_deployments(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
) -> Result<impl IntoResponse> {
    let start = std::time::Instant::now();

    let deployments = db::list_user_deployments(&state.db, &auth.user_id).await?;

    apimetrics::record_request("GET /deployments", "200", start, true);

    #[derive(Serialize)]
    struct ListResponse {
        deployments: Vec<DeploymentResponse>,
        total: usize,
    }

    let deployment_responses: Vec<DeploymentResponse> = deployments
        .iter()
        .map(DeploymentResponse::from_record)
        .collect();

    let total = deployment_responses.len();

    Ok((
        StatusCode::OK,
        Json(ListResponse {
            deployments: deployment_responses,
            total,
        }),
    ))
}

impl DeploymentResponse {
    pub fn from_record(record: &db::DeploymentRecord) -> Self {
        Self {
            instance_name: record.instance_name.clone(),
            user_id: record.user_id.clone(),
            namespace: record.namespace.clone(),
            state: record.state.clone(),
            url: record.public_url.clone(),
            replicas: super::types::ReplicaStatus {
                desired: record.replicas as u32,
                ready: 0,
            },
            created_at: record.created_at.to_rfc3339(),
            updated_at: Some(record.updated_at.to_rfc3339()),
            pods: None,
        }
    }
}
