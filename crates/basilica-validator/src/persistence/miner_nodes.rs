//! Miner node relationship persistence operations
//!
//! This module contains all SQL operations related to miner-node relationships.

use crate::miner_prover::types::MinerInfo;
use crate::persistence::SimplePersistence;
use anyhow::Result;
use sqlx::Row;
use std::str::FromStr;
use std::time::Duration;
use tracing::{debug, info, warn};

impl SimplePersistence {
    /// Ensure miner-node relationship exists
    pub async fn ensure_miner_node_relationship(
        &self,
        miner_uid: u16,
        node_id: &str,
        node_ssh_endpoint: &str,
        miner_info: &MinerInfo,
    ) -> Result<()> {
        info!(
            miner_uid = miner_uid,
            node_id = node_id,
            "Ensuring miner-node relationship for miner {} and node {} with real data",
            miner_uid,
            node_id
        );

        let miner_id = format!("miner_{miner_uid}");

        self.ensure_miner_exists_with_info(miner_info).await?;

        let query = "SELECT COUNT(*) as count FROM miner_nodes WHERE miner_id = ? AND node_id = ?";
        let row = sqlx::query(query)
            .bind(&miner_id)
            .bind(node_id)
            .fetch_one(self.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check miner-node relationship: {}", e))?;

        let count: i64 = row.get("count");

        if count == 0 {
            let existing_miner: Option<String> = sqlx::query_scalar(
                "SELECT miner_id FROM miner_nodes WHERE node_ssh_endpoint = ? AND miner_id != ? LIMIT 1",
            )
            .bind(node_ssh_endpoint)
            .bind(&miner_id)
            .fetch_optional(self.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check node_ssh_endpoint uniqueness: {}", e))?;

            if let Some(other_miner) = existing_miner {
                return Err(anyhow::anyhow!(
                    "Cannot create node relationship: node_ssh_endpoint {} is already registered to {}",
                    node_ssh_endpoint,
                    other_miner
                ));
            }

            let old_node_id: Option<String> = sqlx::query_scalar(
                "SELECT node_id FROM miner_nodes WHERE node_ssh_endpoint = ? AND miner_id = ?",
            )
            .bind(node_ssh_endpoint)
            .bind(&miner_id)
            .fetch_optional(self.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check for existing node: {}", e))?;

            if let Some(old_id) = old_node_id {
                info!(
                    "Miner {} is changing node ID from {} to {} for endpoint {}",
                    miner_id, old_id, node_id, node_ssh_endpoint
                );

                let mut tx = self.pool().begin().await?;

                sqlx::query(
                    "UPDATE gpu_uuid_assignments SET node_id = ? WHERE node_id = ? AND miner_id = ?"
                )
                .bind(node_id)
                .bind(&old_id)
                .bind(&miner_id)
                .execute(&mut *tx)
                .await?;

                sqlx::query("DELETE FROM miner_nodes WHERE node_id = ? AND miner_id = ?")
                    .bind(&old_id)
                    .bind(&miner_id)
                    .execute(&mut *tx)
                    .await?;

                tx.commit().await?;

                info!(
                    "Successfully migrated GPU assignments from node {} to {}",
                    old_id, node_id
                );
            }

            let insert_query = r#"
                INSERT OR IGNORE INTO miner_nodes (
                    id, miner_id, node_id, node_ssh_endpoint, gpu_count,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            "#;

            let relationship_id = format!("{miner_id}_{node_id}");

            sqlx::query(insert_query)
                .bind(&relationship_id)
                .bind(&miner_id)
                .bind(node_id)
                .bind(node_ssh_endpoint)
                .bind(0)
                .bind("online")
                .execute(self.pool())
                .await
                .map_err(|e| anyhow::anyhow!("Failed to insert miner-node relationship: {}", e))?;

            info!(
                miner_uid = miner_uid,
                node_id = node_id,
                "Created miner-node relationship: {} -> {} with endpoint {}",
                miner_id,
                node_id,
                node_ssh_endpoint
            );
        } else {
            debug!(
                miner_uid = miner_uid,
                node_id = node_id,
                "Miner-node relationship already exists: {} -> {}",
                miner_id,
                node_id
            );

            let duplicate_check_query: &'static str =
                "SELECT id, node_id FROM miner_nodes WHERE node_ssh_endpoint = ? AND id != ?";
            let relationship_id = format!("{miner_id}_{node_id}");

            let duplicates = sqlx::query(duplicate_check_query)
                .bind(node_ssh_endpoint)
                .bind(&relationship_id)
                .fetch_all(self.pool())
                .await
                .map_err(|e| anyhow::anyhow!("Failed to check for duplicate nodes: {}", e))?;

            if !duplicates.is_empty() {
                let duplicate_count = duplicates.len();
                warn!(
                    miner_uid = miner_uid,
                    "Found {} duplicate nodes with same node_ssh_endpoint {} for miner {}",
                    duplicate_count,
                    node_ssh_endpoint,
                    miner_id
                );

                for duplicate in duplicates {
                    let dup_id: String = duplicate.get("id");
                    let dup_node_id: String = duplicate.get("node_id");

                    warn!(
                        miner_uid = miner_uid,
                        "Marking duplicate node {} (id: {}) as offline with same node_ssh_endpoint as {} for miner {}",
                        dup_node_id, dup_id, node_id, miner_id
                    );

                    sqlx::query("UPDATE miner_nodes SET status = 'offline', last_health_check = datetime('now'), updated_at = datetime('now') WHERE id = ?")
                        .bind(&dup_id)
                        .execute(self.pool())
                        .await
                        .map_err(|e| {
                            anyhow::anyhow!("Failed to update duplicate node status: {}", e)
                        })?;

                    self.cleanup_gpu_assignments(&dup_node_id, &miner_id, None)
                        .await
                        .map_err(|e| {
                            anyhow::anyhow!(
                                "Failed to clean up GPU assignments for duplicate: {}",
                                e
                            )
                        })?;
                }

                info!(
                    miner_uid = miner_uid,
                    "Cleaned up {} duplicate nodes for miner {} with node_ssh_endpoint {}",
                    duplicate_count,
                    miner_id,
                    node_ssh_endpoint
                );
            }
        }

        Ok(())
    }

    /// Clean up nodes that have consecutive failed validations
    pub async fn cleanup_failed_nodes_after_failures(
        &self,
        consecutive_failures_threshold: i32,
        gpu_assignment_cleanup_ttl: Option<Duration>,
    ) -> Result<()> {
        info!(
            "Running node cleanup - checking for {} consecutive failures",
            consecutive_failures_threshold
        );

        let offline_with_gpus_query = r#"
            SELECT DISTINCT me.node_id, me.miner_id, COUNT(ga.gpu_uuid) as gpu_count
            FROM miner_nodes me
            INNER JOIN gpu_uuid_assignments ga ON me.node_id = ga.node_id AND me.miner_id = ga.miner_id
            WHERE me.status = 'offline'
            GROUP BY me.node_id, me.miner_id
        "#;

        let offline_with_gpus = sqlx::query(offline_with_gpus_query)
            .fetch_all(self.pool())
            .await?;

        let mut gpu_assignments_cleaned = 0;
        for row in offline_with_gpus {
            let node_id: String = row.try_get("node_id")?;
            let miner_id: String = row.try_get("miner_id")?;
            let gpu_count: i64 = row.try_get("gpu_count")?;

            info!(
                "Cleaning up {} GPU assignments for offline node {} (miner: {})",
                gpu_count, node_id, miner_id
            );

            let rows_cleaned = self
                .cleanup_gpu_assignments(&node_id, &miner_id, None)
                .await?;
            gpu_assignments_cleaned += rows_cleaned;
        }

        let mismatched_gpu_query = r#"
            SELECT me.node_id, me.miner_id, me.gpu_count, me.status
            FROM miner_nodes me
            WHERE me.gpu_count > 0
            AND NOT EXISTS (
                SELECT 1 FROM gpu_uuid_assignments ga
                WHERE ga.node_id = me.node_id AND ga.miner_id = me.miner_id
            )
        "#;

        let mismatched_nodes = sqlx::query(mismatched_gpu_query)
            .fetch_all(self.pool())
            .await?;

        for row in mismatched_nodes {
            let node_id: String = row.try_get("node_id")?;
            let miner_id: String = row.try_get("miner_id")?;
            let gpu_count: i32 = row.try_get("gpu_count")?;
            let status: String = row.try_get("status")?;

            warn!(
                "Node {} (miner: {}) claims {} GPUs but has no assignments, status: {}. Resetting GPU count to 0",
                node_id, miner_id, gpu_count, status
            );

            sqlx::query(
                "UPDATE miner_nodes SET gpu_count = 0, updated_at = datetime('now')
                 WHERE node_id = ? AND miner_id = ?",
            )
            .bind(&node_id)
            .bind(&miner_id)
            .execute(self.pool())
            .await?;

            if status == "online" || status == "verified" {
                sqlx::query(
                    "UPDATE miner_nodes SET status = 'offline', updated_at = datetime('now')
                     WHERE node_id = ? AND miner_id = ?",
                )
                .bind(&node_id)
                .bind(&miner_id)
                .execute(self.pool())
                .await?;

                info!(
                    "Marked node {} as offline (claimed {} GPUs but has 0 assignments)",
                    node_id, gpu_count
                );
            }
        }

        let stale_gpu_cleanup_query = r#"
            DELETE FROM gpu_uuid_assignments
            WHERE last_verified < datetime('now', '-6 hours')
            OR (
                EXISTS (
                    SELECT 1 FROM miner_nodes me
                    WHERE me.node_id = gpu_uuid_assignments.node_id
                    AND me.miner_id = gpu_uuid_assignments.miner_id
                    AND me.status = 'offline'
                    AND (
                        me.last_health_check < datetime('now', '-2 hours')
                        OR (me.last_health_check IS NULL AND me.updated_at < datetime('now', '-2 hours'))
                    )
                )
            )
        "#;

        let stale_gpu_result = sqlx::query(stale_gpu_cleanup_query)
            .execute(self.pool())
            .await?;

        if stale_gpu_result.rows_affected() > 0 {
            info!(
                security = true,
                cleaned_count = stale_gpu_result.rows_affected(),
                cleanup_reason = "stale_timeout",
                threshold_hours = 6,
                "Cleaned up {} stale GPU assignments (not verified in last 6 hours or belonging to offline nodes >2h)",
                stale_gpu_result.rows_affected()
            );
        }

        let cleanup_minutes = gpu_assignment_cleanup_ttl
            .map(|d| d.as_secs() / 60)
            .unwrap_or(120)
            .max(120);

        info!(
            "Cleaning GPU assignments from nodes offline >{} minutes",
            cleanup_minutes
        );

        let stale_offline_query = format!(
            r#"
            SELECT DISTINCT me.node_id, me.miner_id, COUNT(ga.gpu_uuid) as gpu_count
            FROM miner_nodes me
            INNER JOIN gpu_uuid_assignments ga ON me.node_id = ga.node_id AND me.miner_id = ga.miner_id
            WHERE me.status = 'offline'
            AND (
                me.last_health_check < datetime('now', '-{cleanup_minutes} minutes')
                OR (me.last_health_check IS NULL AND me.updated_at < datetime('now', '-{cleanup_minutes} minutes'))
            )
            GROUP BY me.node_id, me.miner_id
            "#
        );

        let stale_offline = sqlx::query(&stale_offline_query)
            .fetch_all(self.pool())
            .await?;

        let mut stale_gpu_cleaned = 0;
        for row in stale_offline {
            let node_id: String = row.try_get("node_id")?;
            let miner_id: String = row.try_get("miner_id")?;
            let gpu_count: i64 = row.try_get("gpu_count")?;

            info!(
                security = true,
                node_id = %node_id,
                miner_id = %miner_id,
                gpu_count = gpu_count,
                cleanup_minutes = cleanup_minutes,
                "Cleaning GPU assignments from node offline >{}min", cleanup_minutes
            );

            let cleaned = self
                .cleanup_gpu_assignments(&node_id, &miner_id, None)
                .await?;
            stale_gpu_cleaned += cleaned;
        }

        if stale_gpu_cleaned > 0 {
            info!(
                security = true,
                cleaned_count = stale_gpu_cleaned,
                cleanup_minutes = cleanup_minutes,
                "Cleaned {} GPU assignments from nodes offline >{}min",
                stale_gpu_cleaned,
                cleanup_minutes
            );
        }

        let delete_nodes_query = r#"
            WITH recent_verifications AS (
                SELECT
                    vl.node_id,
                    vl.success,
                    vl.timestamp,
                    ROW_NUMBER() OVER (PARTITION BY vl.node_id ORDER BY vl.timestamp DESC) as rn
                FROM verification_logs vl
                WHERE vl.timestamp > datetime('now', '-1 hour')
            )
            SELECT
                me.node_id,
                me.miner_id,
                me.status,
                COALESCE(SUM(CASE WHEN rv.success = 0 AND rv.rn <= ? THEN 1 ELSE 0 END), 0) as consecutive_fails,
                COALESCE(SUM(CASE WHEN rv.success = 1 AND rv.rn <= ? THEN 1 ELSE 0 END), 0) as recent_successes,
                MAX(rv.timestamp) as last_verification
            FROM miner_nodes me
            LEFT JOIN recent_verifications rv ON me.node_id = rv.node_id
            WHERE me.status = 'offline'
            GROUP BY me.node_id, me.miner_id, me.status
            HAVING consecutive_fails >= ? AND recent_successes = 0
        "#;

        let nodes_to_delete = sqlx::query(delete_nodes_query)
            .bind(consecutive_failures_threshold)
            .bind(consecutive_failures_threshold)
            .bind(consecutive_failures_threshold)
            .fetch_all(self.pool())
            .await?;

        let mut deleted = 0;
        for row in nodes_to_delete {
            let node_id: String = row.try_get("node_id")?;
            let miner_id: String = row.try_get("miner_id")?;
            let consecutive_fails: i64 = row.try_get("consecutive_fails")?;
            let last_verification: Option<String> = row.try_get("last_verification").ok();

            info!(
                "Permanently deleting node {} (miner: {}) after {} consecutive failures, last seen: {}",
                node_id, miner_id, consecutive_fails,
                last_verification.as_deref().unwrap_or("never")
            );

            let mut tx = self.pool().begin().await?;

            self.cleanup_gpu_assignments(&node_id, &miner_id, Some(&mut tx))
                .await?;

            sqlx::query("DELETE FROM miner_nodes WHERE node_id = ? AND miner_id = ?")
                .bind(&node_id)
                .bind(&miner_id)
                .execute(&mut *tx)
                .await?;

            tx.commit().await?;
            deleted += 1;
        }

        let stale_delete_query = format!(
            r#"
            DELETE FROM miner_nodes
            WHERE status = 'offline'
            AND (
                last_health_check < datetime('now', '-{} minutes')
                OR (last_health_check IS NULL AND updated_at < datetime('now', '-{} minutes'))
            )
            "#,
            cleanup_minutes, cleanup_minutes
        );

        info!(
            "Deleting stale offline nodes using {}min timeout (configurable via gpu_assignment_cleanup_ttl)",
            cleanup_minutes
        );

        let stale_result = sqlx::query(&stale_delete_query)
            .execute(self.pool())
            .await?;

        let stale_deleted = stale_result.rows_affected();

        let affected_miners_query = r#"
            SELECT DISTINCT miner_uid
            FROM miner_gpu_profiles
            WHERE miner_uid IN (
                SELECT DISTINCT CAST(SUBSTR(miner_id, 7) AS INTEGER)
                FROM miner_nodes
                WHERE status = 'offline'

                UNION

                SELECT miner_uid
                FROM miner_gpu_profiles
                WHERE gpu_counts_json <> '{}'
                AND NOT EXISTS (
                    SELECT 1 FROM miner_nodes
                    WHERE miner_id = 'miner_' || miner_gpu_profiles.miner_uid
                    AND status NOT IN ('offline', 'failed', 'stale')
                )
            )
        "#;

        let affected_miners = sqlx::query(affected_miners_query)
            .fetch_all(self.pool())
            .await?;

        for row in affected_miners {
            let miner_uid: i64 = row.try_get("miner_uid")?;
            let miner_id = format!("miner_{}", miner_uid);

            let gpu_counts = self.get_miner_gpu_uuid_assignments(&miner_id).await?;

            let mut gpu_map: std::collections::HashMap<String, u32> =
                std::collections::HashMap::new();
            for (_, count, gpu_name, _) in gpu_counts {
                let category =
                    crate::gpu::categorization::GpuCategory::from_str(&gpu_name).unwrap();
                let model = category.to_string();
                *gpu_map.entry(model).or_insert(0) += count;
            }

            let update_query = if gpu_map.is_empty() {
                r#"
                UPDATE miner_gpu_profiles
                SET gpu_counts_json = ?,
                    total_score = 0.0,
                    verification_count = 0,
                    last_successful_validation = NULL,
                    last_updated = datetime('now')
                WHERE miner_uid = ?
                "#
            } else {
                r#"
                UPDATE miner_gpu_profiles
                SET gpu_counts_json = ?,
                    last_updated = datetime('now')
                WHERE miner_uid = ?
                "#
            };

            let gpu_json = serde_json::to_string(&gpu_map)?;
            let result = sqlx::query(update_query)
                .bind(&gpu_json)
                .bind(miner_uid)
                .execute(self.pool())
                .await?;

            if result.rows_affected() > 0 {
                info!(
                    "Updated GPU profile for miner {} after cleanup: {}",
                    miner_uid, gpu_json
                );
            }
        }

        if gpu_assignments_cleaned > 0 {
            info!(
                "Deleted {} GPU assignments from offline nodes",
                gpu_assignments_cleaned
            );
        }

        if deleted > 0 {
            info!(
                "Deleted {} nodes with {} or more consecutive failures",
                deleted, consecutive_failures_threshold
            );
        }

        if stale_deleted > 0 {
            info!("Deleted {} stale offline nodes", stale_deleted);
        }

        if gpu_assignments_cleaned == 0 && deleted == 0 && stale_deleted == 0 {
            debug!("No nodes needed cleanup in this cycle");
        }

        Ok(())
    }
}
