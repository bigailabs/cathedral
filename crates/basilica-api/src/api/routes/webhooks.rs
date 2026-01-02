//! Webhook handlers for external provider callbacks

use crate::{api::extractors::ownership::archive_secure_cloud_rental, server::AppState};
use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use basilica_aggregator::models::DeploymentStatus;
use basilica_aggregator::providers::hyperstack::HyperstackCallback;
use basilica_protocol::billing::{FinalizeRentalRequest, RentalStatus};
use chrono::Utc;
use prost_types::Timestamp;
use serde::Deserialize;
use serde_json::json;

/// Query parameters for webhook authentication
#[derive(Deserialize)]
pub struct WebhookQuery {
    token: String,
}

/// Handle Hyperstack webhook callback
/// POST /webhooks/hyperstack?token=...
#[tracing::instrument(skip_all, fields(provider = "hyperstack", cloud_type = "secure"))]
pub async fn hyperstack_callback(
    State(state): State<AppState>,
    Query(query): Query<WebhookQuery>,
    body: axum::body::Bytes,
) -> impl IntoResponse {
    let payload: HyperstackCallback = match serde_json::from_slice(&body) {
        Ok(payload) => payload,
        Err(err) => {
            let body_str = String::from_utf8_lossy(&body);
            tracing::warn!(error = %err, raw_body = %body_str, "Invalid Hyperstack webhook payload");
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({ "error": "invalid_payload" })),
            );
        }
    };
    let token = query.token;
    tracing::debug!(
        vm_id = %payload.vm_id(),
        operation = %payload.operation_name(),
        operation_status = %payload.operation_status(),
        resource_name = ?payload.resource.name,
        resource_type = ?payload.resource.resource_type,
        has_data = payload.data.is_some(),
        has_user_payload = payload.user_payload.is_some(),
        extra_fields = ?payload.extra.keys().collect::<Vec<_>>(),
        "Received Hyperstack webhook callback"
    );

    if let Some(ref data) = payload.data {
        tracing::trace!(data = %data, "Webhook data payload");
    }

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
        tracing::warn!(
            operation = %payload.operation_name(),
            vm_id = %payload.vm_id(),
            "Invalid webhook token received"
        );
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
            tracing::debug!(
                vm_id = %vm_id,
                operation = %payload.operation_name(),
                operation_status = %payload.operation_status(),
                "Webhook received for unknown VM ID - may be from a different environment or already deleted"
            );
            return (StatusCode::OK, Json(json!({ "status": "unknown_vm" })));
        }
        Err(e) => {
            tracing::error!(
                error = %e,
                vm_id = %vm_id,
                operation = %payload.operation_name(),
                "Database error looking up rental by provider_instance_id"
            );
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

    // Determine whether this is a terminal delete event (not just "deleting")
    let raw_status = payload
        .data
        .as_ref()
        .and_then(|d| d.get("status"))
        .and_then(|v| v.as_str())
        .unwrap_or(payload.operation_status());
    let raw_status_lower = raw_status.to_lowercase();

    if matches!(new_status, DeploymentStatus::Deleted) && raw_status_lower != "deleting" {
        tracing::info!(
            rental_id = %rental_id,
            raw_status = %raw_status,
            "Webhook indicates VM deletion; finalizing billing and archiving rental"
        );

        if let Some(billing_client) = state.billing_client.as_deref() {
            let now = Utc::now();
            let end_timestamp = Timestamp {
                seconds: now.timestamp(),
                nanos: now.timestamp_subsec_nanos() as i32,
            };

            let finalize_request = FinalizeRentalRequest {
                rental_id: rental_id.clone(),
                end_time: Some(end_timestamp),
                termination_reason: "vm_deleted_externally".to_string(),
                target_status: RentalStatus::Stopped.into(),
            };

            if let Err(e) = billing_client.finalize_rental(finalize_request).await {
                tracing::error!(
                    error = %e,
                    rental_id = %rental_id,
                    "Failed to finalize billing for webhook-deleted rental"
                );
            } else {
                tracing::info!(
                    rental_id = %rental_id,
                    "Finalized billing for webhook-deleted rental"
                );
            }
        }

        if let Err(e) = archive_secure_cloud_rental(
            &state.db,
            &rental_id,
            Some("Webhook: VM deleted externally"),
            Some("deleted"),
        )
        .await
        {
            tracing::error!(
                error = %e,
                rental_id = %rental_id,
                "Failed to archive rental after webhook delete"
            );
        }

        return (StatusCode::OK, Json(json!({ "status": "archived" })));
    }

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
                tracing::error!(
                    error = %e,
                    rental_id = %rental_id,
                    new_status = ?new_status,
                    "Failed to update rental status"
                );
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
                tracing::error!(
                    error = %e,
                    rental_id = %rental_id,
                    new_status = ?new_status,
                    "Failed to update rental status"
                );
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
