use anyhow::Result;
use chrono::Utc;
use std::sync::Arc;
use tracing::{debug, info, warn};

use super::categorization::{MinerGpuProfile, NodeValidationResult};
use crate::metrics::ValidatorMetrics;
use crate::persistence::gpu_profile_repository::GpuProfileRepository;
use crate::persistence::SimplePersistence;
use cathedral_common::identity::MinerUid;

pub struct GpuScoringEngine {
    gpu_profile_repo: Arc<GpuProfileRepository>,
    /// Persistence layer for uptime tracking (used by gRPC uptime endpoint)
    #[allow(dead_code)]
    persistence: Arc<SimplePersistence>,
    metrics: Option<Arc<ValidatorMetrics>>,
}

impl GpuScoringEngine {
    pub fn new(
        gpu_profile_repo: Arc<GpuProfileRepository>,
        persistence: Arc<SimplePersistence>,
    ) -> Self {
        Self {
            gpu_profile_repo,
            persistence,
            metrics: None,
        }
    }

    /// Create new engine with metrics support
    pub fn with_metrics(
        gpu_profile_repo: Arc<GpuProfileRepository>,
        persistence: Arc<SimplePersistence>,
        metrics: Arc<ValidatorMetrics>,
    ) -> Self {
        Self {
            gpu_profile_repo,
            persistence,
            metrics: Some(metrics),
        }
    }

    /// Update miner profile from validation results
    pub async fn update_miner_profile_from_validation(
        &self,
        miner_uid: MinerUid,
        node_validations: Vec<NodeValidationResult>,
    ) -> Result<MinerGpuProfile> {
        // Calculate verification score from node results
        let new_score = self.calculate_verification_score(&node_validations);

        // Check if there are any successful validations
        let has_successful_validation = node_validations
            .iter()
            .any(|v| v.is_valid && v.attestation_valid);

        // Create or update the profile with the calculated score
        let mut profile = MinerGpuProfile::new(miner_uid, &node_validations, new_score);

        // If there's a successful validation, update the timestamp
        if has_successful_validation {
            profile.last_successful_validation = Some(Utc::now());
        }

        // Store the profile
        self.gpu_profile_repo.upsert_gpu_profile(&profile).await?;

        info!(
            miner_uid = miner_uid.as_u16(),
            score = new_score,
            total_gpus = profile.total_gpu_count(),
            validations = node_validations.len(),
            gpu_distribution = ?profile.gpu_counts,
            "Updated miner GPU profile with GPU count weighting"
        );

        // Record metrics if available
        if let Some(metrics) = &self.metrics {
            // Record miner GPU profile metrics
            metrics.prometheus().record_miner_gpu_count_and_score(
                miner_uid.as_u16(),
                profile.total_gpu_count(),
                new_score,
            );

            // Record individual node GPU counts
            for validation in &node_validations {
                if validation.is_valid && validation.attestation_valid {
                    metrics.prometheus().record_node_gpu_count(
                        miner_uid.as_u16(),
                        &validation.node_id,
                        &validation.gpu_model,
                        validation.gpu_count,
                    );

                    // Record successful validation
                    metrics.prometheus().record_miner_successful_validation(
                        miner_uid.as_u16(),
                        &validation.node_id,
                    );

                    // Record GPU profile
                    metrics.prometheus().record_miner_gpu_profile(
                        miner_uid.as_u16(),
                        &validation.gpu_model,
                        &validation.node_id,
                        validation.gpu_count as u32,
                    );

                    // Also record through business metrics for complete tracking
                    metrics
                        .business()
                        .record_gpu_profile_validation(
                            miner_uid.as_u16(),
                            &validation.node_id,
                            &validation.gpu_model,
                            validation.gpu_count,
                            validation.is_valid && validation.attestation_valid,
                            new_score,
                        )
                        .await;
                }
            }
        }

        Ok(profile)
    }

