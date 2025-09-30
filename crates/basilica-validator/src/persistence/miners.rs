//! Miner persistence operations
//!
//! This module contains all SQL operations related to miners table management.

use crate::miner_prover::types::MinerInfo;
use crate::persistence::SimplePersistence;
use anyhow::Result;
use sqlx::Row;
use tracing::{debug, error, info, warn};

impl SimplePersistence {
    /// Check if a miner with the given UID exists
    pub async fn check_miner_by_uid(&self, miner_uid: &str) -> Result<Option<(String, String)>> {
        let query = "SELECT id, hotkey FROM miners WHERE id = ?";
        let result = sqlx::query(query)
            .bind(miner_uid)
            .fetch_optional(self.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check miner by uid: {}", e))?;

        Ok(result.map(|row| {
            let id: String = row.get("id");
            let hotkey: String = row.get("hotkey");
            (id, hotkey)
        }))
    }

    /// Check if a miner with the given hotkey exists
    pub async fn check_miner_by_hotkey(&self, hotkey: &str) -> Result<Option<String>> {
        let query = "SELECT id FROM miners WHERE hotkey = ?";
        let result = sqlx::query(query)
            .bind(hotkey)
            .fetch_optional(self.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check miner by hotkey: {}", e))?;

        Ok(result.map(|row| row.get("id")))
    }

    /// Create a new miner record
    pub async fn create_new_miner(
        &self,
        miner_uid: &str,
        hotkey: &str,
        miner_info: &MinerInfo,
    ) -> Result<()> {
        let insert_query = r#"
            INSERT INTO miners (
                id, hotkey, endpoint, verification_score, uptime_percentage,
                last_seen, registered_at, updated_at, node_info
            ) VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'), ?)
        "#;

        sqlx::query(insert_query)
            .bind(miner_uid)
            .bind(hotkey)
            .bind(&miner_info.endpoint)
            .bind(miner_info.verification_score)
            .bind(100.0)
            .bind("{}")
            .execute(self.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to insert miner: {}", e))?;

        info!(
            "Created miner record: {} with hotkey {} and endpoint {}",
            miner_uid, hotkey, miner_info.endpoint
        );

        Ok(())
    }

    /// Update existing miner data
    pub async fn update_miner_data(&self, miner_id: &str, miner_info: &MinerInfo) -> Result<()> {
        let update_query = r#"
            UPDATE miners SET
                endpoint = ?, verification_score = ?,
                last_seen = datetime('now'), updated_at = datetime('now')
            WHERE id = ?
        "#;

        sqlx::query(update_query)
            .bind(&miner_info.endpoint)
            .bind(miner_info.verification_score)
            .bind(miner_id)
            .execute(self.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to update miner: {}", e))?;

        debug!("Updated miner record: {} with latest data", miner_id);
        Ok(())
    }

    /// Handle case where miner UID already exists
    pub async fn handle_recycled_miner_uid(
        &self,
        miner_uid: &str,
        new_hotkey: &str,
        existing_hotkey: &str,
        miner_info: &MinerInfo,
    ) -> Result<()> {
        if existing_hotkey != new_hotkey {
            info!(
                miner_uid = miner_uid,
                "Miner {} exists with old hotkey {}, updating to new hotkey {}",
                miner_uid,
                existing_hotkey,
                new_hotkey
            );

            let update_query = r#"
                UPDATE miners SET
                    hotkey = ?, endpoint = ?, verification_score = ?,
                    last_seen = datetime('now'), updated_at = datetime('now')
                WHERE id = ?
            "#;

            sqlx::query(update_query)
                .bind(new_hotkey)
                .bind(&miner_info.endpoint)
                .bind(miner_info.verification_score)
                .bind(miner_uid)
                .execute(self.pool())
                .await
                .map_err(|e| anyhow::anyhow!("Failed to update miner with new hotkey: {}", e))?;

            debug!("Updated miner {} with new hotkey and data", miner_uid);
        } else {
            self.update_miner_data(miner_uid, miner_info).await?;
        }

        Ok(())
    }

    /// Handle case where hotkey exists but with different ID (UID change)
    pub async fn handle_uid_change(
        &self,
        old_miner_id: &str,
        new_miner_id: &str,
        hotkey: &str,
        miner_info: &MinerInfo,
    ) -> Result<()> {
        info!(
            "Detected UID change for hotkey {}: {} -> {}",
            hotkey, old_miner_id, new_miner_id
        );

        if let Err(e) = self
            .migrate_miner_uid(old_miner_id, new_miner_id, miner_info)
            .await
        {
            error!(
                "Failed to migrate miner UID from {} to {}: {}",
                old_miner_id, new_miner_id, e
            );
            return Err(e);
        }

        Ok(())
    }

    /// Migrate miner UID when it changes in the network
    pub async fn migrate_miner_uid(
        &self,
        old_miner_uid: &str,
        new_miner_uid: &str,
        miner_info: &MinerInfo,
    ) -> Result<()> {
        info!(
            "Starting UID migration: {} -> {} for hotkey {}",
            old_miner_uid, new_miner_uid, miner_info.hotkey
        );

        let mut tx = self
            .pool()
            .begin()
            .await
            .map_err(|e| anyhow::anyhow!("Failed to begin transaction: {}", e))?;

        debug!("Fetching old miner record: {}", old_miner_uid);
        let get_old_miner = "SELECT * FROM miners WHERE id = ?";
        let old_miner_row = sqlx::query(get_old_miner)
            .bind(old_miner_uid)
            .fetch_optional(&mut *tx)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to fetch old miner record: {}", e))?;

        if old_miner_row.is_none() {
            return Err(anyhow::anyhow!(
                "Old miner record not found: {}",
                old_miner_uid
            ));
        }

        let old_row = old_miner_row.unwrap();
        debug!("Found old miner record for migration");

        debug!(
            "Checking for existing miners with hotkey: {}",
            miner_info.hotkey
        );
        let check_hotkey = "SELECT id FROM miners WHERE hotkey = ?";
        let all_with_hotkey = sqlx::query(check_hotkey)
            .bind(miner_info.hotkey.to_string())
            .fetch_all(&mut *tx)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check hotkey existence: {}", e))?;

        let existing_with_hotkey = all_with_hotkey.into_iter().find(|row| {
            let id: String = row.get("id");
            id != old_miner_uid
        });

        let should_create_new = if let Some(row) = existing_with_hotkey {
            let existing_id: String = row.get("id");
            debug!(
                "Found existing miner with hotkey {}: id={}",
                miner_info.hotkey, existing_id
            );
            if existing_id == new_miner_uid {
                debug!("New miner record already exists with correct ID");
                false
            } else {
                warn!(
                    "Cannot migrate: Another miner {} already exists with hotkey {} (trying to create {})",
                    existing_id, miner_info.hotkey, new_miner_uid
                );
                return Err(anyhow::anyhow!(
                    "Cannot migrate: Another miner {} already exists with hotkey {}",
                    existing_id,
                    miner_info.hotkey
                ));
            }
        } else {
            debug!(
                "No existing miner with hotkey {}, will create new record",
                miner_info.hotkey
            );
            true
        };

        let verification_score = old_row
            .try_get::<f64, _>("verification_score")
            .unwrap_or(0.0);
        let uptime_percentage = old_row
            .try_get::<f64, _>("uptime_percentage")
            .unwrap_or(100.0);
        let registered_at = old_row
            .try_get::<String, _>("registered_at")
            .unwrap_or_else(|_| chrono::Utc::now().to_rfc3339());
        let node_info = old_row
            .try_get::<String, _>("node_info")
            .unwrap_or_else(|_| "{}".to_string());

        debug!("Fetching related node data");
        let get_nodes = "SELECT * FROM miner_nodes WHERE miner_id = ?";
        let nodes = sqlx::query(get_nodes)
            .bind(old_miner_uid)
            .fetch_all(&mut *tx)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to fetch nodes: {}", e))?;

        debug!("Found {} nodes to migrate", nodes.len());

        debug!("Deleting old miner record: {}", old_miner_uid);
        let delete_old_miner = "DELETE FROM miners WHERE id = ?";
        sqlx::query(delete_old_miner)
            .bind(old_miner_uid)
            .execute(&mut *tx)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to delete old miner record: {}", e))?;

        debug!("Deleted old miner record and related data");

        if should_create_new {
            debug!("Creating new miner record: {}", new_miner_uid);
            let insert_new_miner = r#"
                INSERT INTO miners (
                    id, hotkey, endpoint, verification_score, uptime_percentage,
                    last_seen, registered_at, updated_at, node_info
                ) VALUES (?, ?, ?, ?, ?, datetime('now'), ?, datetime('now'), ?)
            "#;

            sqlx::query(insert_new_miner)
                .bind(new_miner_uid)
                .bind(miner_info.hotkey.to_string())
                .bind(&miner_info.endpoint)
                .bind(verification_score)
                .bind(uptime_percentage)
                .bind(registered_at)
                .bind(node_info)
                .execute(&mut *tx)
                .await
                .map_err(|e| anyhow::anyhow!("Failed to create new miner record: {}", e))?;

            debug!("Successfully created new miner record");
        }

        let mut node_count = 0;
        for node_row in nodes {
            let node_id: String = node_row.get("node_id");
            let ssh_endpoint: String = node_row.get("ssh_endpoint");
            let gpu_count: i32 = node_row.get("gpu_count");
            let status: String = node_row
                .try_get("status")
                .unwrap_or_else(|_| "unknown".to_string());

            let existing_check = sqlx::query(
                "SELECT COUNT(*) as count FROM miner_nodes WHERE ssh_endpoint = ? AND miner_id != ?"
            )
            .bind(&ssh_endpoint)
            .bind(new_miner_uid)
            .fetch_one(&mut *tx)
            .await?;

            let existing_count: i64 = existing_check.get("count");
            if existing_count > 0 {
                warn!(
                    "Skipping node {} during UID migration: ssh_endpoint {} already in use by another miner",
                    node_id, ssh_endpoint
                );
                continue;
            }

            let new_id = format!("{new_miner_uid}_{node_id}");

            let insert_node = r#"
                INSERT INTO miner_nodes (
                    id, miner_id, node_id, ssh_endpoint, gpu_count,
                    status, last_health_check,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, datetime('now'), datetime('now'))
            "#;

            sqlx::query(insert_node)
                .bind(&new_id)
                .bind(new_miner_uid)
                .bind(&node_id)
                .bind(&ssh_endpoint)
                .bind(gpu_count)
                .bind(&status)
                .execute(&mut *tx)
                .await
                .map_err(|e| anyhow::anyhow!("Failed to recreate node relationship: {}", e))?;

            node_count += 1;
        }

        debug!("Recreated {} node relationships", node_count);

        debug!(
            "Migrating GPU UUID assignments from {} to {}",
            old_miner_uid, new_miner_uid
        );
        let update_gpu_assignments = r#"
            UPDATE gpu_uuid_assignments
            SET miner_id = ?
            WHERE miner_id = ?
        "#;

        let gpu_result = sqlx::query(update_gpu_assignments)
            .bind(new_miner_uid)
            .bind(old_miner_uid)
            .execute(&mut *tx)
            .await?;

        debug!(
            "Migrated {} GPU UUID assignments",
            gpu_result.rows_affected()
        );

        debug!("Committing transaction");
        tx.commit()
            .await
            .map_err(|e| anyhow::anyhow!("Failed to commit transaction: {}", e))?;

        info!(
            "Successfully migrated miner UID: {} -> {}. Migrated {} nodes",
            old_miner_uid, new_miner_uid, node_count
        );

        Ok(())
    }

    /// Ensure miner exists in miners table
    ///
    /// This function handles three scenarios:
    /// 1. if UID already exists with same hotkey -> Update data
    /// 2. if UID already exists with different hotkey -> Update to new hotkey (recycled UID)
    /// 3. if UID doesn't exist but hotkey does -> on re-registration, migrate the UID
    /// 4. if neither UID nor hotkey exist -> Create new miner
    pub async fn ensure_miner_exists_with_info(&self, miner_info: &MinerInfo) -> Result<()> {
        let new_miner_uid = format!("miner_{}", miner_info.uid.as_u16());
        let hotkey = miner_info.hotkey.to_string();

        let existing_by_uid = self.check_miner_by_uid(&new_miner_uid).await?;

        if let Some((_, existing_hotkey)) = existing_by_uid {
            return self
                .handle_recycled_miner_uid(&new_miner_uid, &hotkey, &existing_hotkey, miner_info)
                .await;
        }

        let existing_by_hotkey = self.check_miner_by_hotkey(&hotkey).await?;

        if let Some(old_miner_uid) = existing_by_hotkey {
            return self
                .handle_uid_change(&old_miner_uid, &new_miner_uid, &hotkey, miner_info)
                .await;
        }

        self.create_new_miner(&new_miner_uid, &hotkey, miner_info)
            .await
    }

    /// Sync miners from metagraph to database
    pub async fn sync_miners_from_metagraph(&self, miners: &[MinerInfo]) -> Result<()> {
        info!("Syncing {} miners from metagraph to database", miners.len());

        for miner in miners {
            if let Err(e) = self.ensure_miner_exists_with_info(miner).await {
                warn!(
                    "Failed to sync miner {} to database: {}",
                    miner.uid.as_u16(),
                    e
                );
            } else {
                debug!(
                    "Successfully synced miner {} with endpoint {} to database",
                    miner.uid.as_u16(),
                    miner.endpoint
                );
            }
        }

        info!("Completed syncing miners from metagraph");
        Ok(())
    }

    /// Query recent verification logs for a miner's nodes
    pub async fn query_recent_miner_verification_logs(
        &self,
        miner_uid: u16,
        cutoff_time: &str,
    ) -> Result<Vec<sqlx::sqlite::SqliteRow>> {
        let query = r#"
            SELECT vl.*, me.miner_id, me.status
            FROM verification_logs vl
            INNER JOIN miner_nodes me ON vl.node_id = me.node_id
            WHERE me.miner_id = ?
                AND vl.timestamp >= ?
                AND me.status IN ('online', 'verified')
                AND EXISTS (
                    SELECT 1 FROM gpu_uuid_assignments ga
                    WHERE ga.node_id = vl.node_id
                    AND ga.miner_id = me.miner_id
                )
            ORDER BY vl.timestamp DESC
        "#;

        let miner_id = format!("miner_{miner_uid}");
        let rows = sqlx::query(query)
            .bind(&miner_id)
            .bind(cutoff_time)
            .fetch_all(self.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to query verification logs: {}", e))?;

        Ok(rows)
    }

    /// Get all unique miner IDs from recent validations
    pub async fn get_miners_with_recent_validations(
        &self,
        cutoff_time: &str,
    ) -> Result<Vec<String>> {
        let query = r#"
            SELECT DISTINCT me.miner_id
            FROM miner_nodes me
            JOIN verification_logs vl ON me.node_id = vl.node_id
            WHERE vl.timestamp >= ?
        "#;

        let rows = sqlx::query(query)
            .bind(cutoff_time)
            .fetch_all(self.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to query miners: {}", e))?;

        let miner_ids = rows.into_iter().map(|row| row.get("miner_id")).collect();
        Ok(miner_ids)
    }
}
