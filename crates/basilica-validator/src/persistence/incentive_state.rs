use anyhow::Result;
use sqlx::Row;
use sqlx::SqlitePool;
use tracing::warn;

use crate::persistence::SimplePersistence;

#[derive(Clone)]
pub struct IncentiveStateRepository {
    pool: SqlitePool,
}

impl IncentiveStateRepository {
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingSlashEvent {
    pub rental_id: String,
    pub node_id: String,
    pub reason: String,
    pub detected_at_ms: i64,
}

#[derive(Debug, Clone)]
pub struct SlashEventRequest {
    pub rental_id: String,
    pub node_id: String,
    pub reason: String,
    pub detected_at_ms: i64,
}

impl IncentiveStateRepository {
    pub async fn load_cu_progress(&self) -> Result<Option<i64>> {
        sqlx::query_scalar(
            "SELECT last_completed_hour_end_ms
             FROM incentive_cu_generator_progress
             WHERE id = 1",
        )
        .fetch_optional(&self.pool)
        .await
        .map_err(Into::into)
    }

    pub async fn save_cu_progress(&self, last_completed_hour_end_ms: i64) -> Result<()> {
        sqlx::query(
            "INSERT INTO incentive_cu_generator_progress (id, last_completed_hour_end_ms, updated_at)
             VALUES (1, ?, CURRENT_TIMESTAMP)
             ON CONFLICT(id) DO UPDATE SET
               last_completed_hour_end_ms = excluded.last_completed_hour_end_ms,
               updated_at = CURRENT_TIMESTAMP",
        )
        .bind(last_completed_hour_end_ms)
        .execute(&self.pool)
        .await?;

        Ok(())
    }

    pub async fn earliest_availability_effective_at_ms(&self) -> Result<Option<i64>> {
        sqlx::query_scalar("SELECT MIN(row_effective_at) FROM availability_log")
            .fetch_one(&self.pool)
            .await
            .map_err(Into::into)
    }

    pub async fn earliest_unprocessed_slash_event_at_ms(&self) -> Result<Option<i64>> {
        sqlx::query_scalar(
            "SELECT MIN(detected_at_ms)
             FROM incentive_slash_events
             WHERE processed_at_ms IS NULL",
        )
        .fetch_one(&self.pool)
        .await
        .map_err(Into::into)
    }

    pub async fn record_slash_event(&self, event: SlashEventRequest) -> Result<bool> {
        let result = sqlx::query(
            "INSERT INTO incentive_slash_events (
                rental_id, node_id, reason, detected_at_ms
             ) VALUES (?, ?, ?, ?)
             ON CONFLICT(rental_id) DO NOTHING",
        )
        .bind(&event.rental_id)
        .bind(&event.node_id)
        .bind(&event.reason)
        .bind(event.detected_at_ms)
        .execute(&self.pool)
        .await?;

        Ok(result.rows_affected() > 0)
    }

    pub async fn list_unprocessed_slash_events(
        &self,
        up_to_ms: i64,
    ) -> Result<Vec<PendingSlashEvent>> {
        let rows = sqlx::query(
            "SELECT rental_id, node_id, reason, detected_at_ms
             FROM incentive_slash_events
             WHERE processed_at_ms IS NULL
               AND detected_at_ms < ?
             ORDER BY detected_at_ms ASC, rental_id ASC",
        )
        .bind(up_to_ms)
        .fetch_all(&self.pool)
        .await?;

        Ok(rows
            .into_iter()
            .map(|row| PendingSlashEvent {
                rental_id: row.get("rental_id"),
                node_id: row.get("node_id"),
                reason: row.get("reason"),
                detected_at_ms: row.get("detected_at_ms"),
            })
            .collect())
    }

    pub async fn mark_slash_event_processed(
        &self,
        rental_id: &str,
        processed_at_ms: i64,
    ) -> Result<()> {
        sqlx::query(
            "UPDATE incentive_slash_events
             SET processed_at_ms = ?
             WHERE rental_id = ?",
        )
        .bind(processed_at_ms)
        .bind(rental_id)
        .execute(&self.pool)
        .await?;

        Ok(())
    }
}

impl SimplePersistence {
    pub fn record_incentive_slash_event_best_effort(&self, event: SlashEventRequest) {
        let pool = self.pool().clone();
        tokio::spawn(async move {
            let repo = IncentiveStateRepository::new(pool);
            if let Err(error) = repo.record_slash_event(event).await {
                warn!(error = %error, "Failed to persist incentive slash event");
            }
        });
    }
}
