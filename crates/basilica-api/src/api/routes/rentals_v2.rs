use axum::{
    extract::State,
    response::{sse::Event, Sse},
    Json,
};
use futures::Stream;
use serde::{Deserialize, Serialize};

use crate::{
    error::{ApiError, Result},
    k8s_client::{ApiK8sClient, RentalListItemDto, RentalSpecDto, RentalStatusDto, Resources},
    server::AppState,
};
use crate::metrics as apimetrics;
use std::time::Instant;

#[derive(Debug, Clone, Deserialize)]
pub struct CreateRentalRequest {
    pub container_image: String,
    pub resources: Resources,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub namespace: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CreateRentalResponse {
    pub rental_id: String,
}

// List rentals in namespace (v2 K8s backend)
pub async fn list_rentals(State(state): State<AppState>) -> Result<Json<Vec<RentalStatusResponse>>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.list", "GET", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let items: Vec<RentalListItemDto> = client.list_rentals("default").await?;
    let out: Vec<RentalStatusResponse> = items
        .into_iter()
        .map(|it| RentalStatusResponse { rental_id: it.rental_id, status: it.status })
        .collect();
    apimetrics::record_request("rentals_v2.list", "GET", start, true);
    Ok(Json(out))
}

pub async fn create_rental(State(state): State<AppState>, Json(req): Json<CreateRentalRequest>) -> Result<Json<CreateRentalResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.create", "POST", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let name = req.name.clone().unwrap_or_else(|| format!("rent-{}", rand::random::<u32>()));
    let ns = req.namespace.clone().unwrap_or_else(|| "default".into());
    let spec = RentalSpecDto { container_image: req.container_image, resources: req.resources, name: Some(name.clone()), namespace: Some(ns.clone()) };
    let id = client.create_rental(&ns, &name, spec).await?;
    apimetrics::record_rental_created(&ns);
    apimetrics::record_request("rentals_v2.create", "POST", start, true);
    Ok(Json(CreateRentalResponse { rental_id: id }))
}

#[derive(Debug, Clone, Serialize)]
pub struct RentalStatusResponse {
    pub rental_id: String,
    pub status: RentalStatusDto,
}

pub async fn get_rental_status(State(state): State<AppState>, axum::extract::Path(rental_id): axum::extract::Path<String>) -> Result<Json<RentalStatusResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.status", "GET", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let st = client.get_rental_status("default", &rental_id).await?;
    apimetrics::record_request("rentals_v2.status", "GET", start, true);
    Ok(Json(RentalStatusResponse { rental_id, status: st }))
}

#[derive(Debug, Clone, Serialize)]
pub struct DeleteRentalResponse {
    pub rental_id: String,
}

pub async fn delete_rental(State(state): State<AppState>, axum::extract::Path(rental_id): axum::extract::Path<String>) -> Result<Json<DeleteRentalResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.delete", "DELETE", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    client.delete_rental("default", &rental_id).await?;
    apimetrics::record_request("rentals_v2.delete", "DELETE", start, true);
    Ok(Json(DeleteRentalResponse { rental_id }))
}

// Stream rental logs (similar shape to container-based logs)
pub async fn stream_rental_logs(
    State(state): State<AppState>,
    axum::extract::Path(rental_id): axum::extract::Path<String>,
    axum::extract::Query(query): axum::extract::Query<basilica_sdk::types::LogStreamQuery>,
) -> Result<Sse<impl Stream<Item = std::result::Result<Event, std::io::Error>>>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.logs", "GET", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let logs = client.get_rental_logs("default", &rental_id).await?;

    let follow = query.follow.unwrap_or(false);
    let lines: Vec<String> = logs.lines().map(|s| s.to_string()).collect();

    let stream = async_stream::stream! {
        // Emit existing log lines as SSE events (timestamp/stream/message), similar to container-based logs
        for line in &lines {
            let data = serde_json::json!({
                "timestamp": chrono::Utc::now(),
                "stream": "stdout",
                "message": line,
            });
            yield Ok(Event::default().data(data.to_string()));
        }
        // In follow mode, a real implementation would continue tailing; the mock ends here
        if !follow {
            // end of stream
        }
    };

    apimetrics::record_request("rentals_v2.logs", "GET", start, true);
    Ok(Sse::new(stream))
}

// Exec into a rental container (similar to container-based exec)
#[derive(Debug, Clone, Deserialize)]
pub struct ExecRequest {
    pub command: Vec<String>,
    #[serde(default)]
    pub stdin: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExecResponse {
    pub stdout: String,
    #[serde(default)]
    pub stderr: String,
    #[serde(default)]
    pub exit_code: i32,
}

pub async fn exec_rental(
    State(state): State<AppState>,
    axum::extract::Path(rental_id): axum::extract::Path<String>,
    Json(req): Json<ExecRequest>,
) -> Result<Json<ExecResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.exec", "POST", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let out = client.exec_rental("default", &rental_id, req.command).await?;
    apimetrics::record_request("rentals_v2.exec", "POST", start, true);
    Ok(Json(ExecResponse { stdout: out, stderr: String::new(), exit_code: 0 }))
}

