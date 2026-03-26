use crate::basilica_api::{
    BasilicaApiClient, BasilicaApiError, IncentiveConfigResponse, NewCuLedgerRowRequest,
    PostSlashResponse,
};
use crate::config::SlashMode;
use crate::persistence::availability_log::AvailabilityLogRow;
use crate::persistence::incentive_state::{IncentiveStateRepository, PendingSlashEvent};
use anyhow::{Context, Result};
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use sqlx::Row;
use sqlx::SqlitePool;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::time::{sleep, Duration as TokioDuration};
use tracing::{info, warn};

const HOUR_MS: i64 = 60 * 60 * 1000;
const MAX_RETRIES: usize = 3;

#[async_trait]
pub trait IncentiveApi: Send + Sync {
    async fn get_incentive_config(
        &self,
    ) -> std::result::Result<IncentiveConfigResponse, BasilicaApiError>;

    async fn submit_cus(
        &self,
        rows: Vec<NewCuLedgerRowRequest>,
    ) -> std::result::Result<usize, BasilicaApiError>;

    async fn slash_node(
        &self,
        node_id: &str,
        slash_pct: u32,
    ) -> std::result::Result<PostSlashResponse, BasilicaApiError>;
}

#[async_trait]
impl IncentiveApi for BasilicaApiClient {
    async fn get_incentive_config(
        &self,
    ) -> std::result::Result<IncentiveConfigResponse, BasilicaApiError> {
        BasilicaApiClient::get_incentive_config(self).await
    }

    async fn submit_cus(
        &self,
        rows: Vec<NewCuLedgerRowRequest>,
    ) -> std::result::Result<usize, BasilicaApiError> {
        BasilicaApiClient::submit_cus(self, rows).await
    }

