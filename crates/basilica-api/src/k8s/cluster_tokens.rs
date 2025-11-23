use crate::db::cluster_tokens as db;
use crate::error::Result;
use crate::k8s::k3s_commands;
use chrono::{Duration, Utc};
use metrics::{counter, histogram};
use sqlx::PgPool;
use tracing::{debug, info, warn};

pub use crate::db::cluster_tokens::ClusterTokenRecord;

pub async fn get_or_create_cluster_token(
    pool: &PgPool,
    user_id: &str,
    node_id: &str,
    datacenter_id: &str,
) -> Result<String> {
    let start = std::time::Instant::now();

    if let Some(existing_token) = db::get_cluster_token(pool, user_id, node_id).await? {
        if !existing_token.is_expired() {
            counter!("node_tokens_reused_total", "datacenter" => datacenter_id.to_string())
                .increment(1);
            histogram!("node_token_operation_duration_seconds", "operation" => "reuse")
                .record(start.elapsed().as_secs_f64());
            info!(
                node_id = %node_id,
                datacenter_id = %datacenter_id,
                "Reusing existing token"
            );
            return Ok(existing_token.full_token());
        }

        info!(
            node_id = %node_id,
            datacenter_id = %datacenter_id,
            "Existing token expired, deleting"
        );
        if let Err(e) = k3s_commands::delete_token(&existing_token.token_id).await {
            warn!(
                token_id = %existing_token.token_id,
                error = %e,
                "Failed to delete expired k3s token, continuing with new token creation"
            );
        }
        db::delete_cluster_token(pool, user_id, node_id).await?;
    }

    let token_id =
        k3s_commands::generate_token_id()
            .await
            .map_err(|e| crate::error::ApiError::Internal {
                message: format!("Failed to generate K3s token ID: {}", e),
            })?;

    let full_token = k3s_commands::create_token(node_id, datacenter_id, &token_id)
        .await
        .map_err(|e| crate::error::ApiError::Internal {
            message: format!("Failed to create K3s token: {}", e),
        })?;

    let token_secret = full_token.clone();

    let expires_at = Utc::now() + Duration::hours(1);

    db::insert_cluster_token(pool, user_id, node_id, &token_id, &token_secret, expires_at).await?;

    counter!("node_tokens_created_total", "datacenter" => datacenter_id.to_string()).increment(1);
    histogram!("node_token_operation_duration_seconds", "operation" => "create")
        .record(start.elapsed().as_secs_f64());

    info!(
        node_id = %node_id,
        datacenter_id = %datacenter_id,
        "Created and stored new token"
    );

    Ok(full_token)
}

pub async fn revoke_cluster_token(pool: &PgPool, user_id: &str, node_id: &str) -> Result<()> {
    if let Some(token) = db::get_cluster_token(pool, user_id, node_id).await? {
        k3s_commands::delete_token(&token.token_id)
            .await
            .map_err(|e| crate::error::ApiError::Internal {
                message: format!("Failed to delete K3s token: {}", e),
            })?;
        db::delete_cluster_token(pool, user_id, node_id).await?;
        counter!("node_tokens_revoked_total").increment(1);
        info!(
            user_id = %user_id,
            node_id = %node_id,
            "Revoked node token"
        );
    } else {
        debug!(
            user_id = %user_id,
            node_id = %node_id,
            "No token to revoke"
        );
    }

    Ok(())
}

pub async fn cleanup_expired_cluster_tokens(pool: &PgPool) -> Result<usize> {
    debug!("Starting cleanup of expired tokens");

    let expired = db::list_expired_cluster_tokens(pool).await?;

    let mut cleaned = 0;
    for token in expired {
        if let Err(e) = k3s_commands::delete_token(&token.token_id).await {
            warn!(
                token_id = %token.token_id,
                user_id = %token.user_id,
                node_id = %token.node_id,
                error = %e,
                "Failed to delete k3s token during cleanup"
            );
        }

        if db::delete_cluster_token(pool, &token.user_id, &token.node_id).await? {
            cleaned += 1;
        }
    }

    counter!("node_tokens_expired_total").increment(cleaned as u64);
    info!(cleaned_count = %cleaned, "Cleaned up expired tokens");
    Ok(cleaned)
}

pub async fn check_k3s_connectivity() -> Result<()> {
    k3s_commands::check_connectivity()
        .await
        .map_err(|e| crate::error::ApiError::Internal {
            message: format!("K3s connectivity check failed: {}", e),
        })
}
