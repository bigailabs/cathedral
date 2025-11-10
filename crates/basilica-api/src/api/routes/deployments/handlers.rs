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
    resolve_instance_name, sanitize_user_id, validate_image, validate_port, validate_replicas,
    CreateDeploymentRequest, DeploymentResponse,
};

fn user_namespace(user_id: &str) -> String {
    format!("u-{}", sanitize_user_id(user_id))
}

fn generate_cr_name(instance_name: &str) -> String {
    format!("{}-deployment", instance_name)
}

#[allow(clippy::too_many_arguments)]
async fn create_k8s_resources(
    k8s_client: std::sync::Arc<dyn crate::k8s_client::ApiK8sClient + Send + Sync>,
    config: &crate::config::Config,
    namespace: &str,
    cr_name: &str,
    user_id: &str,
    instance_name: &str,
    req: &super::types::CreateDeploymentRequest,
    path_prefix: &str,
) -> Result<()> {
    tracing::info!(
        namespace = namespace,
        cr_name = cr_name,
        "Creating user namespace"
    );
    k8s_client.create_namespace(namespace).await?;

    tracing::info!(
        namespace = namespace,
        cr_name = cr_name,
        "Creating UserDeployment CR"
    );
    k8s_client
        .create_user_deployment(namespace, cr_name, user_id, instance_name, req, path_prefix)
        .await?;

    tracing::debug!(
        namespace = namespace,
        cr_name = cr_name,
        "Configuring Envoy routing"
    );
    let envoy_manager = EnvoyConfigManager::new(
        k8s_client.clone(),
        config.deployment.envoy_namespace.clone(),
        config.deployment.envoy_configmap_name.clone(),
        config.deployment.max_configmap_size_bytes,
    );

    let service_name = format!("s-{}", instance_name);
    let target_service = format!("{}.{}:{}", service_name, namespace, req.port);

    let envoy_host = if req.public {
        config.dns.build_fqdn(instance_name)
    } else {
        None
    };

    envoy_manager
        .add_route(EnvoyRoute {
            prefix: path_prefix.to_string(),
            cluster_name: format!("user_deployment_{}", instance_name),
            target_service,
            rewrite: "/".to_string(),
            host: envoy_host,
        })
        .await?;

    tracing::debug!(
        namespace = namespace,
        cr_name = cr_name,
        "Envoy will auto-reload configuration via watched_directory"
    );

    Ok(())
}