    async fn slash_node(
        &self,
        node_id: &str,
        slash_pct: u32,
    ) -> std::result::Result<PostSlashResponse, BasilicaApiError> {
        BasilicaApiClient::slash_node(self, node_id, slash_pct).await
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CuGeneratorRunSummary {
    pub processed_windows: usize,
    pub submitted_rows: usize,
    pub inserted_rows: usize,
    pub slash_events_processed: usize,
}

pub struct CuGenerator {
    pool: SqlitePool,
    api: Arc<dyn IncentiveApi>,
    slash_mode: SlashMode,
}

impl CuGenerator {
    pub fn new(pool: SqlitePool, api: Arc<dyn IncentiveApi>, slash_mode: SlashMode) -> Self {
        Self {
            pool,
            api,
            slash_mode,
        }
    }

    pub async fn run_once_at(&self, now: DateTime<Utc>) -> Result<CuGeneratorRunSummary> {
        let repo = IncentiveStateRepository::new(self.pool.clone());
        let completed_window_end_ms = floor_to_hour_ms(now.timestamp_millis());

        let progress_ms = match repo.load_cu_progress().await? {
            Some(progress) => progress,
            None => initial_progress_ms(&repo, completed_window_end_ms).await?,
        };

        if progress_ms >= completed_window_end_ms {
            return Ok(CuGeneratorRunSummary {
                processed_windows: 0,
                submitted_rows: 0,
                inserted_rows: 0,
                slash_events_processed: 0,
            });
        }

        let availability_rows =
            load_availability_rows(&self.pool, progress_ms, completed_window_end_ms).await?;
        let slash_events = repo
            .list_unprocessed_slash_events(completed_window_end_ms)
            .await?;
        let node_metadata = load_node_metadata(&self.pool, &availability_rows).await?;
        let config = self.get_config_with_retry().await?;
        let windows = generate_hourly_cu_windows(
            Some(progress_ms),
            completed_window_end_ms,
            &availability_rows,
            &node_metadata,
            &slash_events,
            &config,
        )?;

        let mut summary = CuGeneratorRunSummary {
            processed_windows: 0,
            submitted_rows: 0,
            inserted_rows: 0,
            slash_events_processed: 0,
        };

        for window in windows {
            if !window.rows.is_empty() {
                summary.submitted_rows += window.rows.len();
                summary.inserted_rows += self.submit_cus_with_retry(window.rows.clone()).await?;
            }

            for slash_event in &window.slash_events {
                let mode_str = match self.slash_mode {
                    SlashMode::Hard => {
                        self.slash_node_with_retry(&slash_event.node_id, config.slash_pct)
                            .await
                            .with_context(|| {
                                format!(
                                    "failed to slash node {} for rental {}",
                                    slash_event.node_id, slash_event.rental_id
                                )
                            })?;
                        "hard"
                    }
                    SlashMode::Soft => {
                        warn!(
                            node_id = %slash_event.node_id,
                            rental_id = %slash_event.rental_id,
                            reason = %slash_event.reason,
                            slash_pct = config.slash_pct,
                            "Soft slash: would have slashed node (API call skipped)"
                        );
                        "soft"
                    }
                };
                repo.mark_slash_event_processed(
                    &slash_event.rental_id,
                    window.earned_at.timestamp_millis(),
                    mode_str,
                    config.slash_pct,
                )
                .await?;
                summary.slash_events_processed += 1;
            }

            repo.save_cu_progress(window.earned_at.timestamp_millis())
                .await?;
            summary.processed_windows += 1;
        }

        info!(
            processed_windows = summary.processed_windows,
            submitted_rows = summary.submitted_rows,
            inserted_rows = summary.inserted_rows,
            slash_events_processed = summary.slash_events_processed,
            "Completed CU generator run"
        );

        Ok(summary)
    }

    async fn get_config_with_retry(&self) -> Result<IncentiveConfigResponse> {
        let mut backoff = TokioDuration::from_secs(1);
        for attempt in 1..=MAX_RETRIES {
            match self.api.get_incentive_config().await {
                Ok(config) => return Ok(config),
                Err(error) if attempt < MAX_RETRIES => {
                    warn!(
                        attempt,
                        error = %error,
                        "Failed to fetch incentive config; retrying"
                    );
                    sleep(backoff).await;
                    backoff *= 2;
                }
                Err(error) => return Err(anyhow::anyhow!(error.to_string())),
            }
        }

        unreachable!("retry loop must return");
    }

    async fn submit_cus_with_retry(&self, rows: Vec<NewCuLedgerRowRequest>) -> Result<usize> {
        let mut backoff = TokioDuration::from_secs(1);
        for attempt in 1..=MAX_RETRIES {
            match self.api.submit_cus(rows.clone()).await {
                Ok(inserted) => return Ok(inserted),
                Err(error) if attempt < MAX_RETRIES => {
                    warn!(
                        attempt,
                        error = %error,
                        "Failed to submit CU batch; retrying"
                    );
                    sleep(backoff).await;
                    backoff *= 2;
                }
                Err(error) => return Err(anyhow::anyhow!(error.to_string())),
            }
        }

        unreachable!("retry loop must return");
    }

    async fn slash_node_with_retry(&self, node_id: &str, slash_pct: u32) -> Result<()> {
        let mut backoff = TokioDuration::from_secs(1);
        for attempt in 1..=MAX_RETRIES {
            match self.api.slash_node(node_id, slash_pct).await {
                Ok(_) => return Ok(()),
                Err(error) if attempt < MAX_RETRIES => {
                    warn!(
                        attempt,
                        node_id = %node_id,
                        error = %error,
                        "Failed to submit slash request; retrying"
                    );
                    sleep(backoff).await;
                    backoff *= 2;
                }
                Err(error) => return Err(anyhow::anyhow!(error.to_string())),
            }
        }

        unreachable!("retry loop must return");
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct GeneratedCuWindow {
    pub earned_at: DateTime<Utc>,
    pub rows: Vec<NewCuLedgerRowRequest>,
    pub slash_events: Vec<PendingSlashEvent>,
}

#[derive(Debug, Clone)]
pub struct NodeIncentiveMetadata {
    pub gpu_category: String,
    pub gpu_count: u32,
}

pub fn generate_hourly_cu_windows(
    progress_ms: Option<i64>,
    window_end_ms: i64,
    availability_rows: &[AvailabilityLogRow],
    node_metadata: &HashMap<(String, String), NodeIncentiveMetadata>,
    slash_events: &[PendingSlashEvent],
    config: &IncentiveConfigResponse,
) -> Result<Vec<GeneratedCuWindow>> {
    let Some(mut current_ms) = progress_ms else {
        return Ok(Vec::new());
    };

    let mut windows = Vec::new();
    while current_ms < window_end_ms {
        let next_ms = current_ms + HOUR_MS;
        let mut rows = Vec::new();
        for aggregate in aggregate_node_windows(current_ms, next_ms, availability_rows) {
            let key = (aggregate.hotkey.clone(), aggregate.node_id.clone());
            let Some(metadata) = node_metadata.get(&key) else {
                warn!(
                    node_id = %aggregate.node_id,
                    hotkey = %aggregate.hotkey,
                    "Skipping CU generation for node with missing incentive metadata"
                );
                continue;
            };

            let Some(category_config) = config.gpu_categories.get(&metadata.gpu_category) else {
                warn!(
                    node_id = %aggregate.node_id,
                    gpu_category = %metadata.gpu_category,
                    "Skipping CU generation for node whose GPU category is not in incentive config"
                );
                continue;
            };

            let available_ms = Decimal::from(aggregate.available_ms);
            let cu_amount =
                available_ms * Decimal::from(metadata.gpu_count as i64) / Decimal::from(HOUR_MS);
            if cu_amount <= Decimal::ZERO {
                continue;
            }

            rows.push(NewCuLedgerRowRequest {
                hotkey: aggregate.hotkey,
                miner_uid: aggregate.miner_uid as u32,
                node_id: aggregate.node_id,
                cu_amount,
                earned_at: DateTime::from_timestamp_millis(next_ms)
                    .expect("window end should always be a valid timestamp"),
                is_rented: aggregate.is_rented,
                gpu_category: metadata.gpu_category.clone(),
                window_hours: config.window_hours,
                price_usd: category_config.price_usd,
                idempotency_key: format!("{}:{}", aggregate.node_id_for_key, next_ms / 1000),
            });
        }

        let slash_events = slash_events
            .iter()
            .filter(|event| event.detected_at_ms >= current_ms && event.detected_at_ms < next_ms)
            .cloned()
            .collect();

        windows.push(GeneratedCuWindow {
            earned_at: DateTime::from_timestamp_millis(next_ms)
                .expect("window end should always be a valid timestamp"),
            rows,
            slash_events,
        });

        current_ms = next_ms;
    }

    Ok(windows)
}

#[derive(Debug, Clone)]
struct NodeWindowAggregate {
    hotkey: String,
    miner_uid: u16,
    node_id: String,
    node_id_for_key: String,
    is_rented: bool,
    available_ms: i64,
    latest_available_effective_at: i64,
}

fn aggregate_node_windows(
    window_start_ms: i64,
    window_end_ms: i64,
    availability_rows: &[AvailabilityLogRow],
) -> Vec<NodeWindowAggregate> {
    let mut aggregates: HashMap<(String, String), NodeWindowAggregate> = HashMap::new();

    for row in availability_rows {
        let row_end_ms = row.row_expiration_at.unwrap_or(window_end_ms);
        let overlap_start_ms = row.row_effective_at.max(window_start_ms);
        let overlap_end_ms = row_end_ms.min(window_end_ms);
        let overlap_ms = (overlap_end_ms - overlap_start_ms).max(0);

        if overlap_ms == 0 || !row.is_available {
            continue;
        }

        let key = (row.hotkey.clone(), row.node_id.clone());
        let aggregate = aggregates
            .entry(key)
            .or_insert_with(|| NodeWindowAggregate {
                hotkey: row.hotkey.clone(),
                miner_uid: row.miner_uid,
                node_id: row.node_id.clone(),
                node_id_for_key: row.node_id.clone(),
                is_rented: row.is_rented,
                available_ms: 0,
                latest_available_effective_at: row.row_effective_at,
            });

        aggregate.available_ms += overlap_ms;
        if row.row_effective_at >= aggregate.latest_available_effective_at {
            aggregate.latest_available_effective_at = row.row_effective_at;
            aggregate.is_rented = row.is_rented;
        }
    }

    let mut aggregates = aggregates.into_values().collect::<Vec<_>>();
    aggregates.sort_by(|left, right| {
        left.hotkey
            .cmp(&right.hotkey)
            .then_with(|| left.node_id.cmp(&right.node_id))
    });
    aggregates
}

async fn initial_progress_ms(repo: &IncentiveStateRepository, fallback_ms: i64) -> Result<i64> {
    let availability_start = repo.earliest_availability_effective_at_ms().await?;
    let slash_start = repo.earliest_unprocessed_slash_event_at_ms().await?;

    Ok(match (availability_start, slash_start) {
        (Some(a), Some(b)) => floor_to_hour_ms(a.min(b)),
        (Some(a), None) => floor_to_hour_ms(a),
        (None, Some(b)) => floor_to_hour_ms(b),
        (None, None) => fallback_ms,
    })
}

async fn load_availability_rows(
    pool: &SqlitePool,
    start_ms: i64,
    end_ms: i64,
) -> Result<Vec<AvailabilityLogRow>> {
    let rows = sqlx::query(
        "SELECT miner_uid, hotkey, node_id, is_available, is_rented, is_validated,
                source, source_metadata, row_effective_at, row_expiration_at, is_current
         FROM availability_log
         WHERE row_effective_at < ?
           AND COALESCE(row_expiration_at, ?) > ?
         ORDER BY row_effective_at ASC, id ASC",
    )
    .bind(end_ms)
    .bind(end_ms)
    .bind(start_ms)
    .fetch_all(pool)
    .await?;

    Ok(rows
        .into_iter()
        .map(|row| AvailabilityLogRow {
            miner_uid: row.get::<i64, _>("miner_uid") as u16,
            hotkey: row.get("hotkey"),
            node_id: row.get("node_id"),
            is_available: row.get::<i64, _>("is_available") != 0,
            is_rented: row.get::<i64, _>("is_rented") != 0,
            is_validated: row.get::<i64, _>("is_validated") != 0,
            source: row.get("source"),
            source_metadata: row.get("source_metadata"),
            row_effective_at: row.get("row_effective_at"),
            row_expiration_at: row.get("row_expiration_at"),
            is_current: row.get::<i64, _>("is_current") != 0,
        })
        .collect())
}

async fn load_node_metadata(
    pool: &SqlitePool,
    availability_rows: &[AvailabilityLogRow],
) -> Result<HashMap<(String, String), NodeIncentiveMetadata>> {
    let mut node_keys = availability_rows
        .iter()
        .map(|row| (row.hotkey.clone(), row.node_id.clone()))
        .collect::<Vec<_>>();
    node_keys.sort();
    node_keys.dedup();

    let mut metadata = HashMap::new();
    for (hotkey, node_id) in node_keys {
        if let Some(row) = sqlx::query(
            "SELECT mn.gpu_category, mn.gpu_count
             FROM miner_nodes mn
             JOIN miners m ON mn.miner_id = m.id
             WHERE m.hotkey = ?
               AND mn.node_id = ?
             LIMIT 1",
        )
        .bind(&hotkey)
        .bind(&node_id)
        .fetch_optional(pool)
        .await?
        {
            let gpu_category: Option<String> = row.get("gpu_category");
            let gpu_count: i64 = row.get("gpu_count");
            if let Some(gpu_category) = gpu_category.filter(|value| !value.trim().is_empty()) {
                if gpu_count > 0 {
                    metadata.insert(
                        (hotkey, node_id),
                        NodeIncentiveMetadata {
                            gpu_category,
                            gpu_count: gpu_count as u32,
                        },
                    );
                }
            }
        }
    }

    Ok(metadata)
}

fn floor_to_hour_ms(timestamp_ms: i64) -> i64 {
    timestamp_ms - timestamp_ms.rem_euclid(HOUR_MS)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::basilica_api::{IncentiveGpuCategoryConfig, PostSlashResponse};
    use crate::persistence::availability_log::AvailabilityLogRow;
    use crate::persistence::incentive_state::{IncentiveStateRepository, SlashEventRequest};
    use anyhow::Result;
    use chrono::{Duration, TimeZone};
    use rust_decimal::Decimal;
    use std::collections::{HashMap, HashSet};
    use std::str::FromStr;
    use tokio::sync::Mutex;

    #[derive(Default)]
    struct FakeIncentiveApi {
        submitted_keys: Mutex<HashSet<String>>,
        submitted_batches: Mutex<Vec<Vec<NewCuLedgerRowRequest>>>,
        slash_calls: Mutex<Vec<String>>,
    }

    #[async_trait]
    impl IncentiveApi for FakeIncentiveApi {
        async fn get_incentive_config(
            &self,
        ) -> std::result::Result<IncentiveConfigResponse, BasilicaApiError> {
            Ok(test_config())
        }

        async fn submit_cus(
            &self,
            rows: Vec<NewCuLedgerRowRequest>,
        ) -> std::result::Result<usize, BasilicaApiError> {
            let mut submitted_batches = self.submitted_batches.lock().await;
            submitted_batches.push(rows.clone());
            let mut keys = self.submitted_keys.lock().await;
            let mut inserted = 0usize;
            for row in rows {
                if keys.insert(row.idempotency_key) {
                    inserted += 1;
                }
            }
            Ok(inserted)
        }

        async fn slash_node(
            &self,
            node_id: &str,
            _slash_pct: u32,
        ) -> std::result::Result<PostSlashResponse, BasilicaApiError> {
            self.slash_calls.lock().await.push(node_id.to_string());
            Ok(PostSlashResponse {
                slashed_cu_count: 1,
                slashed_ru_count: 0,
            })
        }
    }

    fn test_config() -> IncentiveConfigResponse {
        let mut gpu_categories = HashMap::new();
        gpu_categories.insert(
            "H100".to_string(),
            IncentiveGpuCategoryConfig {
                target_count: 2,
                price_usd: Decimal::from_str("3.00").unwrap(),
            },
        );

        IncentiveConfigResponse {
            gpu_categories,
            window_hours: 72,
            max_cu_value_usd: Decimal::from_str("0.05").unwrap(),
            revenue_share_pct: Some(30),
            slash_pct: 100,
        }
    }

    fn availability_row(
        effective_at: DateTime<Utc>,
        expiration_at: Option<DateTime<Utc>>,
        is_available: bool,
        is_rented: bool,
    ) -> AvailabilityLogRow {
        AvailabilityLogRow {
            miner_uid: 7,
            hotkey: "hotkey-7".to_string(),
            node_id: "node-1".to_string(),
            is_available,
            is_rented,
            is_validated: true,
            source: "validation".to_string(),
            source_metadata: None,
            row_effective_at: effective_at.timestamp_millis(),
            row_expiration_at: expiration_at.map(|value| value.timestamp_millis()),
            is_current: expiration_at.is_none(),
        }
    }

    async fn create_repo() -> Result<IncentiveStateRepository> {
        let persistence = crate::persistence::SimplePersistence::for_testing().await?;
        Ok(IncentiveStateRepository::new(persistence.pool().clone()))
    }

    #[tokio::test]
    async fn generator_progress_checkpoints_completed_windows() -> Result<()> {
        let persistence = crate::persistence::SimplePersistence::for_testing().await?;
        let api = Arc::new(FakeIncentiveApi::default());
        let generator = CuGenerator::new(persistence.pool().clone(), api.clone(), SlashMode::Hard);
        let start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 5, 0).unwrap();

        sqlx::query(
            "INSERT INTO miners (id, hotkey, endpoint, updated_at)
             VALUES ('miner_7', 'hotkey-7', 'http://127.0.0.1:1234', datetime('now'))",
        )
        .execute(persistence.pool())
        .await?;
        sqlx::query(
            "INSERT INTO miner_nodes (id, miner_id, node_id, ssh_endpoint, node_ip, gpu_count, hourly_rate_cents, gpu_category, status, bid_active, created_at)
             VALUES ('miner_7_node-1', 'miner_7', 'node-1', 'root@127.0.0.1:22', '127.0.0.1', 1, 100, 'H100', 'online', 1, datetime('now'))",
        )
        .execute(persistence.pool())
        .await?;
        sqlx::query(
            "INSERT INTO availability_log (miner_uid, hotkey, node_id, is_available, is_rented, is_validated, source, row_effective_at, row_expiration_at, is_current)
             VALUES (7, 'hotkey-7', 'node-1', 1, 0, 1, 'validation', ?, NULL, 1)",
        )
        .bind(start.timestamp_millis())
        .execute(persistence.pool())
        .await?;

        let first = generator
            .run_once_at(Utc.with_ymd_and_hms(2026, 3, 23, 11, 5, 0).unwrap())
            .await?;
        let second = generator
            .run_once_at(Utc.with_ymd_and_hms(2026, 3, 23, 11, 15, 0).unwrap())
            .await?;

        assert_eq!(first.processed_windows, 1);
        assert_eq!(second.processed_windows, 0);
        assert_eq!(api.submitted_batches.lock().await.len(), 1);
        Ok(())
    }

    #[test]
    fn cu_amount_generation_uses_scd2_overlap_history() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(2);
        let rows = vec![
            availability_row(
                window_start + Duration::minutes(5),
                Some(window_start + Duration::hours(1) + Duration::minutes(15)),
                true,
                false,
            ),
            availability_row(
                window_start + Duration::hours(1) + Duration::minutes(15),
                None,
                false,
                false,
            ),
        ];
        let metadata = HashMap::from([(
            ("hotkey-7".to_string(), "node-1".to_string()),
            NodeIncentiveMetadata {
                gpu_category: "H100".to_string(),
                gpu_count: 1,
            },
        )]);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 2);
        assert_eq!(windows[0].rows.len(), 1);
        assert_eq!(windows[1].rows.len(), 1);
        assert_eq!(
            windows[0].rows[0].cu_amount.round_dp(18),
            Decimal::from_str("0.916666666666666667").unwrap()
        );
        assert_eq!(
            windows[1].rows[0].cu_amount,
            Decimal::from_str("0.25").unwrap()
        );
        Ok(())
    }

    #[tokio::test]
    async fn generator_reuses_stable_idempotency_keys_when_replaying_same_window() -> Result<()> {
        let persistence = crate::persistence::SimplePersistence::for_testing().await?;
        let api = Arc::new(FakeIncentiveApi::default());
        let generator = CuGenerator::new(persistence.pool().clone(), api.clone(), SlashMode::Hard);
        let start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 5, 0).unwrap();
        sqlx::query(
            "INSERT INTO miners (id, hotkey, endpoint, updated_at)
             VALUES ('miner_7', 'hotkey-7', 'http://127.0.0.1:1234', datetime('now'))",
        )
        .execute(persistence.pool())
        .await?;
        sqlx::query(
            "INSERT INTO miner_nodes (id, miner_id, node_id, ssh_endpoint, node_ip, gpu_count, hourly_rate_cents, gpu_category, status, bid_active, created_at)
             VALUES ('miner_7_node-1', 'miner_7', 'node-1', 'root@127.0.0.1:22', '127.0.0.1', 1, 100, 'H100', 'online', 1, datetime('now'))",
        )
        .execute(persistence.pool())
        .await?;
        sqlx::query(
            "INSERT INTO availability_log (miner_uid, hotkey, node_id, is_available, is_rented, is_validated, source, row_effective_at, row_expiration_at, is_current)
             VALUES (7, 'hotkey-7', 'node-1', 1, 0, 1, 'validation', ?, NULL, 1)",
        )
        .bind(start.timestamp_millis())
        .execute(persistence.pool())
        .await?;

        let first_run = generator
            .run_once_at(Utc.with_ymd_and_hms(2026, 3, 23, 11, 5, 0).unwrap())
            .await;
        assert!(first_run.is_ok());

        IncentiveStateRepository::new(persistence.pool().clone())
            .save_cu_progress(
                Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0)
                    .unwrap()
                    .timestamp_millis(),
            )
            .await?;

