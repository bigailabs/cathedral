use crate::error::Result;
use crate::models::{
    Deployment, DeploymentStatus, GpuOffering, Provider, ProviderHealth, ProviderSshKey, SshKey,
};
use chrono::{DateTime, Utc};
use sqlx::sqlite::{SqliteConnectOptions, SqlitePool, SqlitePoolOptions};
use sqlx::Row;
use std::str::FromStr;

pub struct Database {
    pool: SqlitePool,
}

impl Database {
    /// Create new database connection
    pub async fn new(database_path: &str) -> Result<Self> {
        let options = SqliteConnectOptions::from_str(database_path)?
            .create_if_missing(true)
            .foreign_keys(true);

        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect_with(options)
            .await?;

        // Run migrations
        sqlx::migrate!()
            .run(&pool)
            .await
            .map_err(|e| sqlx::Error::Protocol(format!("Migration failed: {}", e)))?;

        Ok(Self { pool })
    }

    /// Insert or update GPU offerings
    pub async fn upsert_offerings(&self, offerings: &[GpuOffering]) -> Result<()> {
        let mut tx = self.pool.begin().await?;

        for offering in offerings {
            sqlx::query(
                r#"
                INSERT INTO gpu_offerings
                (id, provider, gpu_type, gpu_memory_gb_per_gpu, gpu_count, interconnect, storage, deployment_type,
                 system_memory_gb, vcpu_count, region, hourly_rate, availability, raw_metadata, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    interconnect = excluded.interconnect,
                    storage = excluded.storage,
                    deployment_type = excluded.deployment_type,
                    hourly_rate = excluded.hourly_rate,
                    availability = excluded.availability,
                    raw_metadata = excluded.raw_metadata,
                    fetched_at = excluded.fetched_at
                "#,
            )
            .bind(&offering.id)
            .bind(offering.provider.as_str())
            .bind(offering.gpu_type.as_str())
            .bind(offering.gpu_memory_gb_per_gpu.map(|m| m as i64))
            .bind(offering.gpu_count as i64)
            .bind(offering.interconnect.as_ref())
            .bind(offering.storage.as_ref())
            .bind(offering.deployment_type.as_ref())
            .bind(offering.system_memory_gb as i64)
            .bind(offering.vcpu_count as i64)
            .bind(&offering.region)
            .bind(offering.hourly_rate.to_string())
            .bind(offering.availability)
            .bind(offering.raw_metadata.to_string())
            .bind(offering.fetched_at)
            .execute(&mut *tx)
            .await?;
        }

        tx.commit().await?;
        Ok(())
    }

    /// Get all offerings for a provider
    pub async fn get_offerings(&self, provider: Option<Provider>) -> Result<Vec<GpuOffering>> {
        let query = if let Some(p) = provider {
            sqlx::query(
                "SELECT id, provider, gpu_type, gpu_memory_gb_per_gpu, gpu_count, interconnect, storage, deployment_type,
                        system_memory_gb, vcpu_count, region, hourly_rate, availability, raw_metadata, fetched_at
                 FROM gpu_offerings WHERE provider = ? ORDER BY fetched_at DESC",
            )
            .bind(p.as_str())
        } else {
            sqlx::query(
                "SELECT id, provider, gpu_type, gpu_memory_gb_per_gpu, gpu_count, interconnect, storage, deployment_type,
                        system_memory_gb, vcpu_count, region, hourly_rate, availability, raw_metadata, fetched_at
                 FROM gpu_offerings ORDER BY fetched_at DESC",
            )
        };

        let rows = query.fetch_all(&self.pool).await?;

        let offerings = rows
            .into_iter()
            .filter_map(|row| {
                let provider_str: String = row.get("provider");
                let gpu_type_str: String = row.get("gpu_type");
                let hourly_rate_str: String = row.get("hourly_rate");
                let raw_metadata_str: String = row.get("raw_metadata");

                Some(GpuOffering {
                    id: row.get("id"),
                    provider: provider_str.parse().ok()?,
                    gpu_type: gpu_type_str.parse().ok()?,
                    gpu_memory_gb_per_gpu: row
                        .get::<Option<i64>, _>("gpu_memory_gb_per_gpu")
                        .map(|m| m as u32),
                    gpu_count: row.get::<i64, _>("gpu_count") as u32,
                    interconnect: row.get("interconnect"),
                    storage: row.get("storage"),
                    deployment_type: row.get("deployment_type"),
                    system_memory_gb: row.get::<i64, _>("system_memory_gb") as u32,
                    vcpu_count: row.get::<i64, _>("vcpu_count") as u32,
                    region: row.get("region"),
                    hourly_rate: hourly_rate_str.parse().ok()?,
                    availability: row.get("availability"),
                    raw_metadata: serde_json::from_str(&raw_metadata_str).ok()?,
                    fetched_at: row.get("fetched_at"),
                })
            })
            .collect();

        Ok(offerings)
    }

    /// Update provider status
    pub async fn update_provider_status(
        &self,
        provider: Provider,
        success: bool,
        error_msg: Option<String>,
    ) -> Result<()> {
        let now = Utc::now();

        if success {
            sqlx::query(
                r#"
                INSERT INTO provider_status (provider, last_fetch_at, last_success_at, is_healthy, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    last_fetch_at = excluded.last_fetch_at,
                    last_success_at = excluded.last_success_at,
                    is_healthy = 1,
                    last_error = NULL,
                    updated_at = excluded.updated_at
                "#,
            )
            .bind(provider.as_str())
            .bind(now)
            .bind(now)
            .bind(now)
            .execute(&self.pool)
            .await?;
        } else {
            sqlx::query(
                r#"
                INSERT INTO provider_status (provider, last_fetch_at, is_healthy, last_error, updated_at)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    last_fetch_at = excluded.last_fetch_at,
                    is_healthy = 0,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                "#,
            )
            .bind(provider.as_str())
            .bind(now)
            .bind(error_msg)
            .bind(now)
            .execute(&self.pool)
            .await?;
        }

        Ok(())
    }

    /// Get provider health status
    pub async fn get_provider_health(&self, provider: Provider) -> Result<ProviderHealth> {
        let row = sqlx::query(
            "SELECT last_success_at, last_error, is_healthy FROM provider_status WHERE provider = ?",
        )
        .bind(provider.as_str())
        .fetch_optional(&self.pool)
        .await?;

        if let Some(row) = row {
            Ok(ProviderHealth {
                provider,
                is_healthy: row.get("is_healthy"),
                last_success_at: row.get("last_success_at"),
                last_error: row.get("last_error"),
            })
        } else {
            Ok(ProviderHealth {
                provider,
                is_healthy: false,
                last_success_at: None,
                last_error: Some("Never fetched".to_string()),
            })
        }
    }

    /// Get last fetch time for provider
    pub async fn get_last_fetch_time(&self, provider: Provider) -> Result<Option<DateTime<Utc>>> {
        let row = sqlx::query("SELECT last_fetch_at FROM provider_status WHERE provider = ?")
            .bind(provider.as_str())
            .fetch_optional(&self.pool)
            .await?;

        Ok(row.and_then(|r| r.get("last_fetch_at")))
    }

    // ========================================================================
    // Deployment Management
    // ========================================================================

    /// Create a new deployment record
    pub async fn create_deployment(&self, deployment: &Deployment) -> Result<()> {
        let connection_info = deployment.connection_info.as_ref().map(|v| v.to_string());
        let raw_response = deployment.raw_response.as_ref().map(|v| v.to_string());

        sqlx::query(
            r#"
            INSERT INTO deployments
            (id, user_id, provider, provider_instance_id, offering_id, instance_type, location_code,
             status, hostname, ssh_key_id, ip_address, connection_info, raw_response,
             error_message, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            "#,
        )
        .bind(&deployment.id)
        .bind(&deployment.user_id)
        .bind(deployment.provider.as_str())
        .bind(&deployment.provider_instance_id)
        .bind(&deployment.offering_id)
        .bind(&deployment.instance_type)
        .bind(&deployment.location_code)
        .bind(deployment.status.as_str())
        .bind(&deployment.hostname)
        .bind(&deployment.ssh_key_id)
        .bind(&deployment.ip_address)
        .bind(connection_info)
        .bind(raw_response)
        .bind(&deployment.error_message)
        .bind(deployment.created_at)
        .bind(deployment.updated_at)
        .execute(&self.pool)
        .await?;

        Ok(())
    }

    /// Update deployment status and details
    #[allow(clippy::too_many_arguments)]
    pub async fn update_deployment(
        &self,
        id: &str,
        provider_instance_id: Option<String>,
        status: DeploymentStatus,
        ip_address: Option<String>,
        connection_info: Option<serde_json::Value>,
        raw_response: Option<serde_json::Value>,
        error_message: Option<String>,
    ) -> Result<()> {
        let connection_info_str = connection_info.as_ref().map(|v| v.to_string());
        let raw_response_str = raw_response.as_ref().map(|v| v.to_string());
        let now = Utc::now();

        sqlx::query(
            r#"
            UPDATE deployments
            SET provider_instance_id = ?,
                status = ?,
                ip_address = ?,
                connection_info = ?,
                raw_response = ?,
                error_message = ?,
                updated_at = ?
            WHERE id = ?
            "#,
        )
        .bind(provider_instance_id)
        .bind(status.as_str())
        .bind(ip_address)
        .bind(connection_info_str)
        .bind(raw_response_str)
        .bind(error_message)
        .bind(now)
        .bind(id)
        .execute(&self.pool)
        .await?;

        Ok(())
    }

    /// Get deployment by ID
    pub async fn get_deployment(&self, id: &str) -> Result<Option<Deployment>> {
        let row = sqlx::query(
            r#"
            SELECT id, user_id, provider, provider_instance_id, offering_id, instance_type, location_code,
                   status, hostname, ssh_key_id, ip_address, connection_info, raw_response,
                   error_message, created_at, updated_at
            FROM deployments
            WHERE id = ?
            "#,
        )
        .bind(id)
        .fetch_optional(&self.pool)
        .await?;

        let deployment = row.and_then(|r| {
            let provider_str: String = r.get("provider");
            let status_str: String = r.get("status");
            let connection_info_str: Option<String> = r.get("connection_info");
            let raw_response_str: Option<String> = r.get("raw_response");

            Some(Deployment {
                id: r.get("id"),
                user_id: r.get("user_id"),
                provider: provider_str.parse().ok()?,
                provider_instance_id: r.get("provider_instance_id"),
                offering_id: r.get("offering_id"),
                instance_type: r.get("instance_type"),
                location_code: r.get("location_code"),
                status: status_str.parse().ok()?,
                hostname: r.get("hostname"),
                ssh_key_id: r.get("ssh_key_id"),
                ip_address: r.get("ip_address"),
                connection_info: connection_info_str.and_then(|s| serde_json::from_str(&s).ok()),
                raw_response: raw_response_str.and_then(|s| serde_json::from_str(&s).ok()),
                error_message: r.get("error_message"),
                created_at: r.get("created_at"),
                updated_at: r.get("updated_at"),
            })
        });

        Ok(deployment)
    }

    /// List all deployments with optional filters
    pub async fn list_deployments(
        &self,
        provider: Option<Provider>,
        status: Option<DeploymentStatus>,
    ) -> Result<Vec<Deployment>> {
        let mut query = String::from(
            r#"
            SELECT id, user_id, provider, provider_instance_id, offering_id, instance_type, location_code,
                   status, hostname, ssh_key_id, ip_address, connection_info, raw_response,
                   error_message, created_at, updated_at
            FROM deployments
            WHERE 1=1
            "#,
        );

        let mut conditions = Vec::new();

        if let Some(p) = provider {
            conditions.push(format!("provider = '{}'", p.as_str()));
        }

        if let Some(s) = status {
            conditions.push(format!("status = '{}'", s.as_str()));
        }

        if !conditions.is_empty() {
            query.push_str(" AND ");
            query.push_str(&conditions.join(" AND "));
        }

        query.push_str(" ORDER BY created_at DESC");

        let rows = sqlx::query(&query).fetch_all(&self.pool).await?;

        let deployments = rows
            .into_iter()
            .filter_map(|r| {
                let provider_str: String = r.get("provider");
                let status_str: String = r.get("status");
                let connection_info_str: Option<String> = r.get("connection_info");
                let raw_response_str: Option<String> = r.get("raw_response");

                Some(Deployment {
                    id: r.get("id"),
                    user_id: r.get("user_id"),
                    provider: provider_str.parse().ok()?,
                    provider_instance_id: r.get("provider_instance_id"),
                    offering_id: r.get("offering_id"),
                    instance_type: r.get("instance_type"),
                    location_code: r.get("location_code"),
                    status: status_str.parse().ok()?,
                    hostname: r.get("hostname"),
                    ssh_key_id: r.get("ssh_key_id"),
                    ip_address: r.get("ip_address"),
                    connection_info: connection_info_str
                        .and_then(|s| serde_json::from_str(&s).ok()),
                    raw_response: raw_response_str.and_then(|s| serde_json::from_str(&s).ok()),
                    error_message: r.get("error_message"),
                    created_at: r.get("created_at"),
                    updated_at: r.get("updated_at"),
                })
            })
            .collect();

        Ok(deployments)
    }

    /// Delete deployment record
    pub async fn delete_deployment(&self, id: &str) -> Result<()> {
        sqlx::query("DELETE FROM deployments WHERE id = ?")
            .bind(id)
            .execute(&self.pool)
            .await?;

        Ok(())
    }

    // ========================================================================
    // SSH Key Management
    // ========================================================================

    /// Create a new SSH key
    pub async fn create_ssh_key(&self, ssh_key: &SshKey) -> Result<()> {
        sqlx::query(
            r#"
            INSERT INTO ssh_keys (id, user_id, name, public_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            "#,
        )
        .bind(&ssh_key.id)
        .bind(&ssh_key.user_id)
        .bind(&ssh_key.name)
        .bind(&ssh_key.public_key)
        .bind(ssh_key.created_at)
        .bind(ssh_key.updated_at)
        .execute(&self.pool)
        .await?;

        Ok(())
    }

    /// Get SSH key by user ID
    pub async fn get_ssh_key_by_user(&self, user_id: &str) -> Result<Option<SshKey>> {
        let row = sqlx::query(
            "SELECT id, user_id, name, public_key, created_at, updated_at
             FROM ssh_keys WHERE user_id = ?",
        )
        .bind(user_id)
        .fetch_optional(&self.pool)
        .await?;

        Ok(row.map(|r| SshKey {
            id: r.get("id"),
            user_id: r.get("user_id"),
            name: r.get("name"),
            public_key: r.get("public_key"),
            created_at: r.get("created_at"),
            updated_at: r.get("updated_at"),
        }))
    }

    /// Get SSH key by ID
    pub async fn get_ssh_key_by_id(&self, id: &str) -> Result<Option<SshKey>> {
        let row = sqlx::query(
            "SELECT id, user_id, name, public_key, created_at, updated_at
             FROM ssh_keys WHERE id = ?",
        )
        .bind(id)
        .fetch_optional(&self.pool)
        .await?;

        Ok(row.map(|r| SshKey {
            id: r.get("id"),
            user_id: r.get("user_id"),
            name: r.get("name"),
            public_key: r.get("public_key"),
            created_at: r.get("created_at"),
            updated_at: r.get("updated_at"),
        }))
    }

    /// Update SSH key
    pub async fn update_ssh_key(&self, ssh_key: &SshKey) -> Result<()> {
        sqlx::query(
            r#"
            UPDATE ssh_keys
            SET name = ?, public_key = ?, updated_at = ?
            WHERE id = ?
            "#,
        )
        .bind(&ssh_key.name)
        .bind(&ssh_key.public_key)
        .bind(ssh_key.updated_at)
        .bind(&ssh_key.id)
        .execute(&self.pool)
        .await?;

        Ok(())
    }

    /// Delete SSH key (cascades to provider_ssh_keys)
    pub async fn delete_ssh_key(&self, id: &str) -> Result<()> {
        sqlx::query("DELETE FROM ssh_keys WHERE id = ?")
            .bind(id)
            .execute(&self.pool)
            .await?;

        Ok(())
    }

    // ========================================================================
    // Provider SSH Key Mappings
    // ========================================================================

    /// Get provider SSH key mapping
    pub async fn get_provider_ssh_key(
        &self,
        ssh_key_id: &str,
        provider: Provider,
    ) -> Result<Option<ProviderSshKey>> {
        let row = sqlx::query(
            "SELECT id, ssh_key_id, provider, provider_key_id, created_at
             FROM provider_ssh_keys WHERE ssh_key_id = ? AND provider = ?",
        )
        .bind(ssh_key_id)
        .bind(provider.as_str())
        .fetch_optional(&self.pool)
        .await?;

        Ok(row.and_then(|r| {
            let provider_str: String = r.get("provider");
            Provider::from_str(&provider_str)
                .ok()
                .map(|p| ProviderSshKey {
                    id: r.get("id"),
                    ssh_key_id: r.get("ssh_key_id"),
                    provider: p,
                    provider_key_id: r.get("provider_key_id"),
                    created_at: r.get("created_at"),
                })
        }))
    }

    /// Create provider SSH key mapping
    pub async fn create_provider_ssh_key(&self, mapping: &ProviderSshKey) -> Result<()> {
        sqlx::query(
            r#"
            INSERT INTO provider_ssh_keys (id, ssh_key_id, provider, provider_key_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            "#,
        )
        .bind(&mapping.id)
        .bind(&mapping.ssh_key_id)
        .bind(mapping.provider.as_str())
        .bind(&mapping.provider_key_id)
        .bind(mapping.created_at)
        .execute(&self.pool)
        .await?;

        Ok(())
    }

    /// List all provider SSH key mappings for a given SSH key
    pub async fn list_provider_ssh_keys_for_key(
        &self,
        ssh_key_id: &str,
    ) -> Result<Vec<ProviderSshKey>> {
        let rows = sqlx::query(
            "SELECT id, ssh_key_id, provider, provider_key_id, created_at
             FROM provider_ssh_keys WHERE ssh_key_id = ?",
        )
        .bind(ssh_key_id)
        .fetch_all(&self.pool)
        .await?;

        let mappings = rows
            .into_iter()
            .filter_map(|r| {
                let provider_str: String = r.get("provider");
                Provider::from_str(&provider_str)
                    .ok()
                    .map(|p| ProviderSshKey {
                        id: r.get("id"),
                        ssh_key_id: r.get("ssh_key_id"),
                        provider: p,
                        provider_key_id: r.get("provider_key_id"),
                        created_at: r.get("created_at"),
                    })
            })
            .collect();

        Ok(mappings)
    }
}
