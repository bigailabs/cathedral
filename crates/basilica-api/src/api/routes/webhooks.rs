//! Webhook handlers for external provider callbacks

use crate::server::AppState;
use axum::{
    extract::{rejection::JsonRejection, Query, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use basilica_aggregator::models::DeploymentStatus;
use basilica_aggregator::providers::hyperstack::HyperstackCallback;
use serde::Deserialize;
use serde_json::json;

/// Query parameters for webhook authentication
#[derive(Deserialize)]
pub struct WebhookQuery {
    token: String,
}

/// Handle Hyperstack webhook callback
/// POST /webhooks/hyperstack?token=...
pub async fn hyperstack_callback(
    State(state): State<AppState>,
    Query(query): Query<WebhookQuery>,
    payload: Result<Json<HyperstackCallback>, JsonRejection>,
) -> impl IntoResponse {
    let payload = match payload {
        Ok(Json(payload)) => payload,
        Err(err) => {
            tracing::warn!(error = %err, "Invalid Hyperstack webhook payload");
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({ "error": "invalid_payload" })),
            );
        }
    };
    let token = query.token;
    tracing::info!("Received Hyperstack webhook callback");

    // Validate token against configured webhook secret
    let hyperstack_config = match &state.aggregator_config.providers.hyperstack {
        Some(config) => config,
        None => {
            tracing::warn!("Webhook received but Hyperstack not configured");
            return (StatusCode::OK, Json(json!({ "status": "ignored" })));
        }
    };

    // Hyperstack lowercases callback URL query params, so compare case-insensitively
    if token.to_lowercase() != hyperstack_config.webhook_secret.to_lowercase() {
        tracing::warn!("Invalid webhook token received");
        // Return 200 to avoid information leakage
        return (StatusCode::OK, Json(json!({ "status": "unauthorized" })));
    }

    // Get VM ID from payload
    let vm_id = payload.vm_id();

    // Look up deployment by provider_instance_id
    let deployment = match sqlx::query_as::<_, (String, String)>(
        "SELECT id, status FROM secure_cloud_rentals
         WHERE provider_instance_id = $1 AND provider = 'hyperstack'",
    )
    .bind(vm_id)
    .fetch_optional(&state.db)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => {
            // Unknown VM ID - log and return 200
            tracing::trace!(vm_id = %vm_id, "Webhook received for unknown VM ID");
            return (StatusCode::OK, Json(json!({ "status": "unknown_vm" })));
        }
        Err(e) => {
            tracing::error!("Database error: {}", e);
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "error": "internal" })),
            );
        }
    };

    let (rental_id, current_status) = deployment;
    let new_status = map_callback_status(&payload);

    tracing::info!(
        rental_id = %rental_id,
        old_status = %current_status,
        new_status = ?new_status,
        operation_name = %payload.operation_name(),
        operation_status = %payload.operation_status(),
        "Processing webhook status update"
    );

    // Extract floating IP from data if available
    let ip_address = payload
        .data
        .as_ref()
        .and_then(|d| d.get("floating_ip"))
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(String::from);

    if ip_address.is_some() {
        tracing::info!(ip = ?ip_address, "Extracted floating IP from webhook");
    }

    // Build connection_info if we have an IP
    let connection_info = ip_address.as_ref().map(|ip| {
        json!({
            "ssh_host": ip,
            "ssh_port": 22,
            "ssh_user": "ubuntu"
        })
    });

    // Handle based on new status
    match new_status {
        DeploymentStatus::Running => {
            // VM is ready - update status, IP, and connection_info if available
            if let Err(e) = sqlx::query(
                r#"UPDATE secure_cloud_rentals SET
                   status = $1,
                   ip_address = COALESCE($2, ip_address),
                   connection_info = CASE WHEN $2 IS NOT NULL THEN $3 ELSE connection_info END,
                   updated_at = NOW()
                   WHERE id = $4"#,
            )
            .bind("running")
            .bind(&ip_address)
            .bind(&connection_info)
            .bind(&rental_id)
            .execute(&state.db)
            .await
            {
                tracing::error!("Failed to update rental status: {}", e);
            }
        }
        _ => {
            // Any non-running status - just update
            if let Err(e) = sqlx::query(
                "UPDATE secure_cloud_rentals SET status = $1, updated_at = NOW() WHERE id = $2",
            )
            .bind(new_status.as_str())
            .bind(&rental_id)
            .execute(&state.db)
            .await
            {
                tracing::error!("Failed to update rental status: {}", e);
            }
        }
    }

    (StatusCode::OK, Json(json!({ "status": "processed" })))
}

