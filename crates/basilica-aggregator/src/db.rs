use crate::error::Result;
use crate::models::{GpuOffering, Provider, ProviderHealth};
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
                (id, provider, gpu_type, gpu_memory_gb, gpu_count, system_memory_gb, vcpu_count, region,
                 hourly_rate, spot_rate, availability, raw_metadata, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    hourly_rate = excluded.hourly_rate,
                    spot_rate = excluded.spot_rate,
                    availability = excluded.availability,
                    raw_metadata = excluded.raw_metadata,
                    fetched_at = excluded.fetched_at
                "#,
            )
            .bind(&offering.id)
            .bind(offering.provider.as_str())
            .bind(offering.gpu_type.as_str())
            .bind(offering.gpu_memory_gb as i64)
            .bind(offering.gpu_count as i64)
            .bind(offering.system_memory_gb as i64)
            .bind(offering.vcpu_count as i64)
            .bind(&offering.region)
            .bind(offering.hourly_rate.to_string())
            .bind(offering.spot_rate.map(|r| r.to_string()))
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
                "SELECT id, provider, gpu_type, gpu_memory_gb, gpu_count, system_memory_gb, vcpu_count, region,
                        hourly_rate, spot_rate, availability, raw_metadata, fetched_at
                 FROM gpu_offerings WHERE provider = ? ORDER BY fetched_at DESC",
            )
            .bind(p.as_str())
        } else {
            sqlx::query(
                "SELECT id, provider, gpu_type, gpu_memory_gb, gpu_count, system_memory_gb, vcpu_count, region,
                        hourly_rate, spot_rate, availability, raw_metadata, fetched_at
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
                let spot_rate_str: Option<String> = row.get("spot_rate");
                let raw_metadata_str: String = row.get("raw_metadata");

                Some(GpuOffering {
                    id: row.get("id"),
                    provider: provider_str.parse().ok()?,
                    gpu_type: gpu_type_str.parse().ok()?,
                    gpu_memory_gb: row.get::<i64, _>("gpu_memory_gb") as u32,
                    gpu_count: row.get::<i64, _>("gpu_count") as u32,
                    system_memory_gb: row.get::<i64, _>("system_memory_gb") as u32,
                    vcpu_count: row.get::<i64, _>("vcpu_count") as u32,
                    region: row.get("region"),
                    hourly_rate: hourly_rate_str.parse().ok()?,
                    spot_rate: spot_rate_str.and_then(|s| s.parse().ok()),
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
}
