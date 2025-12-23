//! Webhook handlers for external provider callbacks

use crate::api::routes::secure_cloud::stop_secure_cloud_rental_internal;
use crate::server::AppState;
use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use basilica_aggregator::models::DeploymentStatus;
use basilica_aggregator::providers::hyperstack::HyperstackCallback;
use serde_json::json;

/// Handle Hyperstack webhook callback
/// POST /webhooks/hyperstack/{token}
pub async fn hyperstack_callback(
    State(state): State<AppState>,
    Path(token): Path<String>,
    Json(payload): Json<HyperstackCallback>,
) -> impl IntoResponse {
    tracing::info!(
        operation = %payload.operation.name,
        status = %payload.operation.status,
        resource_id = %payload.resource.id,
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

    // Look up deployment by provider_instance_id
    let deployment = match sqlx::query_as::<_, (String, String)>(
        "SELECT id, status FROM secure_cloud_rentals
         WHERE provider_instance_id = $1 AND provider = 'hyperstack'",
    )
    .bind(&payload.resource.id)
    .fetch_optional(&state.db)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => {
            // Unknown VM ID - log and return 200
            tracing::warn!(
                resource_id = %payload.resource.id,
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
        "Processing webhook status update"
    );

    // Handle based on new status
    match new_status {
        DeploymentStatus::Running => {
            // VM is ready - update status and IP if available
            let ip_address = payload
                .data
                .as_ref()
                .and_then(|d| d.get("floating_ip"))
                .and_then(|v| v.as_str())
                .map(String::from);

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
    match (
        callback.operation.name.as_str(),
        callback.operation.status.as_str(),
    ) {
        ("createVM", "SUCCESS") => DeploymentStatus::Running,
        ("createVM", "FAILED") => DeploymentStatus::Error,
        ("deleteVM", "SUCCESS") => DeploymentStatus::Deleted,
        ("deleteVM", "FAILED") => DeploymentStatus::Error,
        (_, "SUCCESS") => DeploymentStatus::Running,
        (_, "FAILED") => DeploymentStatus::Error,
        _ => DeploymentStatus::Pending,
    }
}