/// Map Hyperstack callback operation/status to DeploymentStatus
fn map_callback_status(callback: &HyperstackCallback) -> DeploymentStatus {
    let op_name = callback.operation_name().to_lowercase();

    if let Some(status) = callback
        .data
        .as_ref()
        .and_then(|d| d.get("status"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_lowercase())
    {
        return map_hyperstack_status(&status, &op_name);
    }

    let op_status = callback.operation_status().to_lowercase();
    map_hyperstack_status(&op_status, &op_name)
}

fn map_hyperstack_status(status: &str, op_name: &str) -> DeploymentStatus {
    let status = status.trim();
    if status.is_empty() {
        return DeploymentStatus::Pending;
    }

    match status {
        "active" | "running" => return DeploymentStatus::Running,
        "build" | "building" | "creating" | "provisioning" => {
            return DeploymentStatus::Provisioning
        }
        "deleting" | "deleted" | "terminated" => return DeploymentStatus::Deleted,
        "error" | "failed" => return DeploymentStatus::Error,
        "success" => {
            if op_name.contains("delete") || op_name.contains("deleted") {
                return DeploymentStatus::Deleted;
            }
            if op_name.contains("create") || op_name.contains("created") {
                return DeploymentStatus::Running;
            }
        }
        _ => {}
    }

    if op_name.contains("delete") || op_name.contains("deleted") {
        return DeploymentStatus::Deleted;
    }
    if op_name.contains("create") || op_name.contains("created") {
        return DeploymentStatus::Provisioning;
    }

    DeploymentStatus::Pending
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn callback_from(value: serde_json::Value) -> HyperstackCallback {
        serde_json::from_value(value).expect("valid callback")
    }

    #[test]
    fn map_status_from_data_status() {
        let cases = vec![
            ("CREATING", DeploymentStatus::Provisioning),
            ("BUILD", DeploymentStatus::Provisioning),
            ("ACTIVE", DeploymentStatus::Running),
            ("DELETED", DeploymentStatus::Deleted),
            ("ERROR", DeploymentStatus::Error),
        ];

        for (status, expected) in cases {
            let callback = callback_from(json!({
                "resource": { "id": "507279" },
                "operation": { "name": "createInstance", "status": "CREATING" },
                "data": { "status": status }
            }));
            assert_eq!(map_callback_status(&callback), expected, "status={status}");
        }
    }

    #[test]
    fn map_status_from_operation_status() {
        let callback = callback_from(json!({
            "resource": { "id": "507279" },
            "operation": { "name": "createInstance", "status": "building" }
        }));
        assert_eq!(
            map_callback_status(&callback),
            DeploymentStatus::Provisioning
        );

        let callback = callback_from(json!({
            "resource": { "id": "507279" },
            "operation": { "name": "createInstance", "status": "active" }
        }));
        assert_eq!(map_callback_status(&callback), DeploymentStatus::Running);

        let callback = callback_from(json!({
            "resource": { "id": "507279" },
            "operation": { "name": "deleteInstances", "status": "DELETING" }
        }));
        assert_eq!(map_callback_status(&callback), DeploymentStatus::Deleted);

        let callback = callback_from(json!({
            "resource": { "id": "507279" },
            "operation": { "name": "deleteInstances", "status": "DELETED" }
        }));
        assert_eq!(map_callback_status(&callback), DeploymentStatus::Deleted);

        let callback = callback_from(json!({
            "resource": { "id": "507279" },
            "operation": { "name": "deleteInstance", "status": "FAILED" }
        }));
        assert_eq!(map_callback_status(&callback), DeploymentStatus::Error);
    }
}
