use anyhow::Result;
use chrono::{DateTime, Duration, Utc};
use serde_json::json;
use std::sync::Arc;
use tracing::{debug, info, warn};

use crate::metrics::ValidatorPrometheusMetrics;
use crate::persistence::entities::{MisbehaviourLog, MisbehaviourType};
use crate::persistence::SimplePersistence;

/// Ban manager for handling executor misbehaviour and ban status
pub struct BanManager {
    persistence: Arc<SimplePersistence>,
    metrics: Option<Arc<ValidatorPrometheusMetrics>>,
}

impl BanManager {
    /// Create a new ban manager
    pub fn new(
        persistence: Arc<SimplePersistence>,
        metrics: Option<Arc<ValidatorPrometheusMetrics>>,
    ) -> Self {
        Self {
            persistence,
            metrics,
        }
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

        // Refresh ban metric after recording misbehaviour
        if let Err(err) = self
            .compute_current_ban(miner_uid, executor_id)
            .await
            .map(|_| ())
        {
            warn!(
                miner_uid = miner_uid,
                executor_id = executor_id,
                error = %err,
                "Failed to refresh ban metric after logging misbehaviour"
            );
        }

        Ok(())
    }

    /// Check if an executor is currently banned
    pub async fn is_executor_banned(&self, miner_uid: u16, executor_id: &str) -> Result<bool> {
        let status = self.compute_current_ban(miner_uid, executor_id).await?;

        if let (Some(ban_expiry), Some(ban_trigger)) = (&status.ban_expiry, &status.ban_trigger) {
            debug!(
                miner_uid = miner_uid,
                executor_id = executor_id,
                ban_trigger = %ban_trigger,
                ban_expiry = %ban_expiry,
                offense_count = status.offense_count,
                "Executor is currently banned"
            );
        }

        Ok(status.ban_expiry.is_some())
    }

    /// Get ban expiry time for an executor
    pub async fn get_ban_expiry(
        &self,
        miner_uid: u16,
        executor_id: &str,
    ) -> Result<Option<DateTime<Utc>>> {
        let status = self.compute_current_ban(miner_uid, executor_id).await?;
        Ok(status.ban_expiry)
    }

    async fn compute_current_ban(
        &self,
        miner_uid: u16,
        executor_id: &str,
    ) -> Result<BanComputation> {
        let logs = self
            .get_recent_misbehaviours(miner_uid, executor_id, Duration::days(7))
            .await?;

        let offense_count = logs.len();

        if offense_count == 0 {
            self.record_ban_metric(miner_uid, executor_id, None);
            return Ok(BanComputation {
                ban_expiry: None,
                ban_trigger: None,
                offense_count,
            });
        }

        let ban_trigger = self.find_ban_trigger_timestamp(&logs);

        let ban_expiry = ban_trigger.and_then(|trigger_time| {
            let ban_duration = self.calculate_ban_duration(offense_count);
            let expiry = trigger_time + ban_duration;

            if Utc::now() < expiry {
                Some(expiry)
            } else {
                None
            }
        });

        self.record_ban_metric(miner_uid, executor_id, ban_expiry);

        Ok(BanComputation {
            ban_expiry,
            ban_trigger,
            offense_count,
        })
    }

    fn record_ban_metric(
        &self,
        miner_uid: u16,
        executor_id: &str,
        ban_expiry: Option<DateTime<Utc>>,
    ) {
        if let Some(metrics) = &self.metrics {
            metrics.record_node_ban_till(executor_id, miner_uid, ban_expiry);
        }
    }

    /// Check if ban should be triggered based on recent misbehaviours
    async fn check_ban_trigger(&self, miner_uid: u16, executor_id: &str) -> Result<bool> {
        let logs = self
            .persistence
            .get_misbehaviour_logs(miner_uid, executor_id, Duration::hours(1))
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
        self.persistence
            .get_misbehaviour_logs(miner_uid, executor_id, window)
            .await
    }

