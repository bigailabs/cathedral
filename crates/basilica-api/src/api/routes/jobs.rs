use axum::{extract::State, routing::get, Json};
use serde::{Deserialize, Serialize};

use crate::{
    error::{ApiError, Result},
    k8s_client::{ApiK8sClient, JobSpecDto, JobStatusDto, Resources},
    server::AppState,
};

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
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreateJobResponse { pub job_id: String }

pub async fn create_job(State(state): State<AppState>, Json(req): Json<CreateJobRequest>) -> Result<Json<CreateJobResponse>> {
    let client = state
        .k8s
        .as_ref()
        .ok_or_else(|| ApiError::ServiceUnavailable)?;
    let name = req.name.clone().unwrap_or_else(|| format!("job-{}", rand::random::<u32>()));
    let ns = req.namespace.clone().unwrap_or_else(|| "default".into());
    let spec = JobSpecDto { image: req.image, command: req.command, args: req.args, env: req.env, resources: req.resources, ttl_seconds: req.ttl_seconds };
    let id = client.create_job(&ns, &name, spec).await?;
    Ok(Json(CreateJobResponse { job_id: id }))
}

#[derive(Debug, Clone, Serialize)]
pub struct JobStatusResponse { pub job_id: String, pub status: JobStatusDto }

pub async fn get_job_status(State(state): State<AppState>, axum::extract::Path(job_id): axum::extract::Path<String>) -> Result<Json<JobStatusResponse>> {
    let client = state.k8s.as_ref().ok_or_else(|| ApiError::ServiceUnavailable)?;
    let ns = "default"; // could be inferred from tenancy in future
    let st = client.get_job_status(ns, &job_id).await?;
    Ok(Json(JobStatusResponse { job_id, status: st }))
}

#[derive(Debug, Clone, Serialize)]
pub struct DeleteJobResponse { pub job_id: String }

pub async fn delete_job(State(state): State<AppState>, axum::extract::Path(job_id): axum::extract::Path<String>) -> Result<Json<DeleteJobResponse>> {
    let client = state.k8s.as_ref().ok_or_else(|| ApiError::ServiceUnavailable)?;
    let ns = "default";
    client.delete_job(ns, &job_id).await?;
    Ok(Json(DeleteJobResponse { job_id }))
}

#[derive(Debug, Clone, Serialize)]
pub struct JobLogsResponse { pub job_id: String, pub logs: String }

pub async fn get_job_logs(State(state): State<AppState>, axum::extract::Path(job_id): axum::extract::Path<String>) -> Result<Json<JobLogsResponse>> {
    let client = state.k8s.as_ref().ok_or_else(|| ApiError::ServiceUnavailable)?;
    let ns = "default";
    let logs = client.get_job_logs(ns, &job_id).await?;
    Ok(Json(JobLogsResponse { job_id, logs }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::StatusCode;
    use std::sync::Arc;

    async fn build_state() -> AppState {
        let client = crate::k8s_client::MockK8sClient::default();
        AppState {
            config: std::sync::Arc::new(crate::config::Config::default()),
            validator_client: std::sync::Arc::new(basilica_validator::ValidatorClient::new("http://localhost", std::time::Duration::from_secs(1)).unwrap()),
            validator_endpoint: "http://localhost".into(),
            validator_uid: 0,
            validator_hotkey: "".into(),
            http_client: reqwest::Client::builder().build().unwrap(),
            db: sqlx::PgPool::connect_lazy("postgres://user:pass@localhost/db").unwrap_or_else(|_| unsafe { std::mem::zeroed() }),
            k8s: Some(Arc::new(client)),
        }
    }

    #[tokio::test]
    async fn create_get_delete_job_flow() {
        let state = build_state().await;

        let req_body = serde_json::json!({
            "image": "img",
            "resources": {"cpu": "1", "memory": "512Mi", "gpus": {"count": 0, "model": []}},
            "ttl_seconds": 0,
            "name": "job-test",
            "namespace": "default"
        });
        let res = super::create_job(State(state.clone()), Json(serde_json::from_value::<CreateJobRequest>(req_body).unwrap())).await.unwrap();
        let body = res.0;
        assert_eq!(body.job_id, "job-test");

        let res2 = super::get_job_status(State(state.clone()), axum::extract::Path("job-test".to_string())).await.unwrap();
        assert_eq!(res2.0.status.phase, "Pending");

        let res3 = super::delete_job(State(state.clone()), axum::extract::Path("job-test".to_string())).await.unwrap();
        assert_eq!(res3.0.job_id, "job-test");
    }
}
