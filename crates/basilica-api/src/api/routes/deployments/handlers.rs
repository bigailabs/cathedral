use crate::api::middleware::AuthContext;
use crate::apimetrics;
use crate::db;
use crate::error::{ApiError, Result};
use crate::gateway::HTTPRoute;
use crate::server::AppState;
use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    Extension, Json,
};
use serde::Serialize;

use super::types::{
    sanitize_instance_name, sanitize_user_id, validate_image, validate_instance_name,
    validate_port, validate_replicas, CreateDeploymentRequest, DeploymentResponse,
};

fn user_namespace(user_id: &str) -> String {
    format!("u-{}", sanitize_user_id(user_id))
}

/// Pre-deployment validation for P1-1 (storage), P1-2 (quota), P1-4 (capacity), P1-6 (image pull)
async fn validate_deployment_prerequisites(
    k8s_client: std::sync::Arc<dyn crate::k8s_client::ApiK8sClient + Send + Sync>,
    namespace: &str,
    req: &CreateDeploymentRequest,
) -> Result<()> {
    // P1-1: Validate storage secret exists if storage is enabled
    if let Some(ref storage) = req.storage {
        if let Some(ref persistent) = storage.persistent {
            if persistent.enabled {
                if let Some(ref credentials_secret) = persistent.credentials_secret {
                    let exists = k8s_client
                        .secret_exists(namespace, credentials_secret)
                        .await?;
                    if !exists {
                        return Err(ApiError::InvalidRequest {
                            message: format!(
                                "Storage credentials secret '{}' not found in namespace '{}'. \
                                 Please ensure the secret is created before enabling storage.",
                                credentials_secret, namespace
                            ),
                        });
                    }
                }
            }
        }
    }

    // P1-2: Validate resource quota (if namespace has quota configured)
    if let Some(quota) = k8s_client.get_namespace_resource_quota(namespace).await? {
        validate_against_quota(&quota, req)?;
    }

    // P1-4: Validate cluster capacity
    let (cpu_request, memory_request) = if let Some(ref resources) = req.resources {
        (resources.cpu.clone(), resources.memory.clone())
    } else {
        ("500m".to_string(), "512Mi".to_string())
    };

    let gpu_count = req
        .resources
        .as_ref()
        .and_then(|r| r.gpus.as_ref())
        .map(|g| g.count * req.replicas);

    let capacity = k8s_client
        .check_cluster_capacity(&cpu_request, &memory_request, gpu_count)
        .await?;

    if !capacity.has_capacity {
        return Err(ApiError::InvalidRequest {
            message: format!(
                "Insufficient cluster capacity: {}",
                capacity.message.unwrap_or_else(|| "unknown".to_string())
            ),
        });
    }

    // P1-6: Validate image pull credentials for private registries
    if let Some(registry) = extract_registry_from_image(&req.image) {
        if is_private_registry(&registry) {
            let pull_secrets = k8s_client.get_image_pull_secrets(namespace).await?;
            if pull_secrets.is_empty() {
                tracing::warn!(
                    namespace = namespace,
                    image = %req.image,
                    registry = %registry,
                    "Private registry detected but no image pull secrets found"
                );
                return Err(ApiError::InvalidRequest {
                    message: format!(
                        "Image '{}' requires credentials from private registry '{}', \
                         but no image pull secrets found in namespace '{}'. \
                         Create a docker-registry secret with your credentials.",
                        req.image, registry, namespace
                    ),
                });
            }
        }
    }

    Ok(())
}

fn extract_registry_from_image(image: &str) -> Option<String> {
    let image = image.trim();
    // Simple images like "nginx:latest" or "nginx" have no registry
    if !image.contains('/') {
        return None;
    }
    // Images like "library/nginx" are still Docker Hub
    let first_part = image.split('/').next()?;
    // Registry must contain '.' or ':' (e.g., gcr.io, localhost:5000)
    // or be a known registry prefix
    if first_part.contains('.') || first_part.contains(':') {
        Some(first_part.to_string())
    } else {
        None // Docker Hub image like "myuser/myimage"
    }
}

fn is_private_registry(registry: &str) -> bool {
    // Public registries that don't require credentials
    let public_registries = [
        "docker.io",
        "registry.hub.docker.com",
        "ghcr.io",           // GitHub Container Registry (public images)
        "gcr.io",            // Google Container Registry (public images)
        "quay.io",           // Quay (public images)
        "nvcr.io",           // NVIDIA NGC (some public)
        "public.ecr.aws",    // AWS ECR Public
        "mcr.microsoft.com", // Microsoft Container Registry
    ];
    !public_registries.iter().any(|&r| registry.starts_with(r))
}

