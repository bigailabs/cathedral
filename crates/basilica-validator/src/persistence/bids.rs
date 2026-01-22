use chrono::{DateTime, Utc};
use sqlx::{QueryBuilder, Row, Sqlite, SqlitePool, Transaction};

#[derive(Debug, Clone, PartialEq)]
pub struct MinerBidRecord {
    pub id: String,
    pub miner_hotkey: String,
    pub miner_uid: i64,
    pub gpu_category: String,
    pub bid_per_hour: f64,
    pub gpu_count: i64,
    pub attestation: Option<Vec<u8>>,
    pub signature: Vec<u8>,
    pub nonce: String,
    pub submitted_at: DateTime<Utc>,
    pub epoch_id: String,
    pub is_valid: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AuctionEpoch {
    pub id: String,
    pub start_block: i64,
    pub end_block: Option<i64>,
    pub baseline_prices_json: String,
    pub status: String,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AuctionClearingResult {
    pub id: String,
    pub epoch_id: String,
    pub gpu_category: String,
    pub baseline_price: f64,
    pub clearing_price: Option<f64>,
    pub total_capacity: i64,
    pub winners_count: i64,
    pub cleared_at: DateTime<Utc>,
}

#[derive(Clone)]
pub struct BidRepository {
    pool: SqlitePool,
}

impl BidRepository {
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }

    pub async fn create_epoch(&self, epoch: &AuctionEpoch) -> Result<(), sqlx::Error> {
        sqlx::query(
            r#"
            INSERT INTO auction_epochs (id, start_block, end_block, baseline_prices_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            "#,
        )
        .bind(&epoch.id)
        .bind(epoch.start_block)
        .bind(epoch.end_block)
        .bind(&epoch.baseline_prices_json)
        .bind(&epoch.status)
        .bind(epoch.created_at.to_rfc3339())
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn get_active_epoch(&self) -> Result<Option<AuctionEpoch>, sqlx::Error> {
        let row = sqlx::query(
            r#"
            SELECT id, start_block, end_block, baseline_prices_json, status, created_at
            FROM auction_epochs
            WHERE status = 'active'
            ORDER BY created_at DESC
            LIMIT 1
            "#,
        )
        .fetch_optional(&self.pool)
        .await?;

        let epoch = match row {
            Some(r) => {
                let created_at = DateTime::parse_from_rfc3339(&r.get::<String, _>("created_at"))
                    .map_err(|e| sqlx::Error::Decode(Box::new(e)))?
                    .with_timezone(&Utc);
                Some(AuctionEpoch {
                    id: r.get("id"),
                    start_block: r.get("start_block"),
                    end_block: r.get("end_block"),
                    baseline_prices_json: r.get("baseline_prices_json"),
                    status: r.get("status"),
                    created_at,
                })
            }
            None => None,
        };
        Ok(epoch)
    }

    pub async fn update_epoch_status(
        &self,
        epoch_id: &str,
        status: &str,
        end_block: Option<i64>,
    ) -> Result<(), sqlx::Error> {
        sqlx::query(
            r#"
            UPDATE auction_epochs
            SET status = ?, end_block = ?
            WHERE id = ?
            "#,
        )
        .bind(status)
        .bind(end_block)
        .bind(epoch_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn insert_bid(&self, bid: &MinerBidRecord) -> Result<(), sqlx::Error> {
        sqlx::query(
            r#"
            INSERT INTO miner_bids
            (id, miner_hotkey, miner_uid, gpu_category, bid_per_hour, gpu_count, attestation, signature, nonce, submitted_at, epoch_id, is_valid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            "#,
        )
        .bind(&bid.id)
        .bind(&bid.miner_hotkey)
        .bind(bid.miner_uid)
        .bind(&bid.gpu_category)
        .bind(bid.bid_per_hour)
        .bind(bid.gpu_count)
        .bind(&bid.attestation)
        .bind(&bid.signature)
        .bind(&bid.nonce)
        .bind(bid.submitted_at.to_rfc3339())
        .bind(&bid.epoch_id)
        .bind(bid.is_valid as i32)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn insert_bid_tx(
        &self,
        tx: &mut Transaction<'_, Sqlite>,
        bid: &MinerBidRecord,
    ) -> Result<(), sqlx::Error> {
        sqlx::query(
            r#"
            INSERT INTO miner_bids
            (id, miner_hotkey, miner_uid, gpu_category, bid_per_hour, gpu_count, attestation, signature, nonce, submitted_at, epoch_id, is_valid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            "#,
        )
        .bind(&bid.id)
        .bind(&bid.miner_hotkey)
        .bind(bid.miner_uid)
        .bind(&bid.gpu_category)
        .bind(bid.bid_per_hour)
        .bind(bid.gpu_count)
        .bind(&bid.attestation)
        .bind(&bid.signature)
        .bind(&bid.nonce)
        .bind(bid.submitted_at.to_rfc3339())
        .bind(&bid.epoch_id)
        .bind(bid.is_valid as i32)
        .execute(tx.as_mut())
        .await?;
        Ok(())
    }

    pub async fn insert_bid_nodes(
        &self,
        bid_id: &str,
        miner_id: &str,
        gpu_category: &str,
        gpu_count: i64,
        node_ids: &[String],
        snapshot_at: DateTime<Utc>,
    ) -> Result<(), sqlx::Error> {
        if node_ids.is_empty() {
            return Ok(());
        }

        let snapshot_at = snapshot_at.to_rfc3339();
        let mut query_builder = QueryBuilder::<Sqlite>::new(
            "INSERT INTO miner_bid_nodes (bid_id, node_id, miner_id, gpu_category, gpu_count, snapshot_at) ",
        );
        query_builder.push_values(node_ids, |mut row, node_id| {
            row.push_bind(bid_id)
                .push_bind(node_id)
                .push_bind(miner_id)
                .push_bind(gpu_category)
                .push_bind(gpu_count)
                .push_bind(&snapshot_at);
        });

        query_builder.build().execute(&self.pool).await?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn insert_bid_nodes_tx(
        &self,
        tx: &mut Transaction<'_, Sqlite>,
        bid_id: &str,
        miner_id: &str,
        gpu_category: &str,
        gpu_count: i64,
        node_ids: &[String],
        snapshot_at: DateTime<Utc>,
    ) -> Result<(), sqlx::Error> {
        if node_ids.is_empty() {
            return Ok(());
        }

        let snapshot_at = snapshot_at.to_rfc3339();
        let mut query_builder = QueryBuilder::<Sqlite>::new(
            "INSERT INTO miner_bid_nodes (bid_id, node_id, miner_id, gpu_category, gpu_count, snapshot_at) ",
        );
        query_builder.push_values(node_ids, |mut row, node_id| {
            row.push_bind(bid_id)
                .push_bind(node_id)
                .push_bind(miner_id)
                .push_bind(gpu_category)
                .push_bind(gpu_count)
                .push_bind(&snapshot_at);
        });

        query_builder.build().execute(tx.as_mut()).await?;
        Ok(())
    }

    pub async fn list_bids_for_epoch_category(
        &self,
        epoch_id: &str,
        gpu_category: &str,
    ) -> Result<Vec<MinerBidRecord>, sqlx::Error> {
        let rows = sqlx::query(
            r#"
            SELECT id, miner_hotkey, miner_uid, gpu_category, bid_per_hour, gpu_count, attestation, signature, nonce, submitted_at, epoch_id, is_valid
            FROM miner_bids
            WHERE epoch_id = ? AND gpu_category = ?
            ORDER BY submitted_at ASC
            "#,
        )
        .bind(epoch_id)
        .bind(gpu_category)
        .fetch_all(&self.pool)
        .await?;

        let bids = rows
            .into_iter()
            .map(|r| {
                let submitted_at =
                    DateTime::parse_from_rfc3339(&r.get::<String, _>("submitted_at"))
                        .map_err(|e| sqlx::Error::Decode(Box::new(e)))?
                        .with_timezone(&Utc);
                Ok(MinerBidRecord {
                    id: r.get("id"),
                    miner_hotkey: r.get("miner_hotkey"),
                    miner_uid: r.get("miner_uid"),
                    gpu_category: r.get("gpu_category"),
                    bid_per_hour: r.get("bid_per_hour"),
                    gpu_count: r.get("gpu_count"),
                    attestation: r.get("attestation"),
                    signature: r.get("signature"),
                    nonce: r.get("nonce"),
                    submitted_at,
                    epoch_id: r.get("epoch_id"),
                    is_valid: r.get::<i32, _>("is_valid") != 0,
                })
            })
            .collect::<Result<Vec<_>, sqlx::Error>>()?;
        Ok(bids)
    }

    pub async fn insert_clearing_result(
        &self,
        result: &AuctionClearingResult,
    ) -> Result<(), sqlx::Error> {
        sqlx::query(
            r#"
            INSERT INTO auction_clearing_results
            (id, epoch_id, gpu_category, baseline_price, clearing_price, total_capacity, winners_count, cleared_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            "#,
        )
        .bind(&result.id)
        .bind(&result.epoch_id)
        .bind(&result.gpu_category)
        .bind(result.baseline_price)
        .bind(result.clearing_price)
        .bind(result.total_capacity)
        .bind(result.winners_count)
        .bind(result.cleared_at.to_rfc3339())
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn get_lowest_bid_for_category(
        &self,
        epoch_id: &str,
        gpu_category: &str,
        min_gpu_count: u32,
        submitted_after: DateTime<Utc>,
    ) -> Result<Option<MinerBidRecord>, sqlx::Error> {
        let row = sqlx::query(
            r#"
            SELECT id, miner_hotkey, miner_uid, gpu_category, bid_per_hour, gpu_count,
                   attestation, signature, nonce, submitted_at, epoch_id, is_valid
            FROM miner_bids
            WHERE epoch_id = ? AND gpu_category = ? AND is_valid = 1
              AND gpu_count >= ? AND submitted_at >= ?
            ORDER BY bid_per_hour ASC, submitted_at ASC
            LIMIT 1
            "#,
        )
        .bind(epoch_id)
        .bind(gpu_category)
        .bind(min_gpu_count as i64)
        .bind(submitted_after.to_rfc3339())
        .fetch_optional(&self.pool)
        .await?;

        let record = match row {
            Some(r) => {
                let submitted_at =
                    DateTime::parse_from_rfc3339(&r.get::<String, _>("submitted_at"))
                        .map_err(|e| sqlx::Error::Decode(Box::new(e)))?
                        .with_timezone(&Utc);
                Some(MinerBidRecord {
                    id: r.get("id"),
                    miner_hotkey: r.get("miner_hotkey"),
                    miner_uid: r.get("miner_uid"),
                    gpu_category: r.get("gpu_category"),
                    bid_per_hour: r.get("bid_per_hour"),
                    gpu_count: r.get("gpu_count"),
                    attestation: r.get("attestation"),
                    signature: r.get("signature"),
                    nonce: r.get("nonce"),
                    submitted_at,
                    epoch_id: r.get("epoch_id"),
                    is_valid: r.get::<i32, _>("is_valid") != 0,
                })
            }
            None => None,
        };
        Ok(record)
    }

    pub async fn get_lowest_bid_with_available_node(
        &self,
        epoch_id: &str,
        gpu_category: &str,
        min_gpu_count: u32,
        submitted_after: DateTime<Utc>,
        freshness_secs: u64,
    ) -> Result<Option<(MinerBidRecord, String)>, sqlx::Error> {
        let query = format!(
            r#"
            SELECT b.id, b.miner_hotkey, b.miner_uid, b.gpu_category, b.bid_per_hour, b.gpu_count,
                   b.attestation, b.signature, b.nonce, b.submitted_at, b.epoch_id, b.is_valid,
                   bn.node_id
            FROM miner_bids b
            JOIN miner_bid_nodes bn ON b.id = bn.bid_id
            JOIN miner_nodes me ON bn.node_id = me.node_id AND bn.miner_id = me.miner_id
            LEFT JOIN rentals r ON me.node_id = r.node_id
                AND r.miner_id = me.miner_id
                AND r.state IN ('Active', 'Provisioning', 'active', 'provisioning')
            LEFT JOIN node_reservations nr ON me.node_id = nr.node_id
                AND datetime(nr.expires_at) > datetime('now')
            WHERE b.epoch_id = ? AND b.gpu_category = ? AND b.is_valid = 1
              AND b.gpu_count >= ? AND b.submitted_at >= ?
              AND r.id IS NULL
              AND nr.id IS NULL
              AND (me.status IS NULL OR me.status != 'offline')
              AND me.last_health_check IS NOT NULL
              AND datetime(me.last_health_check) >= datetime('now', '-{freshness_secs} seconds')
            ORDER BY b.bid_per_hour ASC, b.submitted_at ASC
            LIMIT 1
            "#
        );

        let row = sqlx::query(&query)
            .bind(epoch_id)
            .bind(gpu_category)
            .bind(min_gpu_count as i64)
            .bind(submitted_after.to_rfc3339())
            .fetch_optional(&self.pool)
            .await?;

        let record = match row {
            Some(r) => {
                let submitted_at =
                    DateTime::parse_from_rfc3339(&r.get::<String, _>("submitted_at"))
                        .map_err(|e| sqlx::Error::Decode(Box::new(e)))?
                        .with_timezone(&Utc);
                let record = MinerBidRecord {
                    id: r.get("id"),
                    miner_hotkey: r.get("miner_hotkey"),
                    miner_uid: r.get("miner_uid"),
                    gpu_category: r.get("gpu_category"),
                    bid_per_hour: r.get("bid_per_hour"),
                    gpu_count: r.get("gpu_count"),
                    attestation: r.get("attestation"),
                    signature: r.get("signature"),
                    nonce: r.get("nonce"),
                    submitted_at,
                    epoch_id: r.get("epoch_id"),
                    is_valid: r.get::<i32, _>("is_valid") != 0,
                };
                let node_id: String = r.get("node_id");
                Some((record, node_id))
            }
            None => None,
        };
        Ok(record)
    }

    pub async fn expire_old_bids(&self, before: DateTime<Utc>) -> Result<(), sqlx::Error> {
        sqlx::query(
            r#"
            UPDATE miner_bids
            SET is_valid = 0
            WHERE submitted_at < ?
            "#,
        )
        .bind(before.to_rfc3339())
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn delete_bids_before(&self, before: DateTime<Utc>) -> Result<u64, sqlx::Error> {
        let nodes_result = sqlx::query(
            r#"
            DELETE FROM miner_bid_nodes
            WHERE bid_id IN (
                SELECT id FROM miner_bids WHERE submitted_at < ?
            )
            "#,
        )
        .bind(before.to_rfc3339())
        .execute(&self.pool)
        .await?;

        let bids_result = sqlx::query(
            r#"
            DELETE FROM miner_bids
            WHERE submitted_at < ?
            "#,
        )
        .bind(before.to_rfc3339())
        .execute(&self.pool)
        .await?;
        Ok(nodes_result.rows_affected() + bids_result.rows_affected())
    }

    pub async fn delete_clearing_results_before(
        &self,
        before: DateTime<Utc>,
    ) -> Result<u64, sqlx::Error> {
        let result = sqlx::query(
            r#"
            DELETE FROM auction_clearing_results
            WHERE cleared_at < ?
            "#,
        )
        .bind(before.to_rfc3339())
        .execute(&self.pool)
        .await?;
        Ok(result.rows_affected())
    }

    pub async fn delete_epochs_before(&self, before: DateTime<Utc>) -> Result<u64, sqlx::Error> {
        let result = sqlx::query(
            r#"
            DELETE FROM auction_epochs
            WHERE status != 'active' AND created_at < ?
            "#,
        )
        .bind(before.to_rfc3339())
        .execute(&self.pool)
        .await?;
        Ok(result.rows_affected())
    }

    pub async fn nonce_exists(
        &self,
        miner_hotkey: &str,
        nonce: &str,
        submitted_after: DateTime<Utc>,
    ) -> Result<bool, sqlx::Error> {
        let row = sqlx::query(
            r#"
            SELECT 1
            FROM miner_bids
            WHERE miner_hotkey = ? AND nonce = ? AND submitted_at >= ?
            LIMIT 1
            "#,
        )
        .bind(miner_hotkey)
        .bind(nonce)
        .bind(submitted_after.to_rfc3339())
        .fetch_optional(&self.pool)
        .await?;

        Ok(row.is_some())
    }

    pub async fn nonce_exists_tx(
        &self,
        tx: &mut Transaction<'_, Sqlite>,
        miner_hotkey: &str,
        nonce: &str,
        submitted_after: DateTime<Utc>,
    ) -> Result<bool, sqlx::Error> {
        let row = sqlx::query(
            r#"
            SELECT 1
            FROM miner_bids
            WHERE miner_hotkey = ? AND nonce = ? AND submitted_at >= ?
            LIMIT 1
            "#,
        )
        .bind(miner_hotkey)
        .bind(nonce)
        .bind(submitted_after.to_rfc3339())
        .fetch_optional(tx.as_mut())
        .await?;

        Ok(row.is_some())
    }

    pub async fn get_lowest_bid_for_miner(
        &self,
        epoch_id: &str,
        gpu_category: &str,
        miner_uid: i64,
        submitted_after: DateTime<Utc>,
    ) -> Result<Option<MinerBidRecord>, sqlx::Error> {
        let row = sqlx::query(
            r#"
            SELECT id, miner_hotkey, miner_uid, gpu_category, bid_per_hour, gpu_count,
                   attestation, signature, nonce, submitted_at, epoch_id, is_valid
            FROM miner_bids
            WHERE epoch_id = ? AND gpu_category = ? AND is_valid = 1
              AND miner_uid = ? AND submitted_at >= ?
            ORDER BY bid_per_hour ASC, submitted_at ASC
            LIMIT 1
            "#,
        )
        .bind(epoch_id)
        .bind(gpu_category)
        .bind(miner_uid)
        .bind(submitted_after.to_rfc3339())
        .fetch_optional(&self.pool)
        .await?;

        let record = match row {
            Some(r) => {
                let submitted_at =
                    DateTime::parse_from_rfc3339(&r.get::<String, _>("submitted_at"))
                        .map_err(|e| sqlx::Error::Decode(Box::new(e)))?
                        .with_timezone(&Utc);
                Some(MinerBidRecord {
                    id: r.get("id"),
                    miner_hotkey: r.get("miner_hotkey"),
                    miner_uid: r.get("miner_uid"),
                    gpu_category: r.get("gpu_category"),
                    bid_per_hour: r.get("bid_per_hour"),
                    gpu_count: r.get("gpu_count"),
                    attestation: r.get("attestation"),
                    signature: r.get("signature"),
                    nonce: r.get("nonce"),
                    submitted_at,
                    epoch_id: r.get("epoch_id"),
                    is_valid: r.get::<i32, _>("is_valid") != 0,
                })
            }
            None => None,
        };
        Ok(record)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::persistence::SimplePersistence;

    fn sample_epoch() -> AuctionEpoch {
        AuctionEpoch {
            id: "epoch-1".to_string(),
            start_block: 100,
            end_block: None,
            baseline_prices_json: r#"{"H100":2.0}"#.to_string(),
            status: "active".to_string(),
            created_at: Utc::now(),
        }
    }

    fn sample_bid(epoch_id: &str) -> MinerBidRecord {
        MinerBidRecord {
            id: "bid-1".to_string(),
            miner_hotkey: "miner_hotkey".to_string(),
            miner_uid: 42,
            gpu_category: "H100".to_string(),
            bid_per_hour: 2.5,
            gpu_count: 2,
            attestation: Some(vec![1, 2, 3]),
            signature: vec![9, 9, 9],
            nonce: "nonce-1".to_string(),
            submitted_at: Utc::now(),
            epoch_id: epoch_id.to_string(),
            is_valid: true,
        }
    }

    fn sample_clearing(epoch_id: &str) -> AuctionClearingResult {
        AuctionClearingResult {
            id: "clear-1".to_string(),
            epoch_id: epoch_id.to_string(),
            gpu_category: "H100".to_string(),
            baseline_price: 2.0,
            clearing_price: Some(2.3),
            total_capacity: 8,
            winners_count: 2,
            cleared_at: Utc::now(),
        }
    }

    #[tokio::test]
    async fn test_epoch_insert_and_get_active() {
        let persistence = SimplePersistence::for_testing().await.unwrap();
        let repo = BidRepository::new(persistence.pool().clone());

        let epoch = sample_epoch();
        repo.create_epoch(&epoch).await.unwrap();

        let active = repo.get_active_epoch().await.unwrap().unwrap();
        assert_eq!(active.id, epoch.id);
        assert_eq!(active.status, "active");
    }

    #[tokio::test]
    async fn test_update_epoch_status() {
        let persistence = SimplePersistence::for_testing().await.unwrap();
        let repo = BidRepository::new(persistence.pool().clone());

        let epoch = sample_epoch();
        repo.create_epoch(&epoch).await.unwrap();

        repo.update_epoch_status(&epoch.id, "cleared", Some(120))
            .await
            .unwrap();

        let active = repo.get_active_epoch().await.unwrap();
        assert!(active.is_none());
    }

    #[tokio::test]
    async fn test_insert_and_list_bids() {
        let persistence = SimplePersistence::for_testing().await.unwrap();
        let repo = BidRepository::new(persistence.pool().clone());

        let epoch = sample_epoch();
        repo.create_epoch(&epoch).await.unwrap();

        let bid = sample_bid(&epoch.id);
        repo.insert_bid(&bid).await.unwrap();

        let bids = repo
            .list_bids_for_epoch_category(&epoch.id, "H100")
            .await
            .unwrap();
        assert_eq!(bids.len(), 1);
        assert_eq!(bids[0].miner_uid, 42);
    }

    #[tokio::test]
    async fn test_duplicate_bid_rejected() {
        let persistence = SimplePersistence::for_testing().await.unwrap();
        let repo = BidRepository::new(persistence.pool().clone());

        let epoch = sample_epoch();
        repo.create_epoch(&epoch).await.unwrap();

        let bid = sample_bid(&epoch.id);
        repo.insert_bid(&bid).await.unwrap();
        let duplicate = repo.insert_bid(&bid).await;
        assert!(duplicate.is_err());
    }

    #[tokio::test]
    async fn test_insert_clearing_result() {
        let persistence = SimplePersistence::for_testing().await.unwrap();
        let repo = BidRepository::new(persistence.pool().clone());

        let epoch = sample_epoch();
        repo.create_epoch(&epoch).await.unwrap();

        let clearing = sample_clearing(&epoch.id);
        repo.insert_clearing_result(&clearing).await.unwrap();
    }
}