pub async fn create_deployment(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Json(req): Json<CreateDeploymentRequest>,
) -> Result<impl IntoResponse> {
    let start = std::time::Instant::now();

    let instance_name = resolve_instance_name(req.instance_name.clone());

    tracing::info!(
        user_id = %auth.user_id,
        instance_name = %instance_name,
        image = %req.image,
        replicas = req.replicas,
        "Received deployment creation request"
    );

    validate_image(&req.image)?;
    validate_port(req.port)?;
    validate_replicas(req.replicas, state.config.deployment.max_replicas)?;

    if let Some(ref resources) = req.resources {
        super::types::validate_resources(resources)?;
    }

    tracing::debug!(
        user_id = %auth.user_id,
        instance_name = %instance_name,
        "Checking K8s client availability"
    );
    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;

    let namespace = user_namespace(&auth.user_id);
    let cr_name = generate_cr_name(&instance_name);
    let path_prefix = format!("/deployments/{}", instance_name);

    let public_url = if req.public {
        state.config.dns.build_public_url(&instance_name).unwrap_or_else(|| {
            tracing::warn!(
                user_id = %auth.user_id,
                instance_name = %instance_name,
                "Public deployment requested but DNS not configured, falling back to path-based URL"
            );
            state.config.deployment.generate_public_url(&path_prefix)
        })
    } else {
        state.config.deployment.generate_public_url(&path_prefix)
    };

    let existing = db::get_deployment(&state.db, &auth.user_id, &instance_name).await?;
    if let Some(existing) = existing {
        tracing::info!(
            user_id = %auth.user_id,
            instance_name = %instance_name,
            state = %existing.state,
            deployment_id = existing.id,
            "Found existing deployment, attempting to complete K8s resource creation"
        );

        if existing.state == "Active" {
            tracing::info!(
                user_id = %auth.user_id,
                instance_name = %instance_name,
                "Deployment already active, querying K8s for current status"
            );

            let (desired, ready) = k8s_client
                .get_user_deployment_status(&existing.namespace, &existing.cr_name)
                .await
                .unwrap_or((existing.replicas as u32, 0));

            if desired == 0 {
                tracing::warn!(
                    user_id = %auth.user_id,
                    instance_name = %instance_name,
                    "Active deployment has 0 desired replicas, checking if K8s resources exist"
                );

                let exists = k8s_client
                    .user_deployment_exists(&existing.namespace, &existing.cr_name)
                    .await
                    .unwrap_or(true);

                if !exists {
                    tracing::warn!(
                        user_id = %auth.user_id,
                        instance_name = %instance_name,
                        deployment_id = existing.id,
                        "UserDeployment CR missing for Active deployment, attempting self-healing"
                    );

                    match create_k8s_resources(
                        k8s_client.clone(),
                        &state.config,
                        &namespace,
                        &cr_name,
                        &auth.user_id,
                        &instance_name,
                        &req,
                        &path_prefix,
                    )
                    .await
                    {
                        Ok(_) => {
                            tracing::info!(
                                user_id = %auth.user_id,
                                instance_name = %instance_name,
                                deployment_id = existing.id,
                                "Successfully recreated missing K8s resources"
                            );

                            let (desired_new, ready_new) = k8s_client
                                .get_user_deployment_status(&existing.namespace, &existing.cr_name)
                                .await
                                .unwrap_or((existing.replicas as u32, 0));

                            apimetrics::record_request("POST /deployments", "200", start, true);
                            return Ok((
                                StatusCode::OK,
                                Json(DeploymentResponse::from_record_with_status(
                                    &existing,
                                    desired_new,
                                    ready_new,
                                )),
                            ));
                        }
                        Err(e) => {
                            tracing::error!(
                                user_id = %auth.user_id,
                                instance_name = %instance_name,
                                deployment_id = existing.id,
                                error = %e,
                                "Failed to recreate missing K8s resources"
                            );
                            let error_msg = format!("K8s resource recreation failed: {}", e);
                            db::update_deployment_state(
                                &state.db,
                                existing.id,
                                "Failed",
                                Some(&error_msg),
                            )
                            .await?;
                            return Err(e);
                        }
                    }
                }
            }

            tracing::info!(
                user_id = %auth.user_id,
                instance_name = %instance_name,
                desired = desired,
                ready = ready,
                "Returning active deployment with status"
            );

            apimetrics::record_request("POST /deployments", "200", start, true);
            return Ok((
                StatusCode::OK,
                Json(DeploymentResponse::from_record_with_status(
                    &existing, desired, ready,
                )),
            ));
        }

        match create_k8s_resources(
            k8s_client.clone(),
            &state.config,
            &namespace,
            &cr_name,
            &auth.user_id,
            &instance_name,
            &req,
            &path_prefix,
        )
        .await
        {
            Ok(_) => {
                tracing::info!(
                    user_id = %auth.user_id,
                    instance_name = %instance_name,
                    deployment_id = existing.id,
                    "K8s resources created, updating deployment to Active"
                );
                db::update_deployment_state(&state.db, existing.id, "Active", None).await?;

                let (desired, ready) = k8s_client
                    .get_user_deployment_status(&existing.namespace, &existing.cr_name)
                    .await
                    .unwrap_or((existing.replicas as u32, 0));

                apimetrics::record_request("POST /deployments", "200", start, true);
                return Ok((
                    StatusCode::OK,
                    Json(DeploymentResponse::from_record_with_status(
                        &existing, desired, ready,
                    )),
                ));
            }
            Err(e) => {
                tracing::error!(
                    user_id = %auth.user_id,
                    instance_name = %instance_name,
                    deployment_id = existing.id,
                    error = %e,
                    "Failed to create K8s resources, updating deployment to Failed"
                );
                let error_msg = format!("K8s resource creation failed: {}", e);
                db::update_deployment_state(&state.db, existing.id, "Failed", Some(&error_msg))
                    .await?;
                return Err(e);
            }
        }
    }

    tracing::info!(
        user_id = %auth.user_id,
        instance_name = %instance_name,
        namespace = %namespace,
        "Creating database record for new deployment"
    );
    let record = db::create_deployment(
        &state.db,
        db::CreateDeploymentParams {
            user_id: &auth.user_id,
            instance_name: &instance_name,
            namespace: &namespace,
            cr_name: &cr_name,
            image: &req.image,
            replicas: req.replicas as i32,
            port: req.port as i32,
            path_prefix: &path_prefix,
            public_url: &public_url,
            public: req.public,
        },
    )
    .await?;

    match create_k8s_resources(
        k8s_client.clone(),
        &state.config,
        &namespace,
        &cr_name,
        &auth.user_id,
        &instance_name,
        &req,
        &path_prefix,
    )
    .await
    {
        Ok(_) => {
            tracing::info!(
                user_id = %auth.user_id,
                instance_name = %instance_name,
                deployment_id = record.id,
                "K8s resources created, updating deployment to Active"
            );
            db::update_deployment_state(&state.db, record.id, "Active", None).await?;

            if req.public {
                if let Some(dns_provider) = &state.dns_provider {
                    let alb_dns_name = state.config.dns.alb_dns_name.as_ref().ok_or_else(|| {
                        ApiError::Internal {
                            message: "ALB DNS name not configured for public deployments"
                                .to_string(),
                        }
                    })?;

                    match dns_provider
                        .create_record(&instance_name, alb_dns_name)
                        .await
                    {
                        Ok(fqdn) => {
                            tracing::info!(
                                user_id = %auth.user_id,
                                instance_name = %instance_name,
                                fqdn = %fqdn,
                                "Successfully created DNS record for public deployment"
                            );
                        }
                        Err(e) => {
                            tracing::error!(
                                user_id = %auth.user_id,
                                instance_name = %instance_name,
                                error = %e,
                                "Failed to create DNS record, deployment will remain private"
                            );
                        }
                    }
                } else {
                    tracing::warn!(
                        user_id = %auth.user_id,
                        instance_name = %instance_name,
                        "Public deployment requested but DNS provider not available"
                    );
                }
            }

            apimetrics::record_request("POST /deployments", "201", start, true);
            Ok((
                StatusCode::CREATED,
                Json(DeploymentResponse {
                    instance_name: record.instance_name,
                    user_id: record.user_id,
                    namespace: record.namespace,
                    state: "Active".to_string(),
                    url: record.public_url,
                    replicas: super::types::ReplicaStatus {
                        desired: record.replicas as u32,
                        ready: 0,
                    },
                    created_at: record.created_at.to_rfc3339(),
                    updated_at: Some(record.updated_at.to_rfc3339()),
                    pods: None,
                }),
            ))
        }
        Err(e) => {
            tracing::error!(
                user_id = %auth.user_id,
                instance_name = %instance_name,
                deployment_id = record.id,
                error = %e,
                "Failed to create K8s resources, updating deployment to Failed"
            );
            let error_msg = format!("K8s resource creation failed: {}", e);
            db::update_deployment_state(&state.db, record.id, "Failed", Some(&error_msg)).await?;
            Err(e)
        }
    }
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

    let (desired, ready) = if let Some(k8s_client) = state.k8s.as_ref() {
        tracing::debug!(
            user_id = %auth.user_id,
            instance_name = %instance_name,
            namespace = %deployment.namespace,
            cr_name = %deployment.cr_name,
            "Querying K8s for UserDeployment status"
        );
        match k8s_client
            .get_user_deployment_status(&deployment.namespace, &deployment.cr_name)
            .await
        {
            Ok((d, r)) => {
                tracing::info!(
                    user_id = %auth.user_id,
                    instance_name = %instance_name,
                    desired = d,
                    ready = r,
                    "Retrieved replica status from K8s"
                );
                (d, r)
            }
            Err(e) => {
                tracing::warn!(
                    user_id = %auth.user_id,
                    instance_name = %instance_name,
                    error = %e,
                    "Failed to get K8s status, using database values"
                );
                (deployment.replicas as u32, 0)
            }
        }
    } else {
        tracing::debug!(
            user_id = %auth.user_id,
            instance_name = %instance_name,
            "No K8s client available, using database values"
        );
        (deployment.replicas as u32, 0)
    };

    apimetrics::record_request("GET /deployments/:name", "200", start, true);

    Ok((
        StatusCode::OK,
        Json(DeploymentResponse::from_record_with_status(
            &deployment,
            desired,
            ready,
        )),
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
        state.config.deployment.max_configmap_size_bytes,
    );

    let cluster_name = format!("user_deployment_{}", instance_name);
    envoy_manager
        .remove_route(&deployment.path_prefix, &cluster_name)
        .await?;

    if deployment.public {
        if let Some(dns_provider) = &state.dns_provider {
            match dns_provider.delete_record(&instance_name).await {
                Ok(_) => {
                    tracing::info!(
                        user_id = %auth.user_id,
                        instance_name = %instance_name,
                        "Successfully deleted DNS record for public deployment"
                    );
                }
                Err(e) => {
                    tracing::error!(
                        user_id = %auth.user_id,
                        instance_name = %instance_name,
                        error = %e,
                        "Failed to delete DNS record"
                    );
                }
            }
        }
    }

    db::mark_deployment_deleted(&state.db, deployment.id).await?;

    apimetrics::record_request("DELETE /deployments/:name", "200", start, true);

    #[derive(Serialize)]
    #[serde(rename_all = "camelCase")]
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

    let mut deployment_responses: Vec<DeploymentResponse> = Vec::new();

    for record in &deployments {
        let (desired, ready) = if let Some(k8s_client) = state.k8s.as_ref() {
            k8s_client
                .get_user_deployment_status(&record.namespace, &record.cr_name)
                .await
                .unwrap_or((record.replicas as u32, 0))
        } else {
            (record.replicas as u32, 0)
        };

        deployment_responses.push(DeploymentResponse::from_record_with_status(
            record, desired, ready,
        ));
    }

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

    pub fn from_record_with_status(
        record: &db::DeploymentRecord,
        desired: u32,
        ready: u32,
    ) -> Self {
        Self {
            instance_name: record.instance_name.clone(),
            user_id: record.user_id.clone(),
            namespace: record.namespace.clone(),
            state: record.state.clone(),
            url: record.public_url.clone(),
            replicas: super::types::ReplicaStatus { desired, ready },
            created_at: record.created_at.to_rfc3339(),
            updated_at: Some(record.updated_at.to_rfc3339()),
            pods: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_user_namespace_basic() {
        assert_eq!(user_namespace("user123"), "u-user123");
        assert_eq!(user_namespace("test-user"), "u-test-user");
    }

    #[test]
    fn test_user_namespace_auth0_formats() {
        assert_eq!(user_namespace("github|434149"), "u-github-434149");
        assert_eq!(
            user_namespace("google-oauth2|123456789"),
            "u-google-oauth2-123456789"
        );
        assert_eq!(user_namespace("auth0|user123"), "u-auth0-user123");
    }

    #[test]
    fn test_user_namespace_rfc1123_compliant() {
        let namespace = user_namespace("github|434149");

        assert!(
            namespace.len() <= 63,
            "Namespace must be <= 63 chars (RFC 1123)"
        );

        assert!(
            namespace
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-'),
            "Namespace must only contain lowercase, digits, and hyphens"
        );

        assert!(
            !namespace.starts_with('-') && !namespace.ends_with('-'),
            "Namespace must start and end with alphanumeric"
        );

        assert!(
            namespace.starts_with("u-"),
            "Namespace must start with u- prefix"
        );
    }

    #[test]
    fn test_user_namespace_special_characters() {
        assert_eq!(user_namespace("user@example.com"), "u-user-example-com");
        assert_eq!(user_namespace("user/with/slashes"), "u-user-with-slashes");
        assert_eq!(user_namespace("user.with.dots"), "u-user-with-dots");
    }

    #[test]
    fn test_generate_cr_name() {
        assert_eq!(generate_cr_name("my-app"), "my-app-deployment");
        assert_eq!(generate_cr_name("test"), "test-deployment");
    }
}