fn check_resource_quota(
    limit: Option<&String>,
    used: Option<&String>,
    requested_per_replica: i64,
    replicas: i64,
    resource_name: &str,
    parse_fn: fn(&str) -> i64,
    format_fn: fn(i64) -> String,
) -> Result<()> {
    let (Some(limit_str), Some(used_str)) = (limit, used) else {
        return Ok(());
    };

    let limit_val = parse_fn(limit_str);
    let used_val = parse_fn(used_str);
    let requested_total = requested_per_replica.saturating_mul(replicas);
    let available = limit_val.saturating_sub(used_val);

    if used_val.saturating_add(requested_total) > limit_val {
        return Err(ApiError::QuotaExceeded {
            message: format!(
                "{} quota exceeded: requesting {} x {} replicas = {}, but only {} available (limit: {}, used: {})",
                resource_name,
                format_fn(requested_per_replica),
                replicas,
                format_fn(requested_total),
                format_fn(available),
                format_fn(limit_val),
                format_fn(used_val)
            ),
        });
    }
    Ok(())
}

fn validate_against_quota(
    quota: &crate::k8s::ResourceQuotaDto,
    req: &CreateDeploymentRequest,
) -> Result<()> {
    use basilica_common::utils::{
        format_bytes, format_milli_cpu, parse_cpu_to_milli, parse_memory_to_bytes,
    };

    let Some(ref resources) = req.resources else {
        return Ok(());
    };

    let replicas = req.replicas as i64;
    let cpu_per_replica = parse_cpu_to_milli(&resources.cpu);
    let memory_per_replica = parse_memory_to_bytes(&resources.memory);

    // Check CPU limits quota
    check_resource_quota(
        quota.cpu_limit.as_ref(),
        quota.cpu_used.as_ref(),
        cpu_per_replica,
        replicas,
        "CPU limits",
        parse_cpu_to_milli,
        format_milli_cpu,
    )?;

    // Check CPU requests quota
    check_resource_quota(
        quota.requests_cpu_limit.as_ref(),
        quota.requests_cpu_used.as_ref(),
        cpu_per_replica,
        replicas,
        "CPU requests",
        parse_cpu_to_milli,
        format_milli_cpu,
    )?;

    // Check memory limits quota
    check_resource_quota(
        quota.memory_limit.as_ref(),
        quota.memory_used.as_ref(),
        memory_per_replica,
        replicas,
        "Memory limits",
        parse_memory_to_bytes,
        format_bytes,
    )?;

    // Check memory requests quota
    check_resource_quota(
        quota.requests_memory_limit.as_ref(),
        quota.requests_memory_used.as_ref(),
        memory_per_replica,
        replicas,
        "Memory requests",
        parse_memory_to_bytes,
        format_bytes,
    )?;

    // Check GPU quota
    if let (Some(gpu_limit), Some(gpu_used)) = (quota.gpu_limit, quota.gpu_used) {
        let gpu_per_replica = resources.gpus.as_ref().map_or(0, |g| g.count as i64);
        let requested_total = gpu_per_replica.saturating_mul(replicas);
        let available = gpu_limit.saturating_sub(gpu_used);

        if gpu_used.saturating_add(requested_total) > gpu_limit {
            return Err(ApiError::QuotaExceeded {
                message: format!(
                    "GPU quota exceeded: requesting {} x {} replicas = {}, but only {} available (limit: {}, used: {})",
                    gpu_per_replica, replicas, requested_total, available, gpu_limit, gpu_used
                ),
            });
        }
    }

    // Check pods quota
    if let (Some(pods_limit), Some(pods_used)) = (quota.pods_limit, quota.pods_used) {
        let available = pods_limit.saturating_sub(pods_used);
        if pods_used.saturating_add(replicas) > pods_limit {
            return Err(ApiError::QuotaExceeded {
                message: format!(
                    "Pods quota exceeded: requesting {} replicas, but only {} available (limit: {}, used: {})",
                    replicas, available, pods_limit, pods_used
                ),
            });
        }
    }

    Ok(())
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
        "Creating HTTPRoute for Gateway API routing"
    );

    let service_name = format!("s-{}", instance_name);
    let httproute = if req.public {
        if let Some(hostname) = config.dns.build_fqdn(instance_name) {
            HTTPRoute::new_host_based(
                instance_name.to_string(),
                namespace.to_string(),
                service_name,
                req.port as i32,
                hostname,
            )
        } else {
            tracing::warn!(
                namespace = namespace,
                instance_name = instance_name,
                "Public deployment requested but DNS not configured, using path-based routing"
            );
            HTTPRoute::new_path_based(
                instance_name.to_string(),
                namespace.to_string(),
                service_name,
                req.port as i32,
            )
        }
    } else {
        HTTPRoute::new_path_based(
            instance_name.to_string(),
            namespace.to_string(),
            service_name,
            req.port as i32,
        )
    };

    httproute.create(&k8s_client.kube_client()).await?;

    tracing::debug!(
        namespace = namespace,
        cr_name = cr_name,
        "HTTPRoute created, Envoy Gateway will auto-update via xDS"
    );

    Ok(())
}

