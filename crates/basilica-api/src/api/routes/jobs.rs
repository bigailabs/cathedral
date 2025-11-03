use axum::{extract::State, Json};
use serde::{Deserialize, Serialize};

use crate::apimetrics;
use crate::{
    error::{ApiError, Result},
    k8s_client::{JobSpecDto, JobStatusDto, Resources},
    server::AppState,
};
use std::time::Instant;

#[derive(Debug, Clone, Deserialize)]
pub struct CreateJobRequest {
    pub image: String,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: Vec<(String, String)>,
    pub resources: Resources,
    #[serde(default)]
    pub ttl_seconds: u32,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub namespace: Option<String>,
    #[serde(default)]
    pub ports: Vec<crate::k8s_client::PortSpec>,
    #[serde(default)]
    pub storage: Option<crate::k8s_client::StorageConfig>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreateJobResponse {
    pub job_id: String,
}

pub async fn create_job(
    State(state): State<AppState>,
    Json(req): Json<CreateJobRequest>,
) -> Result<Json<CreateJobResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("jobs.create", "POST", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let name = req
        .name
        .clone()
        .unwrap_or_else(|| format!("job-{}", rand::random::<u32>()));
    let ns = req.namespace.clone().unwrap_or_else(|| "default".into());
    let spec = JobSpecDto {
        image: req.image,
        command: req.command,
        args: req.args,
        env: req.env,
        resources: req.resources,
        ttl_seconds: req.ttl_seconds,
        ports: req.ports,
        storage: req.storage,
    };
    let id = client.create_job(&ns, &name, spec).await?;
    apimetrics::record_job_created(&ns);
    apimetrics::record_request("jobs.create", "POST", start, true);
    Ok(Json(CreateJobResponse { job_id: id }))
}

#[derive(Debug, Clone, Serialize)]
pub struct JobStatusResponse {
    pub job_id: String,
    pub status: JobStatusDto,
}

pub async fn get_job_status(
    State(state): State<AppState>,
    axum::extract::Path(job_id): axum::extract::Path<String>,
) -> Result<Json<JobStatusResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("jobs.status", "GET", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    // TODO: infer from authenticated user's tenant namespace
    let ns = std::env::var("TENANT_NAMESPACE").unwrap_or_else(|_| "u-test".into());
    let st = client.get_job_status(&ns, &job_id).await?;
    apimetrics::record_request("jobs.status", "GET", start, true);
    Ok(Json(JobStatusResponse { job_id, status: st }))
}

#[derive(Debug, Clone, Serialize)]
pub struct DeleteJobResponse {
    pub job_id: String,
}

pub async fn delete_job(
    State(state): State<AppState>,
    axum::extract::Path(job_id): axum::extract::Path<String>,
) -> Result<Json<DeleteJobResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("jobs.delete", "DELETE", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    // TODO: infer from authenticated user's tenant namespace
    let ns = std::env::var("TENANT_NAMESPACE").unwrap_or_else(|_| "u-test".into());
    client.delete_job(&ns, &job_id).await?;
    apimetrics::record_request("jobs.delete", "DELETE", start, true);
    Ok(Json(DeleteJobResponse { job_id }))
}

#[derive(Debug, Clone, Serialize)]
pub struct JobLogsResponse {
    pub job_id: String,
    pub logs: String,
}

pub async fn get_job_logs(
    State(state): State<AppState>,
    axum::extract::Path(job_id): axum::extract::Path<String>,
) -> Result<Json<JobLogsResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("jobs.logs", "GET", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    // TODO: infer from authenticated user's tenant namespace
    let ns = std::env::var("TENANT_NAMESPACE").unwrap_or_else(|_| "u-test".into());
    let logs = client.get_job_logs(&ns, &job_id).await?;
    apimetrics::record_request("jobs.logs", "GET", start, true);
    Ok(Json(JobLogsResponse { job_id, logs }))
}

#[derive(Debug, Clone, Deserialize)]
pub struct ReadFileRequest {
    pub file_path: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ReadFileResponse {
    pub content: String,
}

/// Read a file from a job's container via exec
pub async fn read_job_file(
    State(state): State<AppState>,
    axum::extract::Path(job_id): axum::extract::Path<String>,
    Json(req): Json<ReadFileRequest>,
) -> Result<Json<ReadFileResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("jobs.read_file", "POST", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };

    // TODO: infer from authenticated user's tenant namespace
    let ns = std::env::var("TENANT_NAMESPACE").unwrap_or_else(|_| "u-test".into());

    // Execute cat command to read file
    let result = client
        .exec_job(
            &ns,
            &job_id,
            vec!["cat".to_string(), req.file_path.clone()],
            None,
            false,
        )
        .await?;