        let second_run = generator
            .run_once_at(Utc.with_ymd_and_hms(2026, 3, 23, 11, 10, 0).unwrap())
            .await;
        assert!(second_run.is_ok());

        let batches = api.submitted_batches.lock().await;
        assert_eq!(batches.len(), 2);
        assert_eq!(batches[0][0].idempotency_key, batches[1][0].idempotency_key);
        Ok(())
    }

    #[tokio::test]
    async fn slash_events_are_replayed_from_persisted_queue() -> Result<()> {
        let repo = create_repo().await?;
        let detected_at = Utc.with_ymd_and_hms(2026, 3, 23, 12, 15, 0).unwrap();

        repo.record_slash_event(SlashEventRequest {
            rental_id: "rental-1".to_string(),
            node_id: "node-1".to_string(),
            reason: "Health check timeout".to_string(),
            detected_at_ms: detected_at.timestamp_millis(),
        })
        .await?;

        let pending = repo
            .list_unprocessed_slash_events((detected_at + Duration::minutes(1)).timestamp_millis())
            .await?;
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].node_id, "node-1");
        Ok(())
    }

    async fn setup_miner_with_slash_event(
        persistence: &crate::persistence::SimplePersistence,
    ) -> Result<()> {
        let start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 5, 0).unwrap();
        sqlx::query(
            "INSERT INTO miners (id, hotkey, endpoint, updated_at)
             VALUES ('miner_7', 'hotkey-7', 'http://127.0.0.1:1234', datetime('now'))",
        )
        .execute(persistence.pool())
        .await?;
        sqlx::query(
            "INSERT INTO miner_nodes (id, miner_id, node_id, ssh_endpoint, node_ip, gpu_count, hourly_rate_cents, gpu_category, status, bid_active, created_at)
             VALUES ('miner_7_node-1', 'miner_7', 'node-1', 'root@127.0.0.1:22', '127.0.0.1', 1, 100, 'H100', 'online', 1, datetime('now'))",
        )
        .execute(persistence.pool())
        .await?;
        sqlx::query(
            "INSERT INTO availability_log (miner_uid, hotkey, node_id, is_available, is_rented, is_validated, source, row_effective_at, row_expiration_at, is_current)
             VALUES (7, 'hotkey-7', 'node-1', 1, 1, 1, 'validation', ?, NULL, 1)",
        )
        .bind(start.timestamp_millis())
        .execute(persistence.pool())
        .await?;

        let repo = IncentiveStateRepository::new(persistence.pool().clone());
        let detected_at = Utc.with_ymd_and_hms(2026, 3, 23, 10, 30, 0).unwrap();
        repo.record_slash_event(SlashEventRequest {
            rental_id: "rental-1".to_string(),
            node_id: "node-1".to_string(),
            reason: "Health check timeout".to_string(),
            detected_at_ms: detected_at.timestamp_millis(),
        })
        .await?;
        Ok(())
    }

    #[tokio::test]
    async fn soft_mode_skips_api_call_and_records_to_db() -> Result<()> {
        let persistence = crate::persistence::SimplePersistence::for_testing().await?;
        let api = Arc::new(FakeIncentiveApi::default());
        let generator = CuGenerator::new(persistence.pool().clone(), api.clone(), SlashMode::Soft);

        setup_miner_with_slash_event(&persistence).await?;

        let summary = generator
            .run_once_at(Utc.with_ymd_and_hms(2026, 3, 23, 11, 5, 0).unwrap())
            .await?;

        assert_eq!(summary.slash_events_processed, 1);
        assert!(
            api.slash_calls.lock().await.is_empty(),
            "soft mode must not call slash API"
        );

        let row = sqlx::query(
            "SELECT slash_mode, applied_slash_pct, processed_at_ms
             FROM incentive_slash_events WHERE rental_id = 'rental-1'",
        )
        .fetch_one(persistence.pool())
        .await?;
        assert_eq!(row.get::<String, _>("slash_mode"), "soft");
        assert_eq!(row.get::<i64, _>("applied_slash_pct"), 100);
        assert!(row.get::<Option<i64>, _>("processed_at_ms").is_some());
        Ok(())
    }

    #[tokio::test]
    async fn hard_mode_calls_api_and_records_to_db() -> Result<()> {
        let persistence = crate::persistence::SimplePersistence::for_testing().await?;
        let api = Arc::new(FakeIncentiveApi::default());
        let generator = CuGenerator::new(persistence.pool().clone(), api.clone(), SlashMode::Hard);

        setup_miner_with_slash_event(&persistence).await?;

        let summary = generator
            .run_once_at(Utc.with_ymd_and_hms(2026, 3, 23, 11, 5, 0).unwrap())
            .await?;

        assert_eq!(summary.slash_events_processed, 1);
        let slash_calls = api.slash_calls.lock().await;
        assert_eq!(slash_calls.len(), 1);
        assert_eq!(slash_calls[0], "node-1");

        let row = sqlx::query(
            "SELECT slash_mode, applied_slash_pct, processed_at_ms
             FROM incentive_slash_events WHERE rental_id = 'rental-1'",
        )
        .fetch_one(persistence.pool())
        .await?;
        assert_eq!(row.get::<String, _>("slash_mode"), "hard");
        assert_eq!(row.get::<i64, _>("applied_slash_pct"), 100);
        assert!(row.get::<Option<i64>, _>("processed_at_ms").is_some());
        Ok(())
    }

    fn availability_row_for(
        hotkey: &str,
        miner_uid: u16,
        node_id: &str,
        effective_at: DateTime<Utc>,
        expiration_at: Option<DateTime<Utc>>,
        is_available: bool,
        is_rented: bool,
    ) -> AvailabilityLogRow {
        AvailabilityLogRow {
            miner_uid,
            hotkey: hotkey.to_string(),
            node_id: node_id.to_string(),
            is_available,
            is_rented,
            is_validated: true,
            source: "validation".to_string(),
            source_metadata: None,
            row_effective_at: effective_at.timestamp_millis(),
            row_expiration_at: expiration_at.map(|value| value.timestamp_millis()),
            is_current: expiration_at.is_none(),
        }
    }

    fn single_node_metadata(
        gpu_category: &str,
        gpu_count: u32,
    ) -> HashMap<(String, String), NodeIncentiveMetadata> {
        HashMap::from([(
            ("hotkey-7".to_string(), "node-1".to_string()),
            NodeIncentiveMetadata {
                gpu_category: gpu_category.to_string(),
                gpu_count,
            },
        )])
    }

    #[test]
    fn fully_available_single_node_earns_full_cus() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![availability_row(window_start, None, true, false)];
        let metadata = single_node_metadata("H100", 8);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 1);
        assert_eq!(windows[0].rows.len(), 1);
        assert_eq!(windows[0].rows[0].cu_amount, Decimal::from(8));
        Ok(())
    }

    #[test]
    fn partially_available_node_earns_proportional_cus() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![
            availability_row(
                window_start,
                Some(window_start + Duration::minutes(30)),
                true,
                false,
            ),
            availability_row(window_start + Duration::minutes(30), None, false, false),
        ];
        let metadata = single_node_metadata("H100", 1);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 1);
        assert_eq!(windows[0].rows.len(), 1);
        assert_eq!(
            windows[0].rows[0].cu_amount,
            Decimal::from_str("0.5").unwrap()
        );
        Ok(())
    }

    #[test]
    fn unavailable_node_earns_zero_cus() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![availability_row(
            window_start - Duration::minutes(10),
            None,
            false,
            false,
        )];
        let metadata = single_node_metadata("H100", 8);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 1);
        assert_eq!(windows[0].rows.len(), 0);
        Ok(())
    }

    #[test]
    fn multiple_nodes_generate_separate_cu_rows() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![
            availability_row_for("hotkey-7", 7, "node-1", window_start, None, true, false),
            availability_row_for("hotkey-7", 7, "node-2", window_start, None, true, false),
        ];
        let metadata = HashMap::from([
            (
                ("hotkey-7".to_string(), "node-1".to_string()),
                NodeIncentiveMetadata {
                    gpu_category: "H100".to_string(),
                    gpu_count: 8,
                },
            ),
            (
                ("hotkey-7".to_string(), "node-2".to_string()),
                NodeIncentiveMetadata {
                    gpu_category: "H100".to_string(),
                    gpu_count: 4,
                },
            ),
        ]);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 1);
        assert_eq!(windows[0].rows.len(), 2);

        let mut rows_by_node: HashMap<&str, &NewCuLedgerRowRequest> = HashMap::new();
        for row in &windows[0].rows {
            rows_by_node.insert(&row.node_id, row);
        }
        assert_eq!(rows_by_node["node-1"].cu_amount, Decimal::from(8));
        assert_eq!(rows_by_node["node-2"].cu_amount, Decimal::from(4));
        Ok(())
    }

    #[test]
    fn node_with_unknown_gpu_category_is_skipped() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![availability_row(window_start, None, true, false)];
        let metadata = single_node_metadata("B200", 8);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 1);
        assert_eq!(windows[0].rows.len(), 0);
        Ok(())
    }

    #[test]
    fn availability_starting_mid_window_clips_to_window_boundary() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![availability_row(
            window_start + Duration::minutes(15),
            None,
            true,
            false,
        )];
        let metadata = single_node_metadata("H100", 1);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 1);
        assert_eq!(windows[0].rows.len(), 1);
        assert_eq!(
            windows[0].rows[0].cu_amount,
            Decimal::from_str("0.75").unwrap()
        );
        Ok(())
    }

    #[test]
    fn availability_ending_mid_window_clips_to_expiration() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![
            availability_row(
                window_start - Duration::minutes(10),
                Some(window_start + Duration::minutes(40)),
                true,
                false,
            ),
            availability_row(window_start + Duration::minutes(40), None, false, false),
        ];
        let metadata = single_node_metadata("H100", 1);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 1);
        assert_eq!(windows[0].rows.len(), 1);
        assert_eq!(
            windows[0].rows[0].cu_amount.round_dp(18),
            Decimal::from_str("0.666666666666666667").unwrap()
        );
        Ok(())
    }

    #[test]
    fn intermittent_availability_sums_all_available_segments() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![
            availability_row(
                window_start,
                Some(window_start + Duration::minutes(20)),
                true,
                false,
            ),
            availability_row(
                window_start + Duration::minutes(20),
                Some(window_start + Duration::minutes(40)),
                false,
                false,
            ),
            availability_row(window_start + Duration::minutes(40), None, true, false),
        ];
        let metadata = single_node_metadata("H100", 1);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 1);
        assert_eq!(windows[0].rows.len(), 1);
        assert_eq!(
            windows[0].rows[0].cu_amount.round_dp(18),
            Decimal::from_str("0.666666666666666667").unwrap()
        );
        Ok(())
    }

    #[test]
    fn multi_hour_catchup_generates_correct_windows() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(3);
        let rows = vec![availability_row(window_start, None, true, false)];
        let metadata = single_node_metadata("H100", 8);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 3);
        for window in &windows {
            assert_eq!(window.rows.len(), 1);
            assert_eq!(window.rows[0].cu_amount, Decimal::from(8));
        }
        Ok(())
    }

    #[test]
    fn is_rented_flag_is_passed_through_but_does_not_affect_cu_amount() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![availability_row(window_start, None, true, true)];
        let metadata = single_node_metadata("H100", 8);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        assert_eq!(windows.len(), 1);
        assert_eq!(windows[0].rows.len(), 1);
        assert_eq!(windows[0].rows[0].cu_amount, Decimal::from(8));
        assert!(windows[0].rows[0].is_rented);
        Ok(())
    }

    #[test]
    fn snapshotted_config_values_on_cu_rows() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![availability_row(window_start, None, true, false)];
        let metadata = single_node_metadata("H100", 1);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        let cu_row = &windows[0].rows[0];
        assert_eq!(cu_row.window_hours, 72);
        assert_eq!(cu_row.price_usd, Decimal::from_str("3.00").unwrap());
        assert_eq!(cu_row.gpu_category, "H100");
        Ok(())
    }

    #[test]
    fn idempotency_key_format() -> Result<()> {
        let window_start = Utc.with_ymd_and_hms(2026, 3, 23, 10, 0, 0).unwrap();
        let window_end = window_start + Duration::hours(1);
        let rows = vec![availability_row(window_start, None, true, false)];
        let metadata = single_node_metadata("H100", 1);

        let windows = generate_hourly_cu_windows(
            Some(window_start.timestamp_millis()),
            window_end.timestamp_millis(),
            &rows,
            &metadata,
            &[],
            &test_config(),
        )?;

        let expected_key = format!("node-1:{}", window_end.timestamp_millis() / 1000);
        assert_eq!(windows[0].rows[0].idempotency_key, expected_key);
        Ok(())
    }
}
