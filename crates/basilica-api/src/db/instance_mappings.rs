use crate::error::{ApiError, Result};
use sqlx::PgPool;
use uuid::Uuid;

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct InstanceMapping {
    pub user_id: String,
    pub instance_name: String,
    pub instance_id: String,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

/// Get or create a stable instance_id for a (user_id, instance_name) pair.
/// If the mapping exists, returns the existing instance_id.
/// If not, creates a new UUID and stores the mapping.
pub async fn get_or_create_instance_id(
    pool: &PgPool,
    user_id: &str,
    instance_name: &str,
) -> Result<String> {
    // First, try to get existing mapping
    let existing = sqlx::query_scalar::<_, String>(
        r#"
        SELECT instance_id
        FROM deployment_instance_mappings
        WHERE user_id = $1 AND instance_name = $2
        "#,
    )
    .bind(user_id)
    .bind(instance_name)
    .fetch_optional(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to query instance mapping: {e}"),
    })?;

    if let Some(instance_id) = existing {
        tracing::debug!(
            user_id = %user_id,
            instance_name = %instance_name,
            instance_id = %instance_id,
            "Found existing instance mapping"
        );
        return Ok(instance_id);
    }

    // Create new mapping with a new UUID
    let new_instance_id = Uuid::new_v4().to_string();

    sqlx::query(
        r#"
        INSERT INTO deployment_instance_mappings (user_id, instance_name, instance_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, instance_name) DO NOTHING
        "#,
    )
    .bind(user_id)
    .bind(instance_name)
    .bind(&new_instance_id)
    .execute(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to create instance mapping: {e}"),
    })?;

    // Re-fetch to handle race condition (another request may have inserted first)
    let instance_id = sqlx::query_scalar::<_, String>(
        r#"
        SELECT instance_id
        FROM deployment_instance_mappings
        WHERE user_id = $1 AND instance_name = $2
        "#,
    )
    .bind(user_id)
    .bind(instance_name)
    .fetch_one(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to fetch instance mapping after insert: {e}"),
    })?;

    tracing::info!(
        user_id = %user_id,
        instance_name = %instance_name,
        instance_id = %instance_id,
        "Created new instance mapping"
    );

    Ok(instance_id)
}

/// Get existing instance mapping if it exists
pub async fn get_instance_mapping(
    pool: &PgPool,
    user_id: &str,
    instance_name: &str,
) -> Result<Option<InstanceMapping>> {
    let record = sqlx::query_as::<_, InstanceMapping>(
        r#"
        SELECT user_id, instance_name, instance_id, created_at
        FROM deployment_instance_mappings
        WHERE user_id = $1 AND instance_name = $2
        "#,
    )
    .bind(user_id)
    .bind(instance_name)
    .fetch_optional(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to query instance mapping: {e}"),
    })?;

    Ok(record)
}

/// List all instance mappings for a user
pub async fn list_user_instance_mappings(
    pool: &PgPool,
    user_id: &str,
) -> Result<Vec<InstanceMapping>> {
    let records = sqlx::query_as::<_, InstanceMapping>(
        r#"
        SELECT user_id, instance_name, instance_id, created_at
        FROM deployment_instance_mappings
        WHERE user_id = $1
        ORDER BY created_at DESC
        "#,
    )
    .bind(user_id)
    .fetch_all(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to list instance mappings: {e}"),
    })?;

    Ok(records)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_instance_mapping_structure() {
        let mapping = InstanceMapping {
            user_id: "test_user".to_string(),
            instance_name: "my-app".to_string(),
            instance_id: "abc-123-def".to_string(),
            created_at: chrono::Utc::now(),
        };
        assert_eq!(mapping.user_id, "test_user");
        assert_eq!(mapping.instance_name, "my-app");
        assert_eq!(mapping.instance_id, "abc-123-def");
    }
}