    if result.exit_code != 0 {
        apimetrics::record_request("jobs.read_file", "POST", start, false);
        return Err(ApiError::NotFound {
            message: format!("File not found: {}", req.file_path),
        });
    }

    apimetrics::record_request("jobs.read_file", "POST", start, true);
    Ok(Json(ReadFileResponse {
        content: result.stdout,
    }))
}

#[derive(Debug, Clone, Serialize)]
pub struct SuspendJobResponse {
    pub job_id: String,
}

/// Suspend a job (pause execution)
pub async fn suspend_job(
    State(state): State<AppState>,
    axum::extract::Path(job_id): axum::extract::Path<String>,
) -> Result<Json<SuspendJobResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("jobs.suspend", "POST", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };

    // TODO: infer from authenticated user's tenant namespace
    let ns = std::env::var("TENANT_NAMESPACE").unwrap_or_else(|_| "u-test".into());

    client.suspend_job(&ns, &job_id).await?;
    apimetrics::record_request("jobs.suspend", "POST", start, true);
    Ok(Json(SuspendJobResponse { job_id }))
}

#[derive(Debug, Clone, Serialize)]
pub struct ResumeJobResponse {
    pub job_id: String,
}

/// Resume a suspended job
pub async fn resume_job(
    State(state): State<AppState>,
    axum::extract::Path(job_id): axum::extract::Path<String>,
) -> Result<Json<ResumeJobResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("jobs.resume", "POST", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };

    // TODO: infer from authenticated user's tenant namespace
    let ns = std::env::var("TENANT_NAMESPACE").unwrap_or_else(|_| "u-test".into());

    client.resume_job(&ns, &job_id).await?;
    apimetrics::record_request("jobs.resume", "POST", start, true);
    Ok(Json(ResumeJobResponse { job_id }))
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::sync::Arc;

    async fn build_state() -> AppState {
        let client = crate::k8s_client::MockK8sClient::default();
        AppState {
            config: std::sync::Arc::new(crate::config::Config::default()),
            validator_client: std::sync::Arc::new(
                basilica_validator::ValidatorClient::new(
                    "http://localhost",
                    std::time::Duration::from_secs(1),
                )
                .unwrap(),
            ),
            validator_endpoint: "http://localhost".into(),
            validator_uid: 0,
            validator_hotkey: "".into(),
            http_client: reqwest::Client::builder().build().unwrap(),
            db: sqlx::PgPool::connect_lazy("postgres://user:pass@localhost/db")
                .expect("lazy PG pool dsn should be valid"),
            k8s: Some(Arc::new(client)),
            payments_client: None,
            billing_client: None,
            metrics: None,
        }
    }

    #[tokio::test]
    async fn create_get_delete_job_flow() {
        let state = build_state().await;
        std::env::set_var("TENANT_NAMESPACE", "default");

        let req_body = serde_json::json!({
            "image": "img",
            "resources": {"cpu": "1", "memory": "512Mi", "gpus": {"count": 0, "model": []}},
            "ttl_seconds": 0,
            "name": "job-test",
            "namespace": "default"
        });
        let res = super::create_job(
            State(state.clone()),
            Json(serde_json::from_value::<CreateJobRequest>(req_body).unwrap()),
        )
        .await
        .unwrap();
        let body = res.0;
        assert_eq!(body.job_id, "job-test");

        let res2 = super::get_job_status(
            State(state.clone()),
            axum::extract::Path("job-test".to_string()),
        )
        .await
        .unwrap();
        assert_eq!(res2.0.status.phase, "Pending");

        let res3 = super::delete_job(
            State(state.clone()),
            axum::extract::Path("job-test".to_string()),
        )
        .await
        .unwrap();
        assert_eq!(res3.0.job_id, "job-test");
    }

    #[tokio::test]
    async fn test_read_job_file_success() {
        let state = build_state().await;

        // Create a job first
        let req_body = serde_json::json!({
            "image": "img",
            "resources": {"cpu": "1", "memory": "512Mi", "gpus": {"count": 0, "model": []}},
            "ttl_seconds": 0,
            "name": "job-with-file",
            "namespace": "default"
        });
        let _res = super::create_job(
            State(state.clone()),
            Json(serde_json::from_value::<CreateJobRequest>(req_body).unwrap()),
        )
        .await
        .unwrap();

        // Read the metadata file
        let read_req = ReadFileRequest {
            file_path: "/app/basilica-env.json".to_string(),
        };

        let result = super::read_job_file(
            State(state.clone()),
            axum::extract::Path("job-with-file".to_string()),
            Json(read_req),
        )
        .await
        .unwrap();

        // Verify we got valid JSON
        let content = result.0.content;
        assert!(content.contains("test:latest"));
        assert!(content.contains("protocol_version"));
        assert!(content.contains("environments"));

        // Parse as JSON to verify structure
        let parsed: serde_json::Value = serde_json::from_str(&content).unwrap();
        assert_eq!(parsed["protocol_version"], "1.0");
        assert_eq!(parsed["image"], "test:latest");
        assert!(parsed["environments"].is_array());
    }

    #[tokio::test]
    async fn test_read_job_file_not_found() {
        let state = build_state().await;

        // Create a job first
        let req_body = serde_json::json!({
            "image": "img",
            "resources": {"cpu": "1", "memory": "512Mi", "gpus": {"count": 0, "model": []}},
            "ttl_seconds": 0,
            "name": "job-no-file",
            "namespace": "default"
        });
        let _res = super::create_job(
            State(state.clone()),
            Json(serde_json::from_value::<CreateJobRequest>(req_body).unwrap()),
        )
        .await
        .unwrap();

        // Try to read a non-existent file
        let read_req = ReadFileRequest {
            file_path: "/nonexistent/file.txt".to_string(),
        };

        let result = super::read_job_file(
            State(state.clone()),
            axum::extract::Path("job-no-file".to_string()),
            Json(read_req),
        )
        .await;

        // Should return NotFound error
        assert!(result.is_err());
        match result.unwrap_err() {
            ApiError::NotFound { message } => {
                assert!(message.contains("/nonexistent/file.txt"));
            }
            _ => panic!("Expected NotFound error"),
        }
    }

    #[tokio::test]
    async fn test_suspend_resume_job() {
        let state = build_state().await;

        // Set TENANT_NAMESPACE to match where we create the job
        std::env::set_var("TENANT_NAMESPACE", "default");

        // Create a job
        let req_body = serde_json::json!({
            "image": "img",
            "resources": {"cpu": "1", "memory": "512Mi", "gpus": {"count": 0, "model": []}},
            "ttl_seconds": 0,
            "name": "job-suspendable",
            "namespace": "default"
        });
        let _res = super::create_job(
            State(state.clone()),
            Json(serde_json::from_value::<CreateJobRequest>(req_body).unwrap()),
        )
        .await
        .unwrap();

        // Verify initial status is Pending
        let status = super::get_job_status(
            State(state.clone()),
            axum::extract::Path("job-suspendable".to_string()),
        )
        .await
        .unwrap();
        assert_eq!(status.0.status.phase, "Pending");

        // Suspend the job
        let suspend_res = super::suspend_job(
            State(state.clone()),
            axum::extract::Path("job-suspendable".to_string()),
        )
        .await
        .unwrap();
        assert_eq!(suspend_res.0.job_id, "job-suspendable");

        // Verify status is now Suspended
        let status = super::get_job_status(
            State(state.clone()),
            axum::extract::Path("job-suspendable".to_string()),
        )
        .await
        .unwrap();
        assert_eq!(status.0.status.phase, "Suspended");

        // Resume the job
        let resume_res = super::resume_job(
            State(state.clone()),
            axum::extract::Path("job-suspendable".to_string()),
        )
        .await
        .unwrap();
        assert_eq!(resume_res.0.job_id, "job-suspendable");

        // Verify status is now Running
        let status = super::get_job_status(
            State(state.clone()),
            axum::extract::Path("job-suspendable".to_string()),
        )
        .await
        .unwrap();
        assert_eq!(status.0.status.phase, "Running");
    }

    #[tokio::test]
    async fn test_suspend_nonexistent_job() {
        let state = build_state().await;

        // Try to suspend a non-existent job
        let result = super::suspend_job(
            State(state.clone()),
            axum::extract::Path("nonexistent-job".to_string()),
        )
        .await;

        // Should return NotFound error
        assert!(result.is_err());
        match result.unwrap_err() {
            ApiError::NotFound { .. } => {}
            _ => panic!("Expected NotFound error"),
        }
    }

    #[tokio::test]
    async fn test_resume_nonexistent_job() {
        let state = build_state().await;

        // Try to resume a non-existent job
        let result = super::resume_job(
            State(state.clone()),
            axum::extract::Path("nonexistent-job".to_string()),
        )
        .await;

        // Should return NotFound error
        assert!(result.is_err());
        match result.unwrap_err() {
            ApiError::NotFound { .. } => {}
            _ => panic!("Expected NotFound error"),
        }
    }

    #[tokio::test]
    async fn test_job_with_ports() {
        let state = build_state().await;
        std::env::set_var("TENANT_NAMESPACE", "default");

        // Create a job with ports
        let req_body = serde_json::json!({
            "image": "nginx:latest",
            "resources": {"cpu": "1", "memory": "512Mi", "gpus": {"count": 0, "model": []}},
            "ttl_seconds": 3600,
            "name": "job-with-ports",
            "namespace": "default",
            "ports": [
                {"containerPort": 8080, "protocol": "TCP"},
                {"containerPort": 9090, "protocol": "TCP"}
            ]
        });
        let res = super::create_job(
            State(state.clone()),
            Json(serde_json::from_value::<CreateJobRequest>(req_body).unwrap()),
        )
        .await
        .unwrap();
        assert_eq!(res.0.job_id, "job-with-ports");

        // Get status and verify endpoints were generated
        let status = super::get_job_status(
            State(state.clone()),
            axum::extract::Path("job-with-ports".to_string()),
        )
        .await
        .unwrap();

        assert_eq!(status.0.status.phase, "Pending");
        assert_eq!(status.0.status.endpoints.len(), 2);
        assert!(status
            .0
            .status
            .endpoints
            .contains(&"mock-endpoint.local:8080".to_string()));
        assert!(status
            .0
            .status
            .endpoints
            .contains(&"mock-endpoint.local:9090".to_string()));
    }

    #[tokio::test]
    async fn test_job_with_storage_config() {
        let state = build_state().await;
        std::env::set_var("TENANT_NAMESPACE", "default");

        // Create a job with storage configuration
        let mut credentials = std::collections::HashMap::new();
        credentials.insert("access_key".to_string(), "test-key".to_string());
        credentials.insert("secret_key".to_string(), "test-secret".to_string());

        let req_body = serde_json::json!({
            "image": "training-job:latest",
            "resources": {"cpu": "4", "memory": "8Gi", "gpus": {"count": 1, "model": ["H100"]}},
            "ttl_seconds": 7200,
            "name": "job-with-storage",
            "namespace": "default",
            "storage": {
                "backend": "s3",
                "bucket": "my-training-data",
                "prefix": "experiments/exp-001",
                "credentials": credentials
            }
        });
        let res = super::create_job(
            State(state.clone()),
            Json(serde_json::from_value::<CreateJobRequest>(req_body).unwrap()),
        )
        .await
        .unwrap();
        assert_eq!(res.0.job_id, "job-with-storage");

        // Verify job was created successfully
        let status = super::get_job_status(
            State(state.clone()),
            axum::extract::Path("job-with-storage".to_string()),
        )
        .await
        .unwrap();

        assert_eq!(status.0.status.phase, "Pending");
    }

    #[tokio::test]
    async fn test_job_with_ports_and_storage() {
        let state = build_state().await;
        std::env::set_var("TENANT_NAMESPACE", "default");

        // Create a job with both ports and storage
        let req_body = serde_json::json!({
            "image": "ml-server:latest",
            "resources": {"cpu": "2", "memory": "4Gi", "gpus": {"count": 1, "model": ["A100"]}},
            "ttl_seconds": 3600,
            "name": "job-full-config",
            "namespace": "default",
            "ports": [
                {"containerPort": 8000, "protocol": "TCP"},
                {"containerPort": 8001, "protocol": "TCP"}
            ],
            "storage": {
                "backend": "gcs",
                "bucket": "my-models",
                "prefix": "checkpoints",
                "credentials": null
            }
        });
        let res = super::create_job(
            State(state.clone()),
            Json(serde_json::from_value::<CreateJobRequest>(req_body).unwrap()),
        )
        .await
        .unwrap();
        assert_eq!(res.0.job_id, "job-full-config");

        // Get status and verify both endpoints and status
        let status = super::get_job_status(
            State(state.clone()),
            axum::extract::Path("job-full-config".to_string()),
        )
        .await
        .unwrap();

        assert_eq!(status.0.status.phase, "Pending");
        assert_eq!(status.0.status.endpoints.len(), 2);
        assert!(status
            .0
            .status
            .endpoints
            .contains(&"mock-endpoint.local:8000".to_string()));
        assert!(status
            .0
            .status
            .endpoints
            .contains(&"mock-endpoint.local:8001".to_string()));
    }

    #[tokio::test]
    async fn test_job_without_ports_has_no_endpoints() {
        let state = build_state().await;
        std::env::set_var("TENANT_NAMESPACE", "default");

        // Create a job without ports
        let req_body = serde_json::json!({
            "image": "batch-job:latest",
            "resources": {"cpu": "1", "memory": "1Gi", "gpus": {"count": 0, "model": []}},
            "ttl_seconds": 600,
            "name": "job-no-ports",
            "namespace": "default"
        });
        let res = super::create_job(
            State(state.clone()),
            Json(serde_json::from_value::<CreateJobRequest>(req_body).unwrap()),
        )
        .await
        .unwrap();
        assert_eq!(res.0.job_id, "job-no-ports");

        // Get status and verify no endpoints
        let status = super::get_job_status(
            State(state.clone()),
            axum::extract::Path("job-no-ports".to_string()),
        )
        .await
        .unwrap();

        assert_eq!(status.0.status.phase, "Pending");
        assert!(status.0.status.endpoints.is_empty());
    }
}
