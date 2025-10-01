//! # Assignment Database
//!
//! Database operations for manual node assignments

use anyhow::Result;
use chrono::Utc;
use sqlx::types::chrono;
use sqlx::SqlitePool;
use tracing::info;

/// Assignment database operations
pub struct AssignmentDb {
    pool: SqlitePool,
}

impl AssignmentDb {
    /// Create a new assignment database
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    /// Run database migrations
    pub async fn run_migrations(&self) -> Result<()> {
        info!("Running assignment database migrations");

        sqlx::migrate!("./migrations")
            .run(&self.pool)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to run migrations: {}", e))?;

        info!("Assignment database migrations completed");
        Ok(())
    }

    /// Update validator stakes in batch
    pub async fn update_validator_stakes_batch(
        &self,
        stakes: &[(String, f64, f64)], // (hotkey, stake_amount, percentage)
    ) -> Result<()> {
        let now = Utc::now();

        for (hotkey, stake_amount, percentage) in stakes {
            sqlx::query(
                r#"
                INSERT OR REPLACE INTO validator_stakes (validator_hotkey, stake_amount, percentage_of_total, last_updated)
                VALUES (?, ?, ?, ?)
                "#,
            )
            .bind(hotkey)
            .bind(stake_amount)
            .bind(percentage)
            .bind(now)
            .execute(&self.pool)
            .await?;
        }

        Ok(())
    }
}
