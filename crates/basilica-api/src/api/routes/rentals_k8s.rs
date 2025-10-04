use axum::{extract::State, Json, http, body::Body};
use serde::{Deserialize, Serialize};

use crate::{
    error::{ApiError, Result},
    k8s_client::{ApiK8sClient, RentalSpecDto, RentalStatusDto, Resources},
    server::AppState,
};

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
pub struct CreateRentalResponse { pub rental_id: String }

pub async fn create_rental(State(state): State<AppState>, Json(req): Json<CreateRentalRequest>) -> Result<Json<CreateRentalResponse>> {
    let client = state.k8s.as_ref().ok_or_else(|| ApiError::ServiceUnavailable)?;
    let name = req.name.clone().unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let ns = req.namespace.clone().unwrap_or_else(|| "default".into());
    let spec = RentalSpecDto { container_image: req.container_image, resources: req.resources, name: Some(name.clone()), namespace: Some(ns.clone()) };
    let id = client.create_rental(&ns, &name, spec).await?;
    Ok(Json(CreateRentalResponse { rental_id: id }))
}

#[derive(Debug, Clone, Serialize)]
pub struct RentalStatusResponse { pub rental_id: String, pub status: RentalStatusDto }

pub async fn get_rental_status(State(state): State<AppState>, axum::extract::Path(rental_id): axum::extract::Path<String>) -> Result<Json<RentalStatusResponse>> {
    let client = state.k8s.as_ref().ok_or_else(|| ApiError::ServiceUnavailable)?;
    let st = client.get_rental_status("default", &rental_id).await?;
    Ok(Json(RentalStatusResponse { rental_id, status: st }))
}

#[derive(Debug, Clone, Serialize)]
pub struct DeleteRentalResponse { pub rental_id: String }

pub async fn delete_rental(State(state): State<AppState>, axum::extract::Path(rental_id): axum::extract::Path<String>) -> Result<Json<DeleteRentalResponse>> {
    let client = state.k8s.as_ref().ok_or_else(|| ApiError::ServiceUnavailable)?;
    client.delete_rental("default", &rental_id).await?;
    Ok(Json(DeleteRentalResponse { rental_id }))
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
    async fn create_get_delete_rental_flow() {
        let state = build_state().await;
        let req_body = serde_json::json!({
            "container_image": "img",
            "resources": {"cpu": "1", "memory": "512Mi", "gpus": {"count": 0, "model": []}},
            "name": "rent-test",
            "namespace": "default"
        });
        let create = super::create_rental(State(state.clone()), Json(serde_json::from_value::<CreateRentalRequest>(req_body).unwrap())).await.unwrap();
        assert_eq!(create.0.rental_id, "rent-test");
        let status = super::get_rental_status(State(state.clone()), axum::extract::Path("rent-test".to_string())).await.unwrap();
        assert_eq!(status.0.status.state, "Provisioning");
        let del = super::delete_rental(State(state.clone()), axum::extract::Path("rent-test".to_string())).await.unwrap();
        assert_eq!(del.0.rental_id, "rent-test");
    }
}