// Extend a rental's duration
#[derive(Debug, Clone, Deserialize)]
pub struct ExtendRentalRequest { pub additional_hours: u32 }

pub async fn extend_rental(
    State(state): State<AppState>,
    axum::extract::Path(rental_id): axum::extract::Path<String>,
    Json(req): Json<ExtendRentalRequest>,
) -> Result<Json<RentalStatusResponse>> {
    let start = Instant::now();
    let _ = req; // unused in pay-as-you-go
    // Under pay-as-you-go, extension is not supported; rentals are terminated when out of credits.
    apimetrics::record_request("rentals_v2.extend", "POST", start, false);
    Err(ApiError::BadRequest { message: "Extend is not supported under pay-as-you-go".into() })
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
    async fn v2_rental_create_get_delete() {
        let state = build_state().await;
        let req_body = CreateRentalRequest { container_image: "img".into(), resources: Resources { cpu: "1".into(), memory: "512Mi".into(), gpus: crate::k8s_client::GpuSpec { count: 0, model: vec![] } }, name: Some("rent-v2".into()), namespace: Some("default".into()) };
        let create = super::create_rental(State(state.clone()), Json(req_body)).await.unwrap();
        assert_eq!(create.0.rental_id, "rent-v2");
        let status = super::get_rental_status(State(state.clone()), axum::extract::Path("rent-v2".to_string())).await.unwrap();
        assert!(!status.0.status.state.is_empty());
        let del = super::delete_rental(State(state.clone()), axum::extract::Path("rent-v2".to_string())).await.unwrap();
        assert_eq!(del.0.rental_id, "rent-v2");
    }

    #[tokio::test]
    async fn v2_rental_exec() {
        let state = build_state().await;
        // Create first
        let req_body = CreateRentalRequest { container_image: "img".into(), resources: Resources { cpu: "1".into(), memory: "512Mi".into(), gpus: crate::k8s_client::GpuSpec { count: 0, model: vec![] } }, name: Some("rent-v2-exec".into()), namespace: Some("default".into()) };
        let _ = super::create_rental(State(state.clone()), Json(req_body)).await.unwrap();
        // Exec
        let exec_req = ExecRequest { command: vec!["echo".into(), "hello".into()], stdin: None };
        let resp = super::exec_rental(State(state.clone()), axum::extract::Path("rent-v2-exec".to_string()), Json(exec_req)).await.unwrap();
        assert!(resp.0.stdout.contains("echo hello"));
    }

    #[tokio::test]
    async fn v2_rental_extend() {
        let state = build_state().await;
        let req_body = CreateRentalRequest { container_image: "img".into(), resources: Resources { cpu: "1".into(), memory: "512Mi".into(), gpus: crate::k8s_client::GpuSpec { count: 0, model: vec![] } }, name: Some("rent-v2-extend".into()), namespace: Some("default".into()) };
        let _ = super::create_rental(State(state.clone()), Json(req_body)).await.unwrap();
        let err = super::extend_rental(
            State(state.clone()),
            axum::extract::Path("rent-v2-extend".to_string()),
            Json(ExtendRentalRequest { additional_hours: 2 }),
        )
        .await;
        assert!(err.is_err());
    }

    #[tokio::test]
    async fn v2_rental_list() {
        let state = build_state().await;
        let req_body1 = CreateRentalRequest { container_image: "img".into(), resources: Resources { cpu: "1".into(), memory: "512Mi".into(), gpus: crate::k8s_client::GpuSpec { count: 0, model: vec![] } }, name: Some("rent-a".into()), namespace: Some("default".into()) };
        let _ = super::create_rental(State(state.clone()), Json(req_body1)).await.unwrap();
        let req_body2 = CreateRentalRequest { container_image: "img".into(), resources: Resources { cpu: "1".into(), memory: "512Mi".into(), gpus: crate::k8s_client::GpuSpec { count: 0, model: vec![] } }, name: Some("rent-b".into()), namespace: Some("default".into()) };
        let _ = super::create_rental(State(state.clone()), Json(req_body2)).await.unwrap();
        let list = super::list_rentals(State(state.clone())).await.unwrap();
        let ids: Vec<String> = list.0.into_iter().map(|x| x.rental_id).collect();
        assert!(ids.contains(&"rent-a".to_string()));
        assert!(ids.contains(&"rent-b".to_string()));
    }
}
