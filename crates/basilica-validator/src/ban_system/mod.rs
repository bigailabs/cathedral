use anyhow::Result;
use chrono::{DateTime, Duration, Utc};
use serde_json::json;
use std::sync::Arc;
use tracing::{debug, info, warn};

use crate::persistence::entities::{MisbehaviourLog, MisbehaviourType};
use crate::persistence::SimplePersistence;

/// Ban manager for handling executor misbehaviour and ban status
pub struct BanManager {
    persistence: Arc<SimplePersistence>,
}

impl BanManager {
    /// Create a new ban manager
    pub fn new(persistence: Arc<SimplePersistence>) -> Self {
        Self { persistence }
    }

    /// Log a misbehaviour for an executor
    ///
    /// This function:
    /// 1. Fetches the GPU UUID for the executor
    /// 2. Records the misbehaviour
    /// 3. Checks if a ban should be triggered
    pub async fn log_misbehaviour(
        &self,
        miner_uid: u16,
        executor_id: &str,
        type_of_misbehaviour: MisbehaviourType,
        details: &str,
    ) -> Result<()> {
        // Convert miner_uid to miner_id format
        let miner_id = format!("miner_{}", miner_uid);

        // Get GPU UUID for the executor (at index 0)
        let gpu_uuid = self
            .persistence
            .get_gpu_uuid_for_executor(&miner_id, executor_id)
            .await?
            .ok_or_else(|| anyhow::anyhow!("No GPU UUID found for executor {}", executor_id))?;

        // Get executor endpoint
        let endpoint = self
            .persistence
            .get_executor_endpoint(&miner_id, executor_id)
            .await?
            .unwrap_or_else(|| "unknown".to_string());

        // Create misbehaviour log
        let log = MisbehaviourLog::new(
            miner_uid,
            executor_id.to_string(),
            gpu_uuid.clone(),
            endpoint,
            type_of_misbehaviour,
            details.to_string(),
        );

        // Insert the log into database
        self.persistence.insert_misbehaviour_log(&log).await?;

        info!(
            miner_uid = miner_uid,
            executor_id = executor_id,
            misbehaviour_type = ?type_of_misbehaviour,
            gpu_uuid = %gpu_uuid,
            "Misbehaviour logged for executor"
        );

        // Check if ban should be triggered
        let should_ban = self
            .check_ban_trigger(miner_uid, executor_id)
            .await
            .unwrap_or(false);

        if should_ban {
            warn!(
                miner_uid = miner_uid,
                executor_id = executor_id,
                "Executor has triggered ban conditions"
            );
        }

        Ok(())
    }

    /// Check if an executor is currently banned
    pub async fn is_executor_banned(&self, miner_uid: u16, executor_id: &str) -> Result<bool> {
        // Get recent misbehaviour logs
        let logs = self
            .get_recent_misbehaviours(miner_uid, executor_id, Duration::days(7))
            .await?;

        if logs.is_empty() {
            return Ok(false);
        }

        // Calculate ban duration based on offense count
        let ban_duration = self.calculate_ban_duration(logs.len());

        // Check if the most recent ban has expired
        let most_recent = logs.iter().max_by_key(|log| log.recorded_at).unwrap();

        // Check if executor had multiple failures within 1 hour
        let one_hour_ago = Utc::now() - Duration::hours(1);
        let recent_failures = logs
            .iter()
            .filter(|log| log.recorded_at >= one_hour_ago)
            .count();

        // Ban is active if:
        // 1. There are 2+ failures within the last hour, OR
        // 2. The ban period from the most recent misbehaviour hasn't expired
        if recent_failures >= 2 {
            let ban_expiry = most_recent.recorded_at + ban_duration;
            let is_banned = Utc::now() < ban_expiry;

            if is_banned {
                debug!(
                    miner_uid = miner_uid,
                    executor_id = executor_id,
                    ban_expiry = %ban_expiry,
                    failures_in_hour = recent_failures,
                    "Executor is currently banned"
                );
            }

            Ok(is_banned)
        } else {
            Ok(false)
        }
    }