pub async fn create_deployment(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Json(req): Json<CreateDeploymentRequest>,
) -> Result<impl IntoResponse> {
    let start = std::time::Instant::now();

    // Sanitize the user-provided instance name (or generate one if not provided)
    let user_instance_name = sanitize_instance_name(req.instance_name.clone());

    // Validate the sanitized name
    validate_instance_name(&user_instance_name)?;

    // Get or create a stable instance_id (UUID) for this (user_id, instance_name) pair
    // This enables idempotent deployments - same name = same storage prefix
    let instance_name =
        db::get_or_create_instance_id(&state.db, &auth.user_id, &user_instance_name).await?;

    tracing::info!(
        user_id = %auth.user_id,
        user_instance_name = %user_instance_name,
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

    // Pre-deployment validation (P1-1, P1-2, P1-4)
    validate_deployment_prerequisites(k8s_client.clone(), &namespace, &req).await?;

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

    // Check for soft-deleted deployment with the same instance_name.
    // If found, reactivate it instead of creating a new record.
    // This preserves the storage path (instance_id) for data persistence.
    let deleted =
        db::get_deployment_including_deleted(&state.db, &auth.user_id, &instance_name).await?;

    if let Some(deleted_record) = deleted {
        if deleted_record.deleted_at.is_some() {
            tracing::info!(
                user_id = %auth.user_id,
                instance_name = %instance_name,
                deployment_id = deleted_record.id,
                "Found soft-deleted deployment, reactivating for storage persistence"
            );

            let record = db::reactivate_deployment(
                &state.db,
                deleted_record.id,
                db::ReactivateDeploymentParams {
                    image: &req.image,
                    replicas: req.replicas as i32,
                    port: req.port as i32,
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
                        "K8s resources created for reactivated deployment"
                    );
                    db::update_deployment_state(&state.db, record.id, "Active", None).await?;

                    if req.public {
                        if let Some(dns_provider) = &state.dns_provider {
                            let alb_dns_name =
                                state.config.dns.alb_dns_name.as_ref().ok_or_else(|| {
                                    ApiError::Internal {
                                        message:
                                            "ALB DNS name not configured for public deployments"
                                                .to_string(),
                                    }
                                })?;

                            if let Err(e) = dns_provider
                                .create_record(&instance_name, alb_dns_name)
                                .await
                            {
                                tracing::error!(
                                    user_id = %auth.user_id,
                                    instance_name = %instance_name,
                                    error = %e,
                                    "Failed to create DNS record for reactivated deployment"
                                );
                            }
                        }
                    }

                    apimetrics::record_request("POST /deployments", "200", start, true);
                    return Ok((
                        StatusCode::OK,
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
                            phase: Some("pending".to_string()),
                            progress: None,
                        }),
                    ));
                }
                Err(e) => {
                    tracing::error!(
                        user_id = %auth.user_id,
                        instance_name = %instance_name,
                        deployment_id = record.id,
                        error = %e,
                        "Failed to create K8s resources for reactivated deployment"
                    );
                    let error_msg = format!("K8s resource creation failed: {}", e);
                    db::update_deployment_state(&state.db, record.id, "Failed", Some(&error_msg))
                        .await?;
                    return Err(e);
                }
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
                    phase: Some("pending".to_string()),
                    progress: None,
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

    // Delete UserDeployment CR first
    k8s_client
        .delete_user_deployment(&deployment.namespace, &deployment.cr_name)
        .await?;

    // Delete HTTPRoute
    let service_name = format!("s-{}", instance_name);
    let httproute = HTTPRoute {
        instance_name: instance_name.clone(),
        namespace: deployment.namespace.clone(),
        service_name: service_name.clone(),
        service_port: deployment.port,
        path_prefix: None,
        hostname: None,
    };
    httproute.delete(&k8s_client.kube_client()).await?;

    // Delete K8s resources created by the operator
    // These don't have owner references, so we need to clean them up manually
    // Operator naming: Deployment={instance}-deployment, Service=s-{instance}, NetPol={instance}-netpol
    let deployment_name = deployment.cr_name.clone(); // Already {instance}-deployment
    let netpol_name = format!("{}-netpol", instance_name);

    // Delete in order: NetworkPolicy, Service, Deployment (which cascades to Pods)
    k8s_client
        .delete_network_policy(&deployment.namespace, &netpol_name)
        .await
        .ok(); // Don't fail if already deleted

    k8s_client
        .delete_service(&deployment.namespace, &service_name)
        .await
        .ok(); // Don't fail if already deleted

    k8s_client
        .delete_deployment(&deployment.namespace, &deployment_name)
        .await
        .ok(); // Don't fail if already deleted

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
            phase: None,
            progress: None,
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
            phase: None,
            progress: None,
        }
    }

    pub fn from_record_with_phase(
        record: &db::DeploymentRecord,
        desired: u32,
        ready: u32,
        phase: Option<String>,
        progress: Option<super::types::ProgressResponse>,
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
            phase,
            progress,
        }
    }
}