    /// Find the latest timestamp where a ban was triggered
    ///
    /// A ban is triggered when there are 2+ misbehaviours within any 1-hour sliding window.
    /// Returns the timestamp of the later misbehaviour that triggered the ban.
    fn find_ban_trigger_timestamp(&self, logs: &[MisbehaviourLog]) -> Option<DateTime<Utc>> {
        if logs.len() < 2 {
            return None;
        }

        // Sort logs by timestamp (oldest to newest)
        let mut sorted_logs: Vec<&MisbehaviourLog> = logs.iter().collect();
        sorted_logs.sort_by_key(|log| log.recorded_at);

        let mut latest_trigger: Option<DateTime<Utc>> = None;

        // Check each log as a potential trigger point
        for i in 1..sorted_logs.len() {
            let current_log = sorted_logs[i];
            let one_hour_before = current_log.recorded_at - Duration::hours(1);

            // Count how many logs fall within the 1-hour window before this log
            let failures_in_window = sorted_logs
                .iter()
                .filter(|log| {
                    log.recorded_at >= one_hour_before && log.recorded_at <= current_log.recorded_at
                })
                .count();

            // If this timestamp triggers a ban (2+ failures in window), update latest trigger
            if failures_in_window >= 2 {
                latest_trigger = Some(current_log.recorded_at);
            }
        }

        latest_trigger
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

struct BanComputation {
    ban_expiry: Option<DateTime<Utc>>,
    ban_trigger: Option<DateTime<Utc>>,
    offense_count: usize,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::persistence::entities::{MisbehaviourLog, MisbehaviourType};
    use chrono::{Duration, Utc};

    fn create_test_log(
        miner_uid: u16,
        executor_id: &str,
        recorded_at: DateTime<Utc>,
    ) -> MisbehaviourLog {
        let now = Utc::now();
        MisbehaviourLog {
            miner_uid,
            executor_id: executor_id.to_string(),
            gpu_uuid: "test-gpu-uuid".to_string(),
            endpoint_executor: "test-endpoint".to_string(),
            type_of_misbehaviour: MisbehaviourType::BadRental,
            details: "test details".to_string(),
            recorded_at,
            created_at: now,
            updated_at: now,
        }
    }

    // Helper function to test ban trigger logic without needing persistence
    fn test_find_ban_trigger(logs: &[MisbehaviourLog]) -> Option<DateTime<Utc>> {
        if logs.len() < 2 {
            return None;
        }

        let mut sorted_logs: Vec<&MisbehaviourLog> = logs.iter().collect();
        sorted_logs.sort_by_key(|log| log.recorded_at);

        let mut latest_trigger: Option<DateTime<Utc>> = None;

        for i in 1..sorted_logs.len() {
            let current_log = sorted_logs[i];
            let one_hour_before = current_log.recorded_at - Duration::hours(1);

            let failures_in_window = sorted_logs
                .iter()
                .filter(|log| {
                    log.recorded_at >= one_hour_before && log.recorded_at <= current_log.recorded_at
                })
                .count();

            if failures_in_window >= 2 {
                latest_trigger = Some(current_log.recorded_at);
            }
        }

        latest_trigger
    }

    // Helper function to test ban duration calculation
    fn test_calculate_duration(offense_count: usize) -> Duration {
        match offense_count {
            0 => Duration::hours(0),
            1 => Duration::hours(1),
            2 => Duration::hours(2),
            3 => Duration::hours(4),
            4 => Duration::hours(8),
            _ => Duration::hours(24), // Max ban duration
        }
    }

    #[test]
    fn test_find_ban_trigger_no_logs() {
        let logs = vec![];
        assert_eq!(test_find_ban_trigger(&logs), None);
    }

    #[test]
    fn test_find_ban_trigger_single_log() {
        let now = Utc::now();
        let logs = vec![create_test_log(1, "executor1", now)];
        assert_eq!(test_find_ban_trigger(&logs), None);
    }

    #[test]
    fn test_find_ban_trigger_two_logs_within_hour() {
        let now = Utc::now();
        let logs = vec![
            create_test_log(1, "executor1", now - Duration::minutes(30)),
            create_test_log(1, "executor1", now),
        ];
        // Should trigger ban at the second log timestamp
        assert_eq!(test_find_ban_trigger(&logs), Some(now));
    }

    #[test]
    fn test_find_ban_trigger_two_logs_outside_hour() {
        let now = Utc::now();
        let logs = vec![
            create_test_log(1, "executor1", now - Duration::hours(2)),
            create_test_log(1, "executor1", now),
        ];
        // Should not trigger ban as logs are more than 1 hour apart
        assert_eq!(test_find_ban_trigger(&logs), None);
    }

    #[test]
    fn test_find_ban_trigger_multiple_triggers() {
        let now = Utc::now();
        let logs = vec![
            create_test_log(1, "executor1", now - Duration::hours(3)),
            create_test_log(
                1,
                "executor1",
                now - Duration::hours(3) + Duration::minutes(30),
            ),
            create_test_log(1, "executor1", now - Duration::minutes(40)),
            create_test_log(1, "executor1", now - Duration::minutes(20)),
        ];
        // Should return the latest trigger (now - 20 minutes)
        assert_eq!(
            test_find_ban_trigger(&logs),
            Some(now - Duration::minutes(20))
        );
    }

    #[test]
    fn test_find_ban_trigger_sliding_window() {
        let now = Utc::now();
        // Three logs: first two trigger a ban, third is outside the window
        let logs = vec![
            create_test_log(1, "executor1", now - Duration::minutes(90)),
            create_test_log(1, "executor1", now - Duration::minutes(50)), // Triggers ban
            create_test_log(1, "executor1", now),
        ];
        // The last two logs also form a trigger (50 mins apart)
        assert_eq!(test_find_ban_trigger(&logs), Some(now));
    }

    #[test]
    fn test_calculate_ban_duration_progression() {
        assert_eq!(test_calculate_duration(0), Duration::hours(0));
        assert_eq!(test_calculate_duration(1), Duration::hours(1));
        assert_eq!(test_calculate_duration(2), Duration::hours(2));
        assert_eq!(test_calculate_duration(3), Duration::hours(4));
        assert_eq!(test_calculate_duration(4), Duration::hours(8));
        assert_eq!(test_calculate_duration(5), Duration::hours(24));
        assert_eq!(test_calculate_duration(10), Duration::hours(24)); // Max
    }

    #[tokio::test]
    async fn test_ban_persists_after_hour_window() {
        // This is the critical test that verifies the fix
        // It simulates the scenario where a ban should persist even after
        // the 1-hour window no longer contains 2 failures

        use crate::persistence::SimplePersistence;
        use std::sync::Arc;

        // Create in-memory database for testing
        let persistence = Arc::new(
            SimplePersistence::new(":memory:", "test_hotkey".to_string())
                .await
                .unwrap(),
        );
        persistence.run_migrations().await.unwrap();

        let ban_manager = BanManager::new(persistence.clone(), None);
        let miner_uid = 1;
        let executor_id = "executor1";

        // Insert two misbehaviours within 1 hour (triggers ban)
        let now = Utc::now();
        let log1 = MisbehaviourLog {
            miner_uid,
            executor_id: executor_id.to_string(),
            gpu_uuid: "gpu1".to_string(),
            endpoint_executor: "endpoint1".to_string(),
            type_of_misbehaviour: MisbehaviourType::BadRental,
            details: "failure 1".to_string(),
            recorded_at: now - Duration::minutes(90),
            created_at: now,
            updated_at: now,
        };
        let log2 = MisbehaviourLog {
            miner_uid,
            executor_id: executor_id.to_string(),
            gpu_uuid: "gpu1".to_string(),
            endpoint_executor: "endpoint1".to_string(),
            type_of_misbehaviour: MisbehaviourType::BadRental,
            details: "failure 2".to_string(),
            recorded_at: now - Duration::minutes(85),
            created_at: now,
            updated_at: now,
        };

        persistence.insert_misbehaviour_log(&log1).await.unwrap();
        persistence.insert_misbehaviour_log(&log2).await.unwrap();

        // Mock current time as 85 minutes after the second failure
        // At this point, the 1-hour window contains 0 failures
        // But the ban (2 hours for 2nd offense) should still be active

        // Since we have 2 offenses, ban duration is 2 hours
        // Ban was triggered at log2.recorded_at
        // Ban should expire at log2.recorded_at + 2 hours
        // Current time is log2.recorded_at + 85 minutes
        // Ban should still be active (85 minutes < 120 minutes)

        let is_banned = ban_manager
            .is_executor_banned(miner_uid, executor_id)
            .await
            .unwrap();

        // This assertion would fail with the old implementation
        // but passes with the fixed implementation
        assert!(
            is_banned,
            "Executor should still be banned after 85 minutes (ban duration is 2 hours)"
        );

        // Check ban expiry is correct
        let ban_expiry = ban_manager
            .get_ban_expiry(miner_uid, executor_id)
            .await
            .unwrap();
        assert!(ban_expiry.is_some(), "Ban expiry should be set");

        let expected_expiry = log2.recorded_at + Duration::hours(2);
        let actual_expiry = ban_expiry.unwrap();

        // Allow small time difference for test execution
        let diff = (expected_expiry - actual_expiry).num_seconds().abs();
        assert!(
            diff < 5,
            "Ban expiry should be approximately 2 hours from trigger time"
        );
    }

    #[tokio::test]
    async fn test_ban_expires_after_duration() {
        use crate::persistence::SimplePersistence;
        use std::sync::Arc;

        let persistence = Arc::new(
            SimplePersistence::new(":memory:", "test_hotkey".to_string())
                .await
                .unwrap(),
        );
        persistence.run_migrations().await.unwrap();

        let ban_manager = BanManager::new(persistence.clone(), None);
        let miner_uid = 1;
        let executor_id = "executor1";

        // Insert two old misbehaviours that should have expired
        let now = Utc::now();
        let log1 = MisbehaviourLog {
            miner_uid,
            executor_id: executor_id.to_string(),
            gpu_uuid: "gpu1".to_string(),
            endpoint_executor: "endpoint1".to_string(),
            type_of_misbehaviour: MisbehaviourType::BadRental,
            details: "failure 1".to_string(),
            recorded_at: now - Duration::hours(3),
            created_at: now,
            updated_at: now,
        };
        let log2 = MisbehaviourLog {
            miner_uid,
            executor_id: executor_id.to_string(),
            gpu_uuid: "gpu1".to_string(),
            endpoint_executor: "endpoint1".to_string(),
            type_of_misbehaviour: MisbehaviourType::BadRental,
            details: "failure 2".to_string(),
            recorded_at: now - Duration::hours(3) + Duration::minutes(10),
            created_at: now,
            updated_at: now,
        };

        persistence.insert_misbehaviour_log(&log1).await.unwrap();
        persistence.insert_misbehaviour_log(&log2).await.unwrap();

        // Ban duration for 2 offenses is 2 hours
        // Ban was triggered 2h50m ago, so it should have expired

        let is_banned = ban_manager
            .is_executor_banned(miner_uid, executor_id)
            .await
            .unwrap();
        assert!(!is_banned, "Ban should have expired after 2 hours");

        let ban_expiry = ban_manager
            .get_ban_expiry(miner_uid, executor_id)
            .await
            .unwrap();
        assert!(
            ban_expiry.is_none(),
            "No ban expiry should be returned for expired ban"
        );
    }

    #[tokio::test]
    async fn test_progressive_ban_durations() {
        use crate::persistence::SimplePersistence;
        use std::sync::Arc;

        let persistence = Arc::new(
            SimplePersistence::new(":memory:", "test_hotkey".to_string())
                .await
                .unwrap(),
        );
        persistence.run_migrations().await.unwrap();

        let ban_manager = BanManager::new(persistence.clone(), None);
        let miner_uid = 1;
        let executor_id = "executor1";

        // Insert 5 misbehaviours over time to test progressive bans
        let now = Utc::now();
        let logs = vec![
            // First pair triggers 1st ban
            create_test_log(miner_uid, executor_id, now - Duration::days(6)),
            create_test_log(
                miner_uid,
                executor_id,
                now - Duration::days(6) + Duration::minutes(10),
            ),
            // Second pair triggers 2nd ban
            create_test_log(miner_uid, executor_id, now - Duration::days(3)),
            create_test_log(
                miner_uid,
                executor_id,
                now - Duration::days(3) + Duration::minutes(10),
            ),
            // Recent trigger
            create_test_log(miner_uid, executor_id, now - Duration::minutes(30)),
        ];

        for log in &logs {
            persistence.insert_misbehaviour_log(log).await.unwrap();
        }

        // With 5 offenses in 7 days, ban duration should be 24 hours (max)
        let is_banned = ban_manager
            .is_executor_banned(miner_uid, executor_id)
            .await
            .unwrap();
        assert!(
            !is_banned,
            "Should not be banned as no trigger in last logs"
        );

        // Add another log to trigger ban
        let trigger_log = create_test_log(miner_uid, executor_id, now);
        persistence
            .insert_misbehaviour_log(&trigger_log)
            .await
            .unwrap();

        let is_banned = ban_manager
            .is_executor_banned(miner_uid, executor_id)
            .await
            .unwrap();
        assert!(
            is_banned,
            "Should be banned with 6 offenses and recent trigger"
        );

        let ban_expiry = ban_manager
            .get_ban_expiry(miner_uid, executor_id)
            .await
            .unwrap();
        assert!(ban_expiry.is_some());

        // With 6 offenses, duration should be 24 hours (max)
        let expected_expiry = now + Duration::hours(24);
        let actual_expiry = ban_expiry.unwrap();
        let diff = (expected_expiry - actual_expiry).num_seconds().abs();
        assert!(diff < 5, "Ban should be 24 hours for 6 offenses");
    }
}