    /// Get ban expiry time for an executor
    pub async fn get_ban_expiry(
        &self,
        miner_uid: u16,
        executor_id: &str,
    ) -> Result<Option<DateTime<Utc>>> {
        if !self.is_executor_banned(miner_uid, executor_id).await? {
            return Ok(None);
        }

        let logs = self
            .get_recent_misbehaviours(miner_uid, executor_id, Duration::days(7))
            .await?;

        if logs.is_empty() {
            return Ok(None);
        }

        let ban_duration = self.calculate_ban_duration(logs.len());
        let most_recent = logs.iter().max_by_key(|log| log.recorded_at).unwrap();

        Ok(Some(most_recent.recorded_at + ban_duration))
    }

    /// Check if ban should be triggered based on recent misbehaviours
    async fn check_ban_trigger(&self, miner_uid: u16, executor_id: &str) -> Result<bool> {
        let one_hour_ago = Utc::now() - Duration::hours(1);
        let logs = self
            .persistence
            .get_misbehaviour_logs(miner_uid, executor_id, one_hour_ago)
            .await?;

        // Trigger ban if 2 or more misbehaviours within 1 hour
        Ok(logs.len() >= 2)
    }

    /// Get recent misbehaviours within a time window
    async fn get_recent_misbehaviours(
        &self,
        miner_uid: u16,
        executor_id: &str,
        window: Duration,
    ) -> Result<Vec<MisbehaviourLog>> {
        let since = Utc::now() - window;
        self.persistence
            .get_misbehaviour_logs(miner_uid, executor_id, since)
            .await
    }

    /// Calculate ban duration based on offense count within 7 days
    ///
    /// Ban duration progression:
    /// - 1st offense: 1 hour
    /// - 2nd offense: 2 hours
    /// - 3rd offense: 4 hours
    /// - 4th offense: 8 hours
    /// - 5th+ offense: 24 hours (max)
    fn calculate_ban_duration(&self, offense_count: usize) -> Duration {
        match offense_count {
            0 => Duration::hours(0),
            1 => Duration::hours(1),
            2 => Duration::hours(2),
            3 => Duration::hours(4),
            4 => Duration::hours(8),
            _ => Duration::hours(24), // Max ban duration
        }
    }

    /// Create a JSON string with rental failure details
    pub fn create_rental_failure_details(
        rental_id: &str,
        executor_id: &str,
        error: &str,
        ssh_details: Option<&str>,
    ) -> String {
        json!({
            "rental_id": rental_id,
            "executor_id": executor_id,
            "error": error,
            "ssh_details": ssh_details,
            "timestamp": Utc::now().to_rfc3339(),
        })
        .to_string()
    }

    /// Create a JSON string with health check failure details
    pub fn create_health_failure_details(
        rental_id: &str,
        executor_id: &str,
        container_id: &str,
        error: &str,
    ) -> String {
        json!({
            "rental_id": rental_id,
            "executor_id": executor_id,
            "container_id": container_id,
            "error": error,
            "timestamp": Utc::now().to_rfc3339(),
        })
        .to_string()
    }

    /// Create a JSON string for deployment failure details
    pub fn create_deployment_failure_details(error_message: &str) -> String {
        json!({
            "reason": "deployment_failed",
            "error": error_message,
            "timestamp": Utc::now().to_rfc3339(),
        })
        .to_string()
    }

    /// Create a JSON string for rejection details
    pub fn create_rejection_details(rejection_reason: &str) -> String {
        json!({
            "reason": "rental_rejected",
            "rejection_reason": rejection_reason,
            "timestamp": Utc::now().to_rfc3339(),
        })
        .to_string()
    }

    /// Create a JSON string for health check failure details
    pub fn create_health_check_failure_details(
        container_id: &str,
        rental_state: &str,
        error_message: &str,
    ) -> String {
        json!({
            "reason": "health_check_failed",
            "container_id": container_id,
            "rental_state": rental_state,
            "error": error_message,
            "timestamp": Utc::now().to_rfc3339(),
        })
        .to_string()
    }
}
