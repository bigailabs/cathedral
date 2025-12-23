//! Webhook handlers for external provider callbacks

use crate::api::routes::secure_cloud::stop_secure_cloud_rental_internal;
use crate::server::AppState;
use axum::{
    extract::{Query, State},
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
    Json(payload): Json<HyperstackCallback>,
) -> impl IntoResponse {
    let token = query.token;
    // Log full payload for debugging
    tracing::info!(
        payload = ?payload,
        "Received Hyperstack webhook callback"
    );

    // Validate token against configured webhook secret
    let hyperstack_config = match &state.aggregator_config.providers.hyperstack {
        Some(config) => config,
        None => {
            tracing::warn!("Webhook received but Hyperstack not configured");
            return (StatusCode::OK, Json(json!({ "status": "ignored" })));
        }
    };

    if token != hyperstack_config.webhook_secret {
        tracing::warn!("Invalid webhook token received");
        // Return 200 to avoid information leakage
        return (StatusCode::OK, Json(json!({ "status": "unauthorized" })));
    }

    // Get VM ID from payload
    let vm_id = match payload.vm_id() {
        Some(id) => id,
        None => {
            tracing::warn!(
                payload = ?payload,
                "Webhook received without VM ID"
            );
            return (StatusCode::OK, Json(json!({ "status": "no_vm_id" })));
        }
    };

    // Look up deployment by provider_instance_id
    let deployment = match sqlx::query_as::<_, (String, String)>(
        "SELECT id, status FROM secure_cloud_rentals
         WHERE provider_instance_id = $1 AND provider = 'hyperstack'",
    )
    .bind(&vm_id)
    .fetch_optional(&state.db)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => {
            // Unknown VM ID - log and return 200
            tracing::warn!(
                vm_id = %vm_id,
                "Webhook received for unknown VM ID"
            );
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
        operation_name = ?payload.operation_name(),
        operation_status = ?payload.operation_status(),
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

    // Handle based on new status
    match new_status {
        DeploymentStatus::Running => {
            // VM is ready - update status and IP if available
            if let Err(e) = sqlx::query(
                "UPDATE secure_cloud_rentals SET status = $1, ip_address = COALESCE($2, ip_address), updated_at = NOW() WHERE id = $3",
            )
            .bind("running")
            .bind(&ip_address)
            .bind(&rental_id)
            .execute(&state.db)
            .await
            {
                tracing::error!("Failed to update rental status: {}", e);
            }
        }
        DeploymentStatus::Deleted | DeploymentStatus::Error => {
            // Terminal state - archive and finalize billing
            let reason = if new_status == DeploymentStatus::Deleted {
                "vm_deleted_via_webhook"
            } else {
                "vm_error_via_webhook"
            };

            let target_status = if new_status == DeploymentStatus::Deleted {
                basilica_protocol::billing::RentalStatus::Stopped
            } else {
                basilica_protocol::billing::RentalStatus::Failed
            };

            // Use existing stop logic (skip_provider_delete=true since VM already gone)
            if let Err(e) = stop_secure_cloud_rental_internal(
                &state.aggregator_service,
                state.billing_client.as_deref(),
                &state.db,
                &rental_id,
                reason,
                target_status,
                true, // skip_provider_delete since VM state change came from provider
            )
            .await
            {
                tracing::error!("Failed to finalize rental from webhook: {}", e);
            }
        }
        _ => {
            // Provisioning or other interim status - just update
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
    let op_name = callback.operation_name().unwrap_or("").to_lowercase();
    let op_status = callback.operation_status().unwrap_or("").to_lowercase();

    // Also check data.status if available (more reliable for VM state)
    let data_status = callback
        .data
        .as_ref()
        .and_then(|d| d.get("status"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_lowercase();

    // Priority: check data.status first (reflects actual VM state)
    if data_status == "active" || data_status == "running" {
        return DeploymentStatus::Running;
    }
    if data_status == "error" || data_status == "failed" {
        return DeploymentStatus::Error;
    }
    if data_status == "deleted" || data_status == "terminated" {
        return DeploymentStatus::Deleted;
    }
    if data_status == "build" || data_status == "building" || data_status == "creating" {
        return DeploymentStatus::Provisioning;
    }

    // Fall back to operation-based status
    match (op_name.as_str(), op_status.as_str()) {
        // createInstance operations
        ("createinstance", "success") | ("createvm", "success") => DeploymentStatus::Running,
        ("createinstance", "failed") | ("createvm", "failed") => DeploymentStatus::Error,
        ("createinstance", "building") | ("createinstance", "creating") => {
            DeploymentStatus::Provisioning
        }
        // deleteInstance operations
        ("deleteinstance", "success") | ("deletevm", "success") => DeploymentStatus::Deleted,
        ("deleteinstance", "failed") | ("deletevm", "failed") => DeploymentStatus::Error,
        // Generic status handling
        (_, "success") | (_, "active") | (_, "running") => DeploymentStatus::Running,
        (_, "failed") | (_, "error") => DeploymentStatus::Error,
        (_, "deleted") | (_, "terminated") => DeploymentStatus::Deleted,
        (_, "building") | (_, "creating") | (_, "provisioning") => DeploymentStatus::Provisioning,
        _ => DeploymentStatus::Pending,
    }
}
