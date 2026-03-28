use anyhow::{anyhow, Result};
use chrono::{DateTime, Duration, Utc};
use sqlx::{Row, SqlitePool};
use tracing::{debug, warn};

use crate::persistence::SimplePersistence;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AvailabilitySource {
    Validation,
    RentalHealthFailure,
    StaleNodeCleanup,
    FailedNodeCleanup,
    RemoveBid,
}

impl AvailabilitySource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Validation => "validation",
            Self::RentalHealthFailure => "rental_health_failure",
            Self::StaleNodeCleanup => "stale_node_cleanup",
            Self::FailedNodeCleanup => "failed_node_cleanup",
            Self::RemoveBid => "remove_bid",
        }
    }
}

#[derive(Debug, Clone)]
pub struct AvailabilityEventRequest {
    pub miner_id: String,
    pub miner_uid: Option<u16>,
    pub hotkey: Option<String>,
    pub node_id: String,
    pub is_available: bool,
    pub is_rented: Option<bool>,
    pub is_validated: bool,
    pub source: AvailabilitySource,
    pub source_metadata: Option<String>,
    pub observed_at: DateTime<Utc>,
    pub gpu_category: Option<String>,
    pub gpu_count: Option<u32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AvailabilityTransition {
    Inserted,
    Transitioned,
    NoOp,
    IgnoredOutOfOrder,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AvailabilityLogRow {
    pub miner_uid: u16,
    pub hotkey: String,
    pub node_id: String,
    pub is_available: bool,
    pub is_rented: bool,
    pub is_validated: bool,
    pub source: String,
    pub source_metadata: Option<String>,
    pub gpu_category: Option<String>,
    pub gpu_count: Option<u32>,
    pub row_effective_at: i64,
    pub row_expiration_at: Option<i64>,
    pub is_current: bool,
}

#[derive(Debug, Clone)]
struct ResolvedAvailabilityEvent {
    miner_uid: u16,
    hotkey: String,
    node_id: String,
    is_available: bool,
    is_rented: bool,
    is_validated: bool,
    source: AvailabilitySource,
    source_metadata: Option<String>,
    observed_at_ms: i64,
    gpu_category: Option<String>,
    gpu_count: Option<u32>,
}

#[derive(Debug, Clone)]
struct CurrentAvailabilityRow {
    id: i64,
    is_available: bool,
    is_rented: bool,
    is_validated: bool,
    gpu_category: Option<String>,
    gpu_count: Option<u32>,
    row_effective_at: i64,
}

pub struct AvailabilityLogRepository {
    pool: SqlitePool,
}

impl AvailabilityLogRepository {
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    pub async fn record_event(
        &self,
        event: AvailabilityEventRequest,
    ) -> Result<AvailabilityTransition> {
        let resolved = self.resolve_event(event).await?;
        let mut tx = self.pool.begin().await?;

        let current = sqlx::query(
            r#"
            SELECT id, is_available, is_rented, is_validated, gpu_category, gpu_count, row_effective_at
            FROM availability_log
            WHERE hotkey = ? AND node_id = ? AND is_current = 1
            LIMIT 1
            "#,
        )
        .bind(&resolved.hotkey)
        .bind(&resolved.node_id)
        .fetch_optional(&mut *tx)
        .await?
        .map(|row| CurrentAvailabilityRow {
            id: row.get("id"),
            is_available: int_to_bool(row.get::<i64, _>("is_available")),
            is_rented: int_to_bool(row.get::<i64, _>("is_rented")),
            is_validated: int_to_bool(row.get::<i64, _>("is_validated")),
            gpu_category: row.get("gpu_category"),
            gpu_count: row.get::<Option<i64>, _>("gpu_count").map(|v| v as u32),
            row_effective_at: row.get("row_effective_at"),
        });

        let transition = match current {
            Some(current) => {
                if resolved.observed_at_ms < current.row_effective_at {
                    if states_match(&current, &resolved) {
                        AvailabilityTransition::NoOp
                    } else {
                        debug!(
                            hotkey = %resolved.hotkey,
                            node_id = %resolved.node_id,
                            observed_at_ms = resolved.observed_at_ms,
                            current_effective_at = current.row_effective_at,
                            source = resolved.source.as_str(),
                            "Ignoring out-of-order availability observation"
                        );
                        AvailabilityTransition::IgnoredOutOfOrder
                    }
                } else if states_match(&current, &resolved) {
                    AvailabilityTransition::NoOp
                } else {
                    sqlx::query(
                        r#"
                        UPDATE availability_log
                        SET is_current = 0,
                            row_expiration_at = ?
                        WHERE id = ?
                        "#,
                    )
                    .bind(resolved.observed_at_ms)
                    .bind(current.id)
                    .execute(&mut *tx)
                    .await?;

                    self.insert_current_row(&mut tx, &resolved).await?;
                    AvailabilityTransition::Transitioned
                }
            }
            None => {
                self.insert_current_row(&mut tx, &resolved).await?;
                AvailabilityTransition::Inserted
            }
        };

        tx.commit().await?;
        Ok(transition)
    }

    pub async fn record_events(&self, events: Vec<AvailabilityEventRequest>) -> Result<usize> {
        let mut changed = 0usize;
        for event in events {
            if matches!(
                self.record_event(event).await?,
                AvailabilityTransition::Inserted | AvailabilityTransition::Transitioned
            ) {
                changed += 1;
            }
        }
        Ok(changed)
    }

    pub async fn cleanup_expired_rows(&self, retention_days: i64) -> Result<u64> {
        let cutoff_ms = (Utc::now() - Duration::days(retention_days)).timestamp_millis();
        let result = sqlx::query(
            r#"
            DELETE FROM availability_log
            WHERE is_current = 0
              AND row_expiration_at IS NOT NULL
              AND row_expiration_at < ?
            "#,
        )
        .bind(cutoff_ms)
        .execute(&self.pool)
        .await?;

        Ok(result.rows_affected())
    }

    pub async fn current_row(
        &self,
        hotkey: &str,
        node_id: &str,
    ) -> Result<Option<AvailabilityLogRow>> {
        let row = sqlx::query(
            r#"
            SELECT miner_uid, hotkey, node_id, is_available, is_rented, is_validated,
                   source, source_metadata, gpu_category, gpu_count,
                   row_effective_at, row_expiration_at, is_current
            FROM availability_log
            WHERE hotkey = ? AND node_id = ? AND is_current = 1
            LIMIT 1
            "#,
        )
        .bind(hotkey)
        .bind(node_id)
        .fetch_optional(&self.pool)
        .await?;

        Ok(row.map(map_row))
    }

    pub async fn row_history(
        &self,
        hotkey: &str,
        node_id: &str,
    ) -> Result<Vec<AvailabilityLogRow>> {
        let rows = sqlx::query(
            r#"
            SELECT miner_uid, hotkey, node_id, is_available, is_rented, is_validated,
                   source, source_metadata, gpu_category, gpu_count,
                   row_effective_at, row_expiration_at, is_current
            FROM availability_log
            WHERE hotkey = ? AND node_id = ?
            ORDER BY row_effective_at ASC, id ASC
            "#,
        )
        .bind(hotkey)
        .bind(node_id)
        .fetch_all(&self.pool)
        .await?;

        Ok(rows.into_iter().map(map_row).collect())
    }

    async fn resolve_event(
        &self,
        event: AvailabilityEventRequest,
    ) -> Result<ResolvedAvailabilityEvent> {
        let miner_uid = event
            .miner_uid
            .or_else(|| parse_miner_uid(&event.miner_id))
            .ok_or_else(|| anyhow!("Unable to resolve miner UID for {}", event.miner_id))?;

        let hotkey = match event.hotkey {
            Some(hotkey) => hotkey,
            None => self
                .lookup_hotkey(&event.miner_id)
                .await?
                .ok_or_else(|| anyhow!("Missing hotkey for miner {}", event.miner_id))?,
        };

        let is_rented = match event.is_rented {
            Some(is_rented) => is_rented,
            None => self
                .lookup_is_rented(&event.miner_id, &event.node_id)
                .await?
                .unwrap_or(false),
        };

        let (gpu_category, gpu_count) = match (event.gpu_category, event.gpu_count) {
            (Some(cat), Some(cnt)) => (Some(cat), Some(cnt)),
            _ => {
                self.lookup_gpu_metadata(&event.miner_id, &event.node_id)
                    .await?
            }
        };

        Ok(ResolvedAvailabilityEvent {
            miner_uid,
            hotkey,
            node_id: event.node_id,
            is_available: event.is_available,
            is_rented,
            is_validated: event.is_validated,
            source: event.source,
            source_metadata: event.source_metadata,
            observed_at_ms: event.observed_at.timestamp_millis(),
            gpu_category,
            gpu_count,
        })
    }

    async fn lookup_hotkey(&self, miner_id: &str) -> Result<Option<String>> {
        sqlx::query_scalar("SELECT hotkey FROM miners WHERE id = ?")
            .bind(miner_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(Into::into)
    }

    async fn lookup_gpu_metadata(
        &self,
        miner_id: &str,
        node_id: &str,
    ) -> Result<(Option<String>, Option<u32>)> {
        let row = sqlx::query(
            r#"
            SELECT gpu_category, gpu_count
            FROM miner_nodes
            WHERE miner_id = ? AND node_id = ?
            LIMIT 1
            "#,
        )
        .bind(miner_id)
        .bind(node_id)
        .fetch_optional(&self.pool)
        .await?;

        Ok(match row {
            Some(row) => {
                let gpu_category: Option<String> = row.get("gpu_category");
                let gpu_count: i64 = row.get("gpu_count");
                (
                    gpu_category.filter(|v| !v.trim().is_empty()),
                    if gpu_count > 0 {
                        Some(gpu_count as u32)
                    } else {
                        None
                    },
                )
            }
            None => (None, None),
        })
    }

    async fn lookup_is_rented(&self, miner_id: &str, node_id: &str) -> Result<Option<bool>> {
        let row = sqlx::query(
            r#"
            SELECT active_rental_id
            FROM miner_nodes
            WHERE miner_id = ? AND node_id = ?
            LIMIT 1
            "#,
        )
        .bind(miner_id)
        .bind(node_id)
        .fetch_optional(&self.pool)
        .await?;

        Ok(row.map(|row| row.get::<Option<String>, _>("active_rental_id").is_some()))
    }

    async fn insert_current_row(
        &self,
        tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
        event: &ResolvedAvailabilityEvent,
    ) -> Result<()> {
        sqlx::query(
            r#"
            INSERT INTO availability_log (
                miner_uid, hotkey, node_id, is_available, is_rented, is_validated,
                source, source_metadata, gpu_category, gpu_count,
                row_effective_at, row_expiration_at, is_current
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
            "#,
        )
        .bind(event.miner_uid as i64)
        .bind(&event.hotkey)
        .bind(&event.node_id)
        .bind(bool_to_int(event.is_available))
        .bind(bool_to_int(event.is_rented))
        .bind(bool_to_int(event.is_validated))
        .bind(event.source.as_str())
        .bind(&event.source_metadata)
        .bind(&event.gpu_category)
        .bind(event.gpu_count.map(|v| v as i64))
        .bind(event.observed_at_ms)
        .execute(&mut **tx)
        .await?;

        Ok(())
    }
}

impl SimplePersistence {
    pub async fn record_availability_event(&self, event: AvailabilityEventRequest) {
        self.record_availability_events(vec![event]).await;
    }

    pub async fn record_availability_events(&self, events: Vec<AvailabilityEventRequest>) {
        if events.is_empty() {
            return;
        }

        let repo = AvailabilityLogRepository::new(self.pool().clone());
        if let Err(error) = repo.record_events(events).await {
            warn!(error = %error, "Failed to record availability events");
        }
    }
}

fn bool_to_int(value: bool) -> i64 {
    if value {
        1
    } else {
        0
    }
}

fn int_to_bool(value: i64) -> bool {
    value != 0
}

fn parse_miner_uid(miner_id: &str) -> Option<u16> {
    miner_id
        .strip_prefix("miner_")
        .and_then(|value| value.parse::<u16>().ok())
}

fn states_match(current: &CurrentAvailabilityRow, resolved: &ResolvedAvailabilityEvent) -> bool {
    current.is_available == resolved.is_available
        && current.is_rented == resolved.is_rented
        && current.is_validated == resolved.is_validated
        && current.gpu_category == resolved.gpu_category
        && current.gpu_count == resolved.gpu_count
}

fn map_row(row: sqlx::sqlite::SqliteRow) -> AvailabilityLogRow {
    AvailabilityLogRow {
        miner_uid: row.get::<i64, _>("miner_uid") as u16,
        hotkey: row.get("hotkey"),
        node_id: row.get("node_id"),
        is_available: int_to_bool(row.get::<i64, _>("is_available")),
        is_rented: int_to_bool(row.get::<i64, _>("is_rented")),
        is_validated: int_to_bool(row.get::<i64, _>("is_validated")),
        source: row.get("source"),
        source_metadata: row.get("source_metadata"),
        gpu_category: row.get("gpu_category"),
        gpu_count: row.get::<Option<i64>, _>("gpu_count").map(|v| v as u32),
        row_effective_at: row.get("row_effective_at"),
        row_expiration_at: row.get("row_expiration_at"),
        is_current: int_to_bool(row.get::<i64, _>("is_current")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_event(
        is_available: bool,
        is_rented: bool,
        is_validated: bool,
        observed_at: DateTime<Utc>,
    ) -> AvailabilityEventRequest {
        AvailabilityEventRequest {
            miner_id: "miner_1".to_string(),
            miner_uid: Some(1),
            hotkey: Some("hotkey_1".to_string()),
            node_id: "node-1".to_string(),
            is_available,
            is_rented: Some(is_rented),
            is_validated,
            source: AvailabilitySource::Validation,
            source_metadata: Some("full".to_string()),
            observed_at,
            gpu_category: None,
            gpu_count: None,
        }
    }

    async fn create_repo() -> Result<AvailabilityLogRepository> {
        let persistence = SimplePersistence::for_testing().await?;
        sqlx::query(
            "INSERT INTO miners (id, hotkey, endpoint, updated_at)
             VALUES ('miner_1', 'hotkey_1', 'http://127.0.0.1:9090', datetime('now'))",
        )
        .execute(persistence.pool())
        .await?;
        Ok(AvailabilityLogRepository::new(persistence.pool().clone()))
    }

    #[tokio::test]
    async fn inserts_initial_current_row() -> Result<()> {
        let repo = create_repo().await?;
        let observed_at = Utc::now();

        let transition = repo
            .record_event(test_event(true, false, true, observed_at))
            .await?;

        assert_eq!(transition, AvailabilityTransition::Inserted);

        let current = repo
            .current_row("hotkey_1", "node-1")
            .await?
            .expect("current row");
        assert!(current.is_available);
        assert!(!current.is_rented);
        assert!(current.is_validated);
        assert!(current.is_current);
        assert_eq!(current.row_effective_at, observed_at.timestamp_millis());
        assert!(current.row_expiration_at.is_none());

        Ok(())
    }

    #[tokio::test]
    async fn identical_state_observation_is_no_op() -> Result<()> {
        let repo = create_repo().await?;
        let observed_at = Utc::now();

        repo.record_event(test_event(true, false, true, observed_at))
            .await?;

        let transition = repo
            .record_event(test_event(
                true,
                false,
                true,
                observed_at + Duration::seconds(30),
            ))
            .await?;

        assert_eq!(transition, AvailabilityTransition::NoOp);
        let history = repo.row_history("hotkey_1", "node-1").await?;
        assert_eq!(history.len(), 1);

        Ok(())
    }

    #[tokio::test]
    async fn changed_state_expires_previous_row_and_inserts_new_current_row() -> Result<()> {
        let repo = create_repo().await?;
        let first = Utc::now();
        let second = first + Duration::minutes(5);

        repo.record_event(test_event(true, false, true, first))
            .await?;
        let transition = repo
            .record_event(test_event(false, false, false, second))
            .await?;

        assert_eq!(transition, AvailabilityTransition::Transitioned);

        let history = repo.row_history("hotkey_1", "node-1").await?;
        assert_eq!(history.len(), 2);
        assert_eq!(
            history[0].row_expiration_at,
            Some(second.timestamp_millis())
        );
        assert!(!history[0].is_current);
        assert_eq!(history[1].row_effective_at, second.timestamp_millis());
        assert!(history[1].is_current);
        assert!(!history[1].is_available);

        Ok(())
    }

    #[tokio::test]
    async fn same_timestamp_different_state_transitions_instead_of_ignored() -> Result<()> {
        let repo = create_repo().await?;
        let observed_at = Utc::now();

        repo.record_event(test_event(true, false, true, observed_at))
            .await?;

        // Same timestamp, different state (is_rented changed)
        let transition = repo
            .record_event(test_event(true, true, true, observed_at))
            .await?;

        assert_eq!(transition, AvailabilityTransition::Transitioned);

        let history = repo.row_history("hotkey_1", "node-1").await?;
        assert_eq!(history.len(), 2);
        assert!(!history[0].is_current);
        assert_eq!(
            history[0].row_expiration_at,
            Some(observed_at.timestamp_millis())
        );
        assert!(history[1].is_current);
        assert!(history[1].is_rented);

        Ok(())
    }

    #[tokio::test]
    async fn cleanup_expired_rows_only_removes_expired_history() -> Result<()> {
        let repo = create_repo().await?;
        let first = Utc::now() - Duration::days(10);
        let second = first + Duration::hours(1);

        repo.record_event(test_event(true, false, true, first))
            .await?;
        repo.record_event(test_event(false, false, false, second))
            .await?;

        let updated = sqlx::query(
            "UPDATE availability_log
             SET row_expiration_at = ?
             WHERE is_current = 0",
        )
        .bind((Utc::now() - Duration::days(5)).timestamp_millis())
        .execute(&repo.pool)
        .await?;
        assert_eq!(updated.rows_affected(), 1);

        let deleted = repo.cleanup_expired_rows(1).await?;
        assert_eq!(deleted, 1);

        let history = repo.row_history("hotkey_1", "node-1").await?;
        assert_eq!(history.len(), 1);
        assert!(history[0].is_current);

        Ok(())
    }
}