pub async fn stream_deployment_logs(
    State(state): State<AppState>,
    Extension(auth): Extension<crate::api::middleware::AuthContext>,
    axum::extract::Path(instance_name): axum::extract::Path<String>,
    axum::extract::Query(query): axum::extract::Query<basilica_sdk::types::LogStreamQuery>,
) -> Result<
    axum::response::sse::Sse<
        impl futures::Stream<Item = std::result::Result<axum::response::sse::Event, std::io::Error>>,
    >,
> {
    use axum::response::sse::{Event, Sse};
    use futures::Stream;
    use std::pin::Pin;
    use std::time::Instant;

    let start = Instant::now();

    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("deployments.logs", "GET", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };

    let ns = user_namespace(&auth.user_id);
    let cr_name = generate_cr_name(&instance_name);
    let follow = query.follow.unwrap_or(false);
    let tail = query.tail;
    let since_seconds = query.since_seconds;

    let stream: Pin<Box<dyn Stream<Item = std::result::Result<Event, std::io::Error>> + Send>> =
        if !follow {
            let logs = client
                .get_user_deployment_logs(&ns, &cr_name, tail, since_seconds)
                .await?;
            let lines: Vec<String> = logs.lines().map(|s| s.to_string()).collect();
            Box::pin(async_stream::stream! {
                for line in &lines {
                    let data = serde_json::json!({
                        "timestamp": chrono::Utc::now(),
                        "stream": "stdout",
                        "message": line,
                    });
                    yield Ok(Event::default().data(data.to_string()));
                }
            })
        } else {
            let client_clone = client.clone();
            Box::pin(async_stream::stream! {
                use tokio::time::{sleep, Duration, Instant as TokioInstant};
                let mut last_marker: Option<String> = None;
                let start_at = TokioInstant::now();
                let max_duration = Duration::from_secs(300);
                let mut last_heartbeat = TokioInstant::now();
                loop {
                    if start_at.elapsed() >= max_duration {
                        break;
                    }
                    if let Ok(body) = client_clone.get_user_deployment_logs(&ns, &cr_name, tail.or(Some(100)), since_seconds).await {
                        let lines: Vec<String> = body.lines().map(|s| s.to_string()).collect();
                        if !lines.is_empty() {
                            let start_idx = if let Some(ref marker) = last_marker {
                                lines.iter().rposition(|l| l == marker).map(|idx| idx + 1).unwrap_or(0)
                            } else { 0 };
                            for line in &lines[start_idx..] {
                                let data = serde_json::json!({
                                    "timestamp": chrono::Utc::now(),
                                    "stream": "stdout",
                                    "message": line,
                                });
                                yield Ok(Event::default().data(data.to_string()));
                            }
                            last_marker = lines.last().cloned();
                        }
                    }
                    if last_heartbeat.elapsed() >= Duration::from_secs(15) {
                        let hb = serde_json::json!({"heartbeat": true, "timestamp": chrono::Utc::now()});
                        yield Ok(Event::default().data(hb.to_string()));
                        last_heartbeat = TokioInstant::now();
                    }
                    sleep(Duration::from_millis(1000)).await;
                }
            })
        };

    apimetrics::record_request("deployments.logs", "GET", start, true);
    Ok(Sse::new(stream))
}

#[derive(Debug, Serialize)]
pub struct DeploymentEventsResponse {
    pub events: Vec<crate::k8s::DeploymentEventDto>,
}

#[derive(Debug, serde::Deserialize)]
pub struct EventsQuery {
    #[serde(default)]
    pub limit: Option<u32>,
}

pub async fn get_deployment_events(
    Extension(auth): Extension<AuthContext>,
    State(state): State<AppState>,
    Path(instance_name): Path<String>,
    axum::extract::Query(query): axum::extract::Query<EventsQuery>,
) -> Result<Json<DeploymentEventsResponse>> {
    let start = std::time::Instant::now();

    let client = match state.k8s.as_ref() {
        Some(c) => c.clone(),
        None => {
            apimetrics::record_request("deployments.events", "GET", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };

    let ns = user_namespace(&auth.user_id);
    let limit = query.limit.unwrap_or(10);

    let events = client
        .get_user_deployment_events(&ns, &instance_name, Some(limit))
        .await?;

    apimetrics::record_request("deployments.events", "GET", start, true);
    Ok(Json(DeploymentEventsResponse { events }))
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