    /// Calculate verification score from node results
    fn calculate_verification_score(&self, node_validations: &[NodeValidationResult]) -> f64 {
        if node_validations.is_empty() {
            return 0.0;
        }

        let mut valid_count = 0;
        let mut total_count = 0;
        let mut total_gpu_count = 0;
        let mut unique_nodes = std::collections::HashSet::new();

        // count unique nodes and their GPU counts
        for validation in node_validations {
            unique_nodes.insert(&validation.node_id);
            total_count += 1;

            // Count valid attestations and accumulate GPU counts
            if validation.is_valid && validation.attestation_valid {
                valid_count += 1;
            }
        }

        // sum GPU counts from unique nodes only
        let mut seen_nodes = std::collections::HashSet::new();
        for validation in node_validations {
            if validation.is_valid
                && validation.attestation_valid
                && seen_nodes.insert(&validation.node_id)
            {
                total_gpu_count += validation.gpu_count;
            }
        }

        if total_count > 0 {
            // Calculate base pass/fail ratio
            let final_score = valid_count as f64 / total_count as f64;

            // Log the actual GPU-weighted score for transparency
            let gpu_weighted_score = final_score * total_gpu_count as f64;

            debug!(
                validations = node_validations.len(),
                valid_count = valid_count,
                total_count = total_count,
                unique_nodes = unique_nodes.len(),
                total_gpu_count = total_gpu_count,
                final_score = final_score,
                gpu_weighted_score = gpu_weighted_score,
                "Calculated verification score (normalized for DB, GPU count tracked separately)"
            );
            final_score
        } else {
            warn!(
                validations = node_validations.len(),
                "No validations found for score calculation"
            );
            0.0
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::persistence::gpu_profile_repository::GpuProfileRepository;
    use cathedral_common::identity::MinerUid;
    use chrono::{DateTime, Utc};
    use std::collections::HashMap;
    use std::sync::Arc;

    /// Helper function to create a test MinerGpuProfile without specific memory requirements
    fn create_test_profile(
        miner_uid: u16,
        gpu_counts: HashMap<String, u32>,
        total_score: f64,
        now: DateTime<Utc>,
    ) -> MinerGpuProfile {
        MinerGpuProfile {
            miner_uid: MinerUid::new(miner_uid),
            gpu_counts,
            total_score,
            verification_count: 1,
            last_updated: now,
            last_successful_validation: Some(now - chrono::Duration::hours(1)),
        }
    }

    /// Helper function to insert a test miner
    async fn insert_test_miner(
        persistence: &SimplePersistence,
        miner_id: &str,
        hotkey: &str,
        _registered_at: DateTime<Utc>,
    ) -> anyhow::Result<()> {
        let now = Utc::now();
        sqlx::query(
            "INSERT INTO miners (id, hotkey, endpoint, updated_at)
             VALUES (?, ?, ?, ?)",
        )
        .bind(miner_id)
        .bind(hotkey)
        .bind("127.0.0.1:8080")
        .bind(now.to_rfc3339())
        .execute(persistence.pool())
        .await?;
        Ok(())
    }

    /// Helper function to insert a test miner node
    async fn insert_test_miner_node(
        persistence: &SimplePersistence,
        miner_id: &str,
        node_id: &str,
        gpu_count: i64,
        created_at: DateTime<Utc>,
    ) -> anyhow::Result<()> {
        // Derive a unique IP from miner_id to satisfy the UNIQUE index on node_ip
        let uid: u16 = miner_id.trim_start_matches("miner_").parse().unwrap_or(0);
        let node_ip = format!("10.0.{}.{}", uid / 256, uid % 256);
        sqlx::query(
            "INSERT INTO miner_nodes (id, miner_id, node_id, ssh_endpoint, node_ip, gpu_count, status, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        .bind(format!("{}:{}", miner_id, node_id))
        .bind(miner_id)
        .bind(node_id)
        .bind(format!("root@{}:8080", node_ip))
        .bind(&node_ip)
        .bind(gpu_count)
        .bind("online")
        .bind(created_at.to_rfc3339())
        .execute(persistence.pool())
        .await?;
        Ok(())
    }

    /// Helper function to insert a test GPU UUID assignment
    async fn insert_test_gpu_uuid(
        persistence: &SimplePersistence,
        miner_id: &str,
        node_id: &str,
        gpu_name: &str,
    ) -> anyhow::Result<()> {
        let now = Utc::now();
        let gpu_uuid = format!("test-gpu-uuid-{}-{}", miner_id, node_id);
        sqlx::query(
            "INSERT INTO gpu_uuid_assignments (gpu_uuid, gpu_index, node_id, miner_id, gpu_name, last_verified)
             VALUES (?, ?, ?, ?, ?, ?)"
        )
        .bind(gpu_uuid)
        .bind(0i32)
        .bind(node_id)
        .bind(miner_id)
        .bind(gpu_name)
        .bind(now.to_rfc3339())
        .execute(persistence.pool())
        .await?;
        Ok(())
    }

    /// Helper function to insert a test verification log
    async fn insert_test_verification_log(
        persistence: &SimplePersistence,
        node_id: &str,
        timestamp: DateTime<Utc>,
        success: bool,
        with_binary_validation: bool,
    ) -> anyhow::Result<()> {
        let now = Utc::now();
        let log_id = uuid::Uuid::new_v4().to_string();
        let score = if success { 1.0 } else { 0.0 };
        let success_int = if success { 1i32 } else { 0i32 };
        let binary_validation = if with_binary_validation {
            Some("binary_validation_data")
        } else {
            None
        };

        sqlx::query(
            "INSERT INTO verification_logs (id, node_id, validator_hotkey, verification_type, timestamp, score, success, details, duration_ms, last_binary_validation, created_at, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        .bind(log_id)
        .bind(node_id)
        .bind("test_validator_hotkey")
        .bind("gpu_validation")
        .bind(timestamp.to_rfc3339())
        .bind(score)
        .bind(success_int)
        .bind("{}")
        .bind(1000i64)
        .bind(binary_validation)
        .bind(now.to_rfc3339())
        .bind(now.to_rfc3339())
        .execute(persistence.pool())
        .await?;
        Ok(())
    }

    async fn create_test_gpu_profile_repo(
    ) -> Result<(Arc<GpuProfileRepository>, Arc<SimplePersistence>)> {
        let persistence = Arc::new(crate::persistence::SimplePersistence::for_testing().await?);
        let repo = Arc::new(GpuProfileRepository::new(persistence.pool().clone()));
        Ok((repo, persistence))
    }

    #[tokio::test]
    async fn test_verification_score_calculation() {
        let (repo, persistence) = create_test_gpu_profile_repo().await.unwrap();
        let engine = GpuScoringEngine::new(repo, persistence);

        // Test with valid attestations
        let validations = vec![
            NodeValidationResult {
                node_id: "exec1".to_string(),
                is_valid: true,
                gpu_model: "A100".to_string(),
                gpu_count: 2,
                gpu_memory_gb: 80.0,
                attestation_valid: true,
                validation_timestamp: Utc::now(),
            },
            NodeValidationResult {
                node_id: "exec2".to_string(),
                is_valid: true,
                gpu_model: "A100".to_string(),
                gpu_count: 1,
                gpu_memory_gb: 80.0,
                attestation_valid: true,
                validation_timestamp: Utc::now(),
            },
        ];

        let score = engine.calculate_verification_score(&validations);
        // 2 valid validations: validation_ratio = 1.0
        // Actual GPU weight = 1.0 * 3 = 3.0
        let expected = 1.0;
        assert!((score - expected).abs() < 0.001);

        // Test with invalid attestations
        let invalid_validations = vec![NodeValidationResult {
            node_id: "exec1".to_string(),
            is_valid: false,
            gpu_model: "A100".to_string(),
            gpu_count: 2,
            gpu_memory_gb: 80.0,
            attestation_valid: false,
            validation_timestamp: Utc::now(),
        }];

        let score = engine.calculate_verification_score(&invalid_validations);
        assert_eq!(score, 0.0);

        // Test with mixed results
        let mixed_validations = vec![
            NodeValidationResult {
                node_id: "exec1".to_string(),
                is_valid: true,
                gpu_model: "A100".to_string(),
                gpu_count: 2,
                gpu_memory_gb: 80.0,
                attestation_valid: true,
                validation_timestamp: Utc::now(),
            },
            NodeValidationResult {
                node_id: "exec2".to_string(),
                is_valid: false,
                gpu_model: "A100".to_string(),
                gpu_count: 1,
                gpu_memory_gb: 80.0,
                attestation_valid: false,
                validation_timestamp: Utc::now(),
            },
        ];

        let score = engine.calculate_verification_score(&mixed_validations);
        // 1 valid out of 2 = 0.5 validation ratio
        // Actual GPU weight = 0.5 * 2 = 1.0
        let expected = 0.5;
        assert!((score - expected).abs() < 0.001);

        // Test with empty validations
        let empty_validations = vec![];
        let score = engine.calculate_verification_score(&empty_validations);
        assert_eq!(score, 0.0);

        // Test that pass/fail scoring gives 1.0 for valid attestations regardless of memory
        let high_memory_validations = vec![NodeValidationResult {
            node_id: "exec1".to_string(),
            is_valid: true,
            gpu_model: "A100".to_string(),
            gpu_count: 1,
            gpu_memory_gb: 80.0,
            attestation_valid: true,
            validation_timestamp: Utc::now(),
        }];

        let low_memory_validations = vec![NodeValidationResult {
            node_id: "exec1".to_string(),
            is_valid: true,
            gpu_model: "A100".to_string(),
            gpu_count: 1,
            gpu_memory_gb: 16.0,
            attestation_valid: true,
            validation_timestamp: Utc::now(),
        }];

        let high_score = engine.calculate_verification_score(&high_memory_validations);
        let low_score = engine.calculate_verification_score(&low_memory_validations);
        // Actual GPU weight = 1.0 * 1 = 1.0
        assert_eq!(high_score, 1.0);
        assert_eq!(low_score, 1.0);
    }

    #[tokio::test]
    async fn test_gpu_count_weighting() {
        let (repo, persistence) = create_test_gpu_profile_repo().await.unwrap();
        let engine = GpuScoringEngine::new(repo, persistence);

        // Test different GPU counts
        for gpu_count in 1..=8 {
            let validations = vec![NodeValidationResult {
                node_id: format!("exec_{gpu_count}"),
                is_valid: true,
                gpu_model: "A100".to_string(),
                gpu_count,
                gpu_memory_gb: 80.0,
                attestation_valid: true,
                validation_timestamp: Utc::now(),
            }];

            let score = engine.calculate_verification_score(&validations);
            let expected_score = 1.0;
            assert!(
                (score - expected_score).abs() < 0.001,
                "GPU count {gpu_count} should give score {expected_score}, got {score}"
            );
        }

        // Test with many GPUs (no cap, linear scaling)
        let many_gpu_validations = vec![NodeValidationResult {
            node_id: "exec_many".to_string(),
            is_valid: true,
            gpu_model: "A100".to_string(),
            gpu_count: 128,
            gpu_memory_gb: 80.0,
            attestation_valid: true,
            validation_timestamp: Utc::now(),
        }];

        let score = engine.calculate_verification_score(&many_gpu_validations);
        assert_eq!(score, 1.0);
    }

    #[tokio::test]
    async fn test_miner_profile_update() {
        let (repo, persistence) = create_test_gpu_profile_repo().await.unwrap();
        let engine = GpuScoringEngine::new(repo, persistence);

        let miner_uid = MinerUid::new(1);
        let validations = vec![NodeValidationResult {
            node_id: "exec1".to_string(),
            is_valid: true,
            gpu_model: "A100".to_string(),
            gpu_count: 2,
            gpu_memory_gb: 80.0,
            attestation_valid: true,
            validation_timestamp: Utc::now(),
        }];

        // Test new profile creation
        let profile = engine
            .update_miner_profile_from_validation(miner_uid, validations)
            .await
            .unwrap();
        assert_eq!(profile.miner_uid, miner_uid);
        assert!(profile.total_score > 0.0);

        // Test existing profile update with different memory
        let new_validations = vec![NodeValidationResult {
            node_id: "exec2".to_string(),
            is_valid: true,
            gpu_model: "A100".to_string(),
            gpu_count: 1,
            gpu_memory_gb: 40.0, // Different memory than first validation (80GB)
            attestation_valid: true,
            validation_timestamp: Utc::now(),
        }];

        let updated_profile = engine
            .update_miner_profile_from_validation(miner_uid, new_validations)
            .await
            .unwrap();
        assert_eq!(updated_profile.miner_uid, miner_uid);
        assert_eq!(updated_profile.total_score, 1.0);
    }

    #[tokio::test]
    async fn test_pass_fail_scoring_edge_cases() {
        let (repo, persistence) = create_test_gpu_profile_repo().await.unwrap();
        let engine = GpuScoringEngine::new(repo, persistence);

        // Test all invalid validations
        let all_invalid = vec![
            NodeValidationResult {
                node_id: "exec1".to_string(),
                is_valid: false,
                gpu_model: "A100".to_string(),
                gpu_count: 1,
                gpu_memory_gb: 80.0,
                attestation_valid: false,
                validation_timestamp: Utc::now(),
            },
            NodeValidationResult {
                node_id: "exec2".to_string(),
                is_valid: true,
                gpu_model: "A100".to_string(),
                gpu_count: 1,
                gpu_memory_gb: 80.0,
                attestation_valid: false, // Attestation invalid
                validation_timestamp: Utc::now(),
            },
        ];

        let score = engine.calculate_verification_score(&all_invalid);
        assert_eq!(score, 0.0); // All failed

        // Test partial success
        let partial_success = vec![
            NodeValidationResult {
                node_id: "exec1".to_string(),
                is_valid: true,
                gpu_model: "A100".to_string(),
                gpu_count: 1,
                gpu_memory_gb: 80.0,
                attestation_valid: true,
                validation_timestamp: Utc::now(),
            },
            NodeValidationResult {
                node_id: "exec2".to_string(),
                is_valid: false,
                gpu_model: "A100".to_string(),
                gpu_count: 1,
                gpu_memory_gb: 80.0,
                attestation_valid: false,
                validation_timestamp: Utc::now(),
            },
            NodeValidationResult {
                node_id: "exec3".to_string(),
                is_valid: true,
                gpu_model: "A100".to_string(),
                gpu_count: 1,
                gpu_memory_gb: 40.0,
                attestation_valid: true,
                validation_timestamp: Utc::now(),
            },
        ];

        let score = engine.calculate_verification_score(&partial_success);
        let expected = 2.0 / 3.0; // Stored score is validation ratio
        assert!((score - expected).abs() < 0.001);
    }

    #[tokio::test]
    async fn test_direct_score_update() {
        let (repo, persistence) = create_test_gpu_profile_repo().await.unwrap();
        let engine = GpuScoringEngine::new(repo.clone(), persistence);

        let miner_uid = MinerUid::new(100);

        // Create initial profile with score 0.2
        let mut gpu_counts = HashMap::new();
        gpu_counts.insert("A100".to_string(), 1);
        let mut initial_profile = create_test_profile(100, gpu_counts, 0.2, Utc::now());
        initial_profile.last_successful_validation = None;
        repo.upsert_gpu_profile(&initial_profile).await.unwrap();

        // Update with new validations that would give score 1.0
        let validations = vec![NodeValidationResult {
            node_id: "exec1".to_string(),
            is_valid: true,
            gpu_model: "A100".to_string(),
            gpu_count: 1,
            gpu_memory_gb: 80.0,
            attestation_valid: true,
            validation_timestamp: Utc::now(),
        }];

        let profile = engine
            .update_miner_profile_from_validation(miner_uid, validations)
            .await
            .unwrap();

        assert_eq!(profile.total_score, 1.0);
    }

    #[tokio::test]
    async fn test_scoring_ignores_gpu_memory() {
        let (repo, persistence) = create_test_gpu_profile_repo().await.unwrap();
        let engine = GpuScoringEngine::new(repo, persistence);

        // Test various memory sizes all get same score
        let memory_sizes = vec![16, 24, 40, 80, 100];

        for memory in memory_sizes {
            let validations = vec![NodeValidationResult {
                node_id: format!("exec_{memory}"),
                is_valid: true,
                gpu_model: "A100".to_string(),
                gpu_count: 1,
                gpu_memory_gb: memory as f64,
                attestation_valid: true,
                validation_timestamp: Utc::now(),
            }];

            let score = engine.calculate_verification_score(&validations);
            assert_eq!(score, 1.0, "Memory {memory} should give score 1.0");
        }
    }

    #[tokio::test]
    async fn test_uptime_ramp_up_calculation() {
        // Test cases for different uptime durations
        let test_cases = vec![
            (0.0, 0.0),       // 0 minutes = 0%
            (1440.0, 0.0714), // 1 day = 7.14%
            (4320.0, 0.2143), // 3 days = 21.43%
            (10080.0, 0.5),   // 7 days = 50%
            (20160.0, 1.0),   // 14 days = 100%
            (43200.0, 1.0),   // 30 days = 100% (capped)
        ];

        for (uptime_minutes, expected_multiplier) in test_cases {
            const FULL_WEIGHT_MINUTES: f64 = 20_160.0;
            let multiplier = uptime_minutes / FULL_WEIGHT_MINUTES;
            let actual = multiplier.min(1.0);
            assert!(
                (actual - expected_multiplier).abs() < 0.0001,
                "For {uptime_minutes} minutes, expected {expected_multiplier}, got {actual}"
            );
        }
    }

    #[tokio::test]
    async fn test_new_node_with_no_verifications() {
        let (_, persistence) = create_test_gpu_profile_repo().await.unwrap();

        let miner_id = "miner_999";
        let node_id = "test_node_new";
        let now = Utc::now();

        // Create miner and node without any verification logs
        insert_test_miner(&persistence, miner_id, "hotkey_999", now)
            .await
            .unwrap();
        insert_test_miner_node(&persistence, miner_id, node_id, 1, now)
            .await
            .unwrap();

        // Should get (0.0, 0.0) - no uptime and no multiplier (no GPU UUID assigned)
        let (uptime_minutes, multiplier) = persistence
            .calculate_node_uptime_multiplier(miner_id, node_id)
            .await
            .unwrap();

        assert_eq!(
            uptime_minutes, 0.0,
            "New node without GPU UUID should get 0.0 uptime minutes"
        );
        assert_eq!(
            multiplier, 0.0,
            "New node without GPU UUID should get 0.0 multiplier"
        );
    }

    #[tokio::test]
    async fn test_node_with_continuous_success() {
        let (_, persistence) = create_test_gpu_profile_repo().await.unwrap();

        let miner_id = "miner_1000";
        let node_id = "test_node_success";
        let now = Utc::now();
        let seven_days_ago = now - chrono::Duration::days(7);

        // Create miner and node
        insert_test_miner(&persistence, miner_id, "hotkey_1000", seven_days_ago)
            .await
            .unwrap();
        insert_test_miner_node(&persistence, miner_id, node_id, 1, seven_days_ago)
            .await
            .unwrap();

        // Add GPU UUID
        insert_test_gpu_uuid(&persistence, miner_id, node_id, "A100")
            .await
            .unwrap();

        // Add verification logs showing 7 days of continuous success
        insert_test_verification_log(&persistence, node_id, seven_days_ago, true, true)
            .await
            .unwrap();

        // Should get ~0.5 multiplier (7 days out of 14)
        let (_uptime_minutes, multiplier) = persistence
            .calculate_node_uptime_multiplier(miner_id, node_id)
            .await
            .unwrap();

        assert!(
            (multiplier - 0.5).abs() < 0.01,
            "Node with 7 days uptime should get ~0.5 multiplier, got {multiplier}"
        );
    }

    #[tokio::test]
    async fn test_node_with_failure_resets_uptime() {
        let (_, persistence) = create_test_gpu_profile_repo().await.unwrap();

        let miner_id = "miner_1001";
        let node_id = "test_node_failure";
        let now = Utc::now();
        let seven_days_ago = now - chrono::Duration::days(7);
        let two_days_ago = now - chrono::Duration::days(2);
        let one_day_ago = now - chrono::Duration::days(1);

        // Create miner and node
        insert_test_miner(&persistence, miner_id, "hotkey_1001", seven_days_ago)
            .await
            .unwrap();
        insert_test_miner_node(&persistence, miner_id, node_id, 1, seven_days_ago)
            .await
            .unwrap();

        // Add GPU UUID
        insert_test_gpu_uuid(&persistence, miner_id, node_id, "A100")
            .await
            .unwrap();

        // Add verification logs: success 7 days ago, failure 2 days ago, success 1 day ago
        // Only the last success period (1 day) should count
        insert_test_verification_log(&persistence, node_id, seven_days_ago, true, true)
            .await
            .unwrap();

        // Failure 2 days ago - this resets uptime
        insert_test_verification_log(&persistence, node_id, two_days_ago, false, true)
            .await
            .unwrap();

        // Success 1 day ago - starts new uptime period
        insert_test_verification_log(&persistence, node_id, one_day_ago, true, true)
            .await
            .unwrap();

        // Should get ~0.071 multiplier (1 day out of 14, ~7.14%)
        let (_uptime_minutes, multiplier) = persistence
            .calculate_node_uptime_multiplier(miner_id, node_id)
            .await
            .unwrap();

        assert!(
            multiplier < 0.1,
            "Node with failure should only count uptime from last success, got {multiplier}"
        );
    }
}
