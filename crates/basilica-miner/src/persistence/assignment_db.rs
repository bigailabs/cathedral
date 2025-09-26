//! # Assignment Database
//!
//! Database operations for manual node assignments

use anyhow::Result;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::types::chrono;
use sqlx::SqlitePool;
use tracing::info;

/// Validator stake information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ValidatorStake {
    pub validator_hotkey: String,
    pub stake_amount: f64,
    pub percentage_of_total: f64,
    pub last_updated: DateTime<Utc>,
}

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

        // Create node_assignments table
        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS node_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL UNIQUE,
                validator_hotkey TEXT NOT NULL,
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                assigned_by TEXT NOT NULL,
                notes TEXT
            )
            "#,
        )
        .execute(&self.pool)
        .await?;

        // Create validator_stakes table
        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS validator_stakes (
                validator_hotkey TEXT PRIMARY KEY,
                stake_amount REAL NOT NULL,
                percentage_of_total REAL NOT NULL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            "#,
        )
        .execute(&self.pool)
        .await?;

        // Create assignment_history table
        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS assignment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                validator_hotkey TEXT,
                action TEXT NOT NULL,
                performed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                performed_by TEXT NOT NULL
            )
            "#,
        )
        .execute(&self.pool)
        .await?;

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
