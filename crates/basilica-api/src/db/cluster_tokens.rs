use crate::error::{ApiError, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::{FromRow, PgPool};

#[derive(Debug, Clone, FromRow, Serialize, Deserialize)]
pub struct ClusterTokenRecord {
    pub user_id: String,
    pub node_id: String,
    pub token_id: String,
    pub token_secret: String,
    pub created_at: DateTime<Utc>,
    pub expires_at: DateTime<Utc>,
}

impl ClusterTokenRecord {
    pub fn is_expired(&self) -> bool {
        Utc::now() >= self.expires_at
    }

    pub fn full_token(&self) -> String {
        self.token_secret.clone()
    }
}

pub async fn get_cluster_token(
    pool: &PgPool,
    user_id: &str,
    node_id: &str,
) -> Result<Option<ClusterTokenRecord>> {
    let token = sqlx::query_as::<_, ClusterTokenRecord>(
        r#"
        SELECT * FROM node_cluster_tokens
        WHERE user_id = $1 AND node_id = $2
        "#,
    )
    .bind(user_id)
    .bind(node_id)
    .fetch_optional(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to get cluster token: {}", e),
    })?;

    Ok(token)
}

pub async fn insert_cluster_token(
    pool: &PgPool,
    user_id: &str,
    node_id: &str,
    token_id: &str,
    token_secret: &str,
    expires_at: DateTime<Utc>,
) -> Result<ClusterTokenRecord> {
    let token = sqlx::query_as::<_, ClusterTokenRecord>(
        r#"
        INSERT INTO node_cluster_tokens (user_id, node_id, token_id, token_secret, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (user_id, node_id)
        DO UPDATE SET
            token_id = EXCLUDED.token_id,
            token_secret = EXCLUDED.token_secret,
            created_at = NOW(),
            expires_at = EXCLUDED.expires_at
        RETURNING *
        "#,
    )
    .bind(user_id)
    .bind(node_id)
    .bind(token_id)
    .bind(token_secret)
    .bind(expires_at)
    .fetch_one(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to insert cluster token: {}", e),
    })?;

    Ok(token)
}

pub async fn delete_cluster_token(pool: &PgPool, user_id: &str, node_id: &str) -> Result<bool> {
    let result = sqlx::query(
        r#"
        DELETE FROM node_cluster_tokens
        WHERE user_id = $1 AND node_id = $2
        "#,
    )
    .bind(user_id)
    .bind(node_id)
    .execute(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to delete cluster token: {}", e),
    })?;

    Ok(result.rows_affected() > 0)
}

pub async fn list_expired_cluster_tokens(pool: &PgPool) -> Result<Vec<ClusterTokenRecord>> {
    let tokens = sqlx::query_as::<_, ClusterTokenRecord>(
        r#"
        SELECT * FROM node_cluster_tokens
        WHERE expires_at < NOW()
        "#,
    )
    .fetch_all(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to list expired cluster tokens: {}", e),
    })?;

    Ok(tokens)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    #[test]
    fn test_cluster_token_is_expired() {
        let expired_token = ClusterTokenRecord {
            user_id: "user1".to_string(),
            node_id: "node1".to_string(),
            token_id: "abc123".to_string(),
            token_secret: "secret".to_string(),
            created_at: Utc::now() - Duration::hours(2),
            expires_at: Utc::now() - Duration::hours(1),
        };
        assert!(expired_token.is_expired());

        let valid_token = ClusterTokenRecord {
            user_id: "user1".to_string(),
            node_id: "node1".to_string(),
            token_id: "abc123".to_string(),
            token_secret: "secret".to_string(),
            created_at: Utc::now(),
            expires_at: Utc::now() + Duration::hours(1),
        };
        assert!(!valid_token.is_expired());
    }

    #[test]
    fn test_cluster_token_full_token() {
        let token = ClusterTokenRecord {
            user_id: "user1".to_string(),
            node_id: "node1".to_string(),
            token_id: "abc123".to_string(),
            token_secret: "K10abc123::server:ca-hash:secret".to_string(),
            created_at: Utc::now(),
            expires_at: Utc::now() + Duration::hours(1),
        };
        assert_eq!(token.full_token(), "K10abc123::server:ca-hash:secret");
    }
}
