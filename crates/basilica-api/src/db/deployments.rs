use crate::error::{ApiError, Result};
use sqlx::PgPool;

#[derive(Debug, Clone)]
pub struct CreateDeploymentParams<'a> {
    pub user_id: &'a str,
    pub instance_name: &'a str,
    pub namespace: &'a str,
    pub cr_name: &'a str,
    pub image: &'a str,
    pub replicas: i32,
    pub port: i32,
    pub path_prefix: &'a str,
    pub public_url: &'a str,
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct DeploymentRecord {
    pub id: i32,
    pub user_id: String,
    pub instance_name: String,
    pub namespace: String,
    pub cr_name: String,
    pub image: String,
    pub replicas: i32,
    pub port: i32,
    pub path_prefix: String,
    pub public_url: String,
    pub state: String,
    pub message: Option<String>,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub updated_at: chrono::DateTime<chrono::Utc>,
    pub deleted_at: Option<chrono::DateTime<chrono::Utc>>,
}

pub async fn get_deployment(
    pool: &PgPool,
    user_id: &str,
    instance_name: &str,
) -> Result<Option<DeploymentRecord>> {
    let record = sqlx::query_as::<_, DeploymentRecord>(
        r#"
        SELECT id, user_id, instance_name, namespace, cr_name, image, replicas, port,
               path_prefix, public_url, state, message, created_at, updated_at, deleted_at
        FROM user_deployments
        WHERE user_id = $1 AND instance_name = $2 AND deleted_at IS NULL
        "#,
    )
    .bind(user_id)
    .bind(instance_name)
    .fetch_optional(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Database query failed: {e}"),
    })?;

    Ok(record)
}

pub async fn create_deployment(
    pool: &PgPool,
    params: CreateDeploymentParams<'_>,
) -> Result<DeploymentRecord> {
    let record = sqlx::query_as::<_, DeploymentRecord>(
        r#"
        INSERT INTO user_deployments
        (user_id, instance_name, namespace, cr_name, image, replicas, port, path_prefix, public_url, state)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'Pending')
        RETURNING id, user_id, instance_name, namespace, cr_name, image, replicas, port,
                  path_prefix, public_url, state, message, created_at, updated_at, deleted_at
        "#,
    )
    .bind(params.user_id)
    .bind(params.instance_name)
    .bind(params.namespace)
    .bind(params.cr_name)
    .bind(params.image)
    .bind(params.replicas)
    .bind(params.port)
    .bind(params.path_prefix)
    .bind(params.public_url)
    .fetch_one(pool)
    .await
    .map_err(|e| {
        if let Some(db_err) = e.as_database_error() {
            if db_err.is_unique_violation() {
                return ApiError::Conflict {
                    message: format!(
                        "Deployment with instance_name '{}' already exists",
                        params.instance_name
                    ),
                };
            }
        }
        ApiError::Internal {
            message: format!("Failed to create deployment: {e}"),
        }
    })?;

    Ok(record)
}

pub async fn update_deployment_state(
    pool: &PgPool,
    id: i32,
    state: &str,
    message: Option<&str>,
) -> Result<()> {
    sqlx::query(
        r#"
        UPDATE user_deployments
        SET state = $1, message = $2
        WHERE id = $3
        "#,
    )
    .bind(state)
    .bind(message)
    .bind(id)
    .execute(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to update deployment state: {e}"),
    })?;

    Ok(())
}

pub async fn mark_deployment_deleted(pool: &PgPool, id: i32) -> Result<()> {
    sqlx::query(
        r#"
        UPDATE user_deployments
        SET deleted_at = NOW(), state = 'Deleted'
        WHERE id = $1
        "#,
    )
    .bind(id)
    .execute(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Failed to mark deployment as deleted: {e}"),
    })?;

    Ok(())
}

pub async fn list_user_deployments(pool: &PgPool, user_id: &str) -> Result<Vec<DeploymentRecord>> {
    let records = sqlx::query_as::<_, DeploymentRecord>(
        r#"
        SELECT id, user_id, instance_name, namespace, cr_name, image, replicas, port,
               path_prefix, public_url, state, message, created_at, updated_at, deleted_at
        FROM user_deployments
        WHERE user_id = $1 AND deleted_at IS NULL
        ORDER BY created_at DESC
        "#,
    )
    .bind(user_id)
    .fetch_all(pool)
    .await
    .map_err(|e| ApiError::Internal {
        message: format!("Database query failed: {e}"),
    })?;

    Ok(records)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_deployment_record() -> DeploymentRecord {
        DeploymentRecord {
            id: 1,
            user_id: "test_user".to_string(),
            instance_name: "test-app".to_string(),
            namespace: "u-test-user".to_string(),
            cr_name: "test-app-deployment".to_string(),
            image: "nginx:latest".to_string(),
            replicas: 2,
            port: 80,
            path_prefix: "/deployments/test-app".to_string(),
            public_url: "http://3.21.154.119:8080/deployments/test-app/".to_string(),
            state: "Active".to_string(),
            message: None,
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            deleted_at: None,
        }
    }

    #[test]
    fn test_deployment_record_structure() {
        let record = test_deployment_record();
        assert_eq!(record.user_id, "test_user");
        assert_eq!(record.instance_name, "test-app");
        assert_eq!(record.state, "Active");
        assert!(record.deleted_at.is_none());
    }
}
