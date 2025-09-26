//! # Verification Engine
//!
//! Handles the actual verification of miners and their nodes.
//! Implements Single Responsibility Principle by focusing only on verification logic.

use super::miner_client::{MinerClient, MinerClientConfig};
use super::types::MinerInfo;
use super::types::{GpuInfo, NodeInfoDetailed, NodeVerificationResult, ValidationType};
use super::validation_strategy::{ValidationNode, ValidationStrategy, ValidationStrategySelector};
use super::validation_worker::{ValidationWorkerQueue, WorkerQueueConfig};
use crate::config::VerificationConfig;
use crate::gpu::{categorization::GpuCategory, MinerGpuProfile};
use crate::metrics::ValidatorMetrics;
use crate::persistence::{
    entities::VerificationLog, gpu_profile_repository::GpuProfileRepository, SimplePersistence,
};
use crate::ssh::{ValidatorSshClient, ValidatorSshKeyManager};
use anyhow::{Context, Result};
use basilica_common::identity::{Hotkey, MinerUid, NodeId};
use chrono::Utc;
use futures::future::join_all;
use sqlx::Row;
use std::collections::HashMap;
use std::path::PathBuf;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;
use tracing::{debug, error, info, warn};

#[derive(Clone)]
pub struct VerificationEngine {
    config: VerificationConfig,
    miner_client_config: MinerClientConfig,
    validator_hotkey: Hotkey,
    /// Database persistence for storing verification results
    persistence: Arc<SimplePersistence>,
    /// Whether to use dynamic discovery or fall back to static config
    use_dynamic_discovery: bool,
    /// SSH key path for node access (fallback)
    ssh_key_path: Option<PathBuf>,
    /// Optional Bittensor service for signing
    bittensor_service: Option<Arc<bittensor::Service>>,
    /// SSH key manager for session keys
    ssh_key_manager: Option<Arc<ValidatorSshKeyManager>>,
    /// Validation strategy selector for determining validation approach
    validation_strategy_selector: Arc<ValidationStrategySelector>,
    /// Validation node for running validation strategies
    validation_node: Arc<tokio::sync::RwLock<ValidationNode>>,
    /// Optional worker queue for decoupled execution
    worker_queue: Option<Arc<ValidationWorkerQueue>>,
}

impl VerificationEngine {
    /// Check if an endpoint is invalid
    fn is_invalid_endpoint(&self, endpoint: &str) -> bool {
        // Check for common invalid patterns
        if endpoint.contains("0:0:0:0:0:0:0:0")
            || endpoint.contains("0.0.0.0")
            || endpoint.is_empty()
            || !endpoint.starts_with("http")
        {
            debug!("Invalid endpoint detected: {}", endpoint);
            return true;
        }

        // Validate URL parsing
        if let Ok(url) = url::Url::parse(endpoint) {
            if let Some(host) = url.host_str() {
                // Check for zero or loopback addresses that indicate invalid configuration
                if host == "0.0.0.0" || host == "::" || host == "localhost" || host == "127.0.0.1" {
                    debug!("Invalid host in endpoint: {}", endpoint);
                    return true;
                }
            } else {
                debug!("No host found in endpoint: {}", endpoint);
                return true;
            }
        } else {
            debug!("Failed to parse endpoint as URL: {}", endpoint);
            return true;
        }

        false
    }

    /// Execute complete automated verification workflow with SSH session management (specs-compliant)
    pub async fn execute_verification_workflow(
        &self,
        task: &super::scheduler::VerificationTask,
    ) -> Result<VerificationResult> {
        info!(
            miner_uid = task.miner_uid,
            intended_strategy = ?task.intended_validation_strategy,
            "[EVAL_FLOW] Executing verification workflow for miner {} (intended strategy: {:?})",
            task.miner_uid, task.intended_validation_strategy
        );

        let workflow_start = std::time::Instant::now();
        let mut verification_steps = Vec::new();

        // Step 1: Get nodes from discovery + database fallback
        let discovered_nodes = self
            .discover_miner_nodes(&task.miner_endpoint, &task.miner_hotkey)
            .await
            .unwrap_or_else(|e| {
                warn!(
                    "Failed to discover nodes for miner {} via gRPC: {}. Using database fallback.",
                    task.miner_uid, e
                );
                Vec::new()
            });

        let known_node_data = self
            .persistence
            .get_known_nodes_for_miner(task.miner_uid)
            .await?;
        let known_nodes = self.convert_db_data_to_node_info(known_node_data, task.miner_uid)?;
        let node_list = self.combine_node_lists(discovered_nodes, known_nodes);

        verification_steps.push(VerificationStep {
            step_name: "node_discovery".to_string(),
            status: StepStatus::Completed,
            duration: workflow_start.elapsed(),
            details: format!("Found {} nodes for verification", node_list.len()),
        });

        if node_list.is_empty() {
            info!(
                miner_uid = task.miner_uid,
                intended_strategy = ?task.intended_validation_strategy,
                "[EVAL_FLOW] No nodes found for miner {}", task.miner_uid
            );

            return Ok(VerificationResult {
                miner_uid: task.miner_uid,
                overall_score: 0.0,
                verification_steps,
                completed_at: chrono::Utc::now(),
                error: Some("No nodes found for miner".to_string()),
            });
        }

        // Route to worker queue if enabled
        if let Some(ref worker_queue) = self.worker_queue {
            info!(
                miner_uid = task.miner_uid,
                node_count = node_list.len(),
                intended_strategy = ?task.intended_validation_strategy,
                "[EVAL_FLOW] Routing {} nodes to worker queue for miner {}",
                node_list.len(),
                task.miner_uid
            );
            return self
                .route_to_worker_queue(
                    node_list,
                    task,
                    worker_queue,
                    &mut verification_steps,
                    workflow_start,
                )
                .await;
        }

        // Step 2: Execute SSH-based verification for each node
        let mut node_results = Vec::new();
        let mut nodes_skipped_for_strategy = 0;
        let total_nodes = node_list.len();

        info!(
            miner_uid = task.miner_uid,
            node_count = total_nodes,
            intended_strategy = ?task.intended_validation_strategy,
            "[EVAL_FLOW] Starting nodes verification"
        );

        // Create futures for all node validations
        let validation_futures: Vec<_> = node_list
            .into_iter()
            .map(|node_info| {
                let self_clone = self.clone();
                let miner_endpoint = task.miner_endpoint.clone();
                let miner_uid = task.miner_uid;
                let miner_hotkey = task.miner_hotkey.clone();
                let intended_strategy = task.intended_validation_strategy;

                async move {
                    info!(
                        miner_uid = miner_uid,
                        node_id = %node_info.id,
                        intended_strategy = ?intended_strategy,
                        "[EVAL_FLOW] Starting verification for node"
                    );

                    let result = self_clone
                        .verify_node(
                            &miner_endpoint,
                            &node_info,
                            miner_uid,
                            &miner_hotkey,
                            intended_strategy,
                        )
                        .await;

                    (node_info, result)
                }
            })
            .collect();

        // Execute all validations concurrently
        let validation_results = join_all(validation_futures).await;

        // Process all validation results
        for (node_info, result) in validation_results {
            match result {
                Ok(result) => {
                    let score = result.verification_score;
                    info!(
                        miner_uid = task.miner_uid,
                        node_id = %node_info.id,
                        verification_score = score,
                        intended_strategy = ?task.intended_validation_strategy,
                        "[EVAL_FLOW] SSH verification completed"
                    );
                    node_results.push(result);
                    verification_steps.push(VerificationStep {
                        step_name: format!("ssh_verification_{}", node_info.id),
                        status: StepStatus::Completed,
                        duration: workflow_start.elapsed(),
                        details: format!("SSH verification completed, score: {score}"),
                    });
                }
                Err(e) if e.to_string().contains("Strategy mismatch") => {
                    nodes_skipped_for_strategy += 1;
                    debug!(
                        miner_uid = task.miner_uid,
                        node_id = %node_info.id,
                        pipeline_type = ?task.intended_validation_strategy,
                        intended_strategy = ?task.intended_validation_strategy,
                        "[EVAL_FLOW] Node requires different validation type, will be handled by other pipeline"
                    );
                    verification_steps.push(VerificationStep {
                        step_name: format!("ssh_verification_{}", node_info.id),
                        status: StepStatus::Completed,
                        duration: workflow_start.elapsed(),
                        details: "Skipped - handled by other validation pipeline".to_string(),
                    });
                }
                Err(e) => {
                    error!(
                        miner_uid = task.miner_uid,
                        node_id = %node_info.id,
                        error = %e,
                        intended_strategy = ?task.intended_validation_strategy,
                        "[EVAL_FLOW] verification failed"
                    );
                    verification_steps.push(VerificationStep {
                        step_name: format!("ssh_verification_{}", node_info.id),
                        status: StepStatus::Failed,
                        duration: workflow_start.elapsed(),
                        details: format!("SSH verification error: {e}"),
                    });
                }
            }
        }

        // Step 3: Calculate overall verification score
        let overall_score = if node_results.is_empty() {
            // Only return 0 if ALL nodes were skipped for strategy mismatch
            // If we have no results and all were skipped, this pipeline isn't responsible for this miner
            if nodes_skipped_for_strategy == total_nodes && total_nodes > 0 {
                debug!(
                    miner_uid = task.miner_uid,
                    intended_strategy = ?task.intended_validation_strategy,
                    skipped_count = nodes_skipped_for_strategy,
                    pipeline_type = ?task.intended_validation_strategy,
                    "[EVAL_FLOW] All nodes require different validation type, score will come from other pipeline"
                );
            }
            0.0
        } else {
            let avg_score = node_results
                .iter()
                .map(|r| r.verification_score)
                .sum::<f64>()
                / node_results.len() as f64;

            info!(
                miner_uid = task.miner_uid,
                intended_strategy = ?task.intended_validation_strategy,
                validated_count = node_results.len(),
                skipped_count = nodes_skipped_for_strategy,
                total_nodes = total_nodes,
                average_score = avg_score,
                pipeline_type = ?task.intended_validation_strategy,
                "[EVAL_FLOW] Validation completed for miner"
            );

            avg_score
        };

        // Step 4: Store individual node verification results
        // Construct MinerInfo from task data
        let hotkey = Hotkey::new(task.miner_hotkey.clone())
            .map_err(|e| anyhow::anyhow!("Invalid miner hotkey '{}': {}", task.miner_hotkey, e))?;

        let miner_info = MinerInfo {
            uid: MinerUid::new(task.miner_uid),
            hotkey,
            endpoint: task.miner_endpoint.clone(),
            is_validator: task.is_validator,
            stake_tao: task.stake_tao,
            last_verified: None,
            verification_score: overall_score,
        };

        for result in &node_results {
            self.store_node_verification_result_with_miner_info(
                task.miner_uid,
                result,
                &miner_info,
            )
            .await?;
        }

        verification_steps.push(VerificationStep {
            step_name: "result_storage".to_string(),
            status: StepStatus::Completed,
            duration: workflow_start.elapsed(),
            details: format!("Stored verification result with score: {overall_score:.2}"),
        });

        info!(
            miner_uid = task.miner_uid,
            intended_strategy = ?task.intended_validation_strategy,
            validated_nodes = node_results.len(),
            skipped_nodes = nodes_skipped_for_strategy,
            total_nodes = total_nodes,
            pipeline_type = ?task.intended_validation_strategy,
            overall_score = overall_score,
            "[EVAL_FLOW] Verification workflow completed for miner {} in {:?}, score: {:.2} ({} of {} nodes validated in {} pipeline)",
            task.miner_uid,
            workflow_start.elapsed(),
            overall_score,
            node_results.len(),
            total_nodes,
            match task.intended_validation_strategy {
                ValidationType::Full => "full",
                ValidationType::Lightweight => "lightweight",
            }
        );

        Ok(VerificationResult {
            miner_uid: task.miner_uid,
            overall_score,
            verification_steps,
            completed_at: chrono::Utc::now(),
            error: None,
        })
    }

    /// Route nodes to worker queue for parallel processing
    async fn route_to_worker_queue(
        &self,
        nodes: Vec<NodeInfoDetailed>,
        task: &super::scheduler::VerificationTask,
        worker_queue: &Arc<ValidationWorkerQueue>,
        verification_steps: &mut Vec<VerificationStep>,
        workflow_start: std::time::Instant,
    ) -> Result<VerificationResult> {
        // Publish all nodes to the appropriate queue
        let mut published_count = 0;
        let mut failed_count = 0;

        for node in nodes {
            match worker_queue.publish(node, task.clone()).await {
                Ok(_) => published_count += 1,
                Err(e) => {
                    warn!("Failed to publish node to queue: {}", e);
                    failed_count += 1;
                }
            }
        }

        verification_steps.push(VerificationStep {
            step_name: "queue_dispatch".to_string(),
            status: if failed_count == 0 {
                StepStatus::Completed
            } else {
                StepStatus::PartialSuccess
            },
            duration: workflow_start.elapsed(),
            details: format!(
                "Published {} nodes to queue ({} failed)",
                published_count, failed_count
            ),
        });

        // Return result indicating work was queued
        // Actual scores will be updated asynchronously by workers
        Ok(VerificationResult {
            miner_uid: task.miner_uid,
            overall_score: 0.0,
            verification_steps: verification_steps.clone(),
            completed_at: chrono::Utc::now(),
            error: if published_count == 0 {
                Some("Failed to publish any nodes to queue".to_string())
            } else {
                None
            },
        })
    }

    /// Discover nodes from miner via gRPC
    async fn discover_miner_nodes(
        &self,
        miner_endpoint: &str,
        miner_hotkey: &str,
    ) -> Result<Vec<NodeInfoDetailed>> {
        info!(
            "[EVAL_FLOW] Starting node discovery from miner at: {}",
            miner_endpoint
        );
        debug!("[EVAL_FLOW] Using config: timeout={:?}, grpc_port_offset={:?}, use_dynamic_discovery={}",
               self.config.discovery_timeout, self.config.grpc_port_offset, self.use_dynamic_discovery);

        // Validate endpoint before attempting connection
        if self.is_invalid_endpoint(miner_endpoint) {
            error!(
                "[EVAL_FLOW] Invalid miner endpoint detected: {}",
                miner_endpoint
            );
            return Err(anyhow::anyhow!(
                "Invalid miner endpoint: {}. Skipping discovery.",
                miner_endpoint
            ));
        }
        info!(
            "[EVAL_FLOW] Endpoint validation passed for: {}",
            miner_endpoint
        );

        // Create authenticated miner client
        info!(
            "[EVAL_FLOW] Creating authenticated miner client with validator hotkey: {}",
            self.validator_hotkey
                .to_string()
                .chars()
                .take(8)
                .collect::<String>()
                + "..."
        );
        let client = self.create_authenticated_client()?;

        // Connect and authenticate to miner
        info!(
            "[EVAL_FLOW] Attempting gRPC connection to miner at: {}",
            miner_endpoint
        );
        let connection_start = std::time::Instant::now();
        let mut connection = match client
            .connect_and_authenticate(miner_endpoint, miner_hotkey)
            .await
        {
            Ok(conn) => {
                info!(
                    "[EVAL_FLOW] Successfully connected and authenticated to miner in {:?}",
                    connection_start.elapsed()
                );
                conn
            }
            Err(e) => {
                error!(
                    "[EVAL_FLOW] Failed to connect to miner at {} after {:?}: {}",
                    miner_endpoint,
                    connection_start.elapsed(),
                    e
                );
                return Err(e).context("Failed to connect to miner for node discovery");
            }
        };

        // Request nodes with requirements
        let requirements = basilica_protocol::common::ResourceLimits {
            max_cpu_cores: 4,
            max_memory_mb: 8192,
            max_storage_mb: 10240,
            max_containers: 1,
            max_bandwidth_mbps: 100.0,
            max_gpus: 1,
        };

        let lease_duration = Duration::from_secs(3600); // 1 hour lease

        info!("[EVAL_FLOW] Requesting nodes with requirements: cpu_cores={}, memory_mb={}, storage_mb={}, max_gpus={}, lease_duration={:?}",
              requirements.max_cpu_cores, requirements.max_memory_mb, requirements.max_storage_mb,
              requirements.max_gpus, lease_duration);

        let request_start = std::time::Instant::now();
        let node_details = match connection
            .request_nodes(Some(requirements), lease_duration)
            .await
        {
            Ok(details) => {
                info!(
                    "[EVAL_FLOW] Successfully received node details in {:?}, count={}",
                    request_start.elapsed(),
                    details.len()
                );
                for (i, detail) in details.iter().enumerate() {
                    info!(
                        "[EVAL_FLOW] Node {}: id={}, endpoint={}:{}",
                        i, detail.node_id, detail.host, detail.port
                    );
                }
                details
            }
            Err(e) => {
                error!(
                    "[EVAL_FLOW] Failed to request nodes from miner after {:?}: {}",
                    request_start.elapsed(),
                    e
                );
                return Ok(vec![]);
            }
        };

        let node_count = node_details.len();
        let nodes: Vec<NodeInfoDetailed> = node_details
            .into_iter()
            .map(|details| -> Result<NodeInfoDetailed> {
                Ok(NodeInfoDetailed {
                    id: NodeId::from_str(&details.node_id).map_err(|e| {
                        anyhow::anyhow!("Invalid node ID '{}': {}", details.node_id, e)
                    })?,
                    status: "available".to_string(),
                    capabilities: vec!["gpu".to_string()],
                    grpc_endpoint: format!("{}:{}", details.host, details.port),
                })
            })
            .collect::<Result<Vec<_>, _>>()?;

        info!(
            "[EVAL_FLOW] Node discovery completed: {} nodes mapped from {} details",
            nodes.len(),
            node_count
        );

        Ok(nodes)
    }

    /// Clean up GPU assignments for an node
    async fn cleanup_gpu_assignments(
        &self,
        node_id: &str,
        miner_id: &str,
        tx: Option<&mut sqlx::Transaction<'_, sqlx::Sqlite>>,
    ) -> Result<u64> {
        let query = "DELETE FROM gpu_uuid_assignments WHERE node_id = ? AND miner_id = ?";

        let rows_affected = if let Some(transaction) = tx {
            sqlx::query(query)
                .bind(node_id)
                .bind(miner_id)
                .execute(&mut **transaction)
                .await?
                .rows_affected()
        } else {
            sqlx::query(query)
                .bind(node_id)
                .bind(miner_id)
                .execute(self.persistence.pool())
                .await?
                .rows_affected()
        };

        if rows_affected > 0 {
            info!(
                "Cleaned up {} GPU assignments for node {} (miner: {})",
                rows_affected, node_id, miner_id
            );
        }

        Ok(rows_affected)
    }

    /// Helper function to clean up active SSH session for a node
    async fn cleanup_active_session(&self, node_id: &str) {
        // Direct SSH sessions are now managed at connection level
        // No explicit release needed
        let _ = node_id; // Avoid unused parameter warning
    }

    /// Store node verification result with actual miner information
    pub async fn store_node_verification_result_with_miner_info(
        &self,
        miner_uid: u16,
        node_result: &NodeVerificationResult,
        miner_info: &super::types::MinerInfo,
    ) -> Result<()> {
        info!(
            miner_uid = miner_uid,
            node_id = %node_result.node_id,
            verification_score = node_result.verification_score,
            validation_type = %node_result.validation_type,
            "Storing node verification result to database for miner {}, node {}: score={:.2}",
            miner_uid, node_result.node_id, node_result.verification_score
        );

        // Create verification log entry for database storage
        let success = match node_result.validation_type {
            ValidationType::Lightweight => node_result.ssh_connection_successful,
            ValidationType::Full => {
                node_result.ssh_connection_successful && node_result.binary_validation_successful
            }
        };

        let verification_log = VerificationLog::new(
            node_result.node_id.to_string(),
            self.validator_hotkey.to_string(),
            "ssh_automation".to_string(),
            node_result.verification_score,
            success,
            serde_json::json!({
                "miner_uid": miner_uid,
                "node_id": node_result.node_id.to_string(),
                "ssh_connection_successful": node_result.ssh_connection_successful,
                "binary_validation_successful": node_result.binary_validation_successful,
                "verification_method": "ssh_automation",
                "node_result": node_result.node_result,
                "gpu_count": node_result.gpu_count,
                "score_details": {
                    "verification_score": node_result.verification_score,
                    "ssh_score": if node_result.ssh_connection_successful { 0.5 } else { 0.0 },
                    "binary_score": if node_result.binary_validation_successful { 0.5 } else { 0.0 }
                }
            }),
            node_result.execution_time.as_millis() as i64,
            if !node_result.ssh_connection_successful {
                Some("SSH connection failed".to_string())
            } else if node_result.validation_type == ValidationType::Full
                && !node_result.binary_validation_successful
            {
                Some("Binary validation failed".to_string())
            } else {
                None
            },
        );

        // Store directly to database to avoid repository trait issues
        let query = r#"
            INSERT INTO verification_logs (
                id, node_id, validator_hotkey, verification_type, timestamp,
                score, success, details, duration_ms, error_message, created_at, updated_at,
                last_binary_validation, last_binary_validation_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        "#;

        let now = chrono::Utc::now().to_rfc3339();
        let success = verification_log.success;

        // Set binary validation timestamp and score if this was a successful binary validation
        let (binary_validation_time, binary_validation_score) =
            if success && node_result.binary_validation_successful {
                (Some(now.clone()), Some(node_result.verification_score))
            } else {
                (None, None)
            };

        if let Err(e) = sqlx::query(query)
            .bind(verification_log.id.to_string())
            .bind(&verification_log.node_id)
            .bind(&verification_log.validator_hotkey)
            .bind(&verification_log.verification_type)
            .bind(verification_log.timestamp.to_rfc3339())
            .bind(verification_log.score)
            .bind(if success { 1 } else { 0 })
            .bind(
                serde_json::to_string(&verification_log.details)
                    .unwrap_or_else(|_| "{}".to_string()),
            )
            .bind(verification_log.duration_ms)
            .bind(&verification_log.error_message)
            .bind(verification_log.created_at.to_rfc3339())
            .bind(verification_log.updated_at.to_rfc3339())
            .bind(binary_validation_time)
            .bind(binary_validation_score)
            .execute(self.persistence.pool())
            .await
        {
            error!("Failed to store verification log: {}", e);
            return Err(anyhow::anyhow!("Database storage failed: {}", e));
        }

        let miner_id = format!("miner_{miner_uid}");
        let status = match (success, &node_result.validation_type) {
            (false, _) => "offline".to_string(),
            (true, ValidationType::Full) => "online".to_string(),
            (true, ValidationType::Lightweight) => {
                match self
                    .persistence
                    .has_active_rental(&node_result.node_id.to_string(), &miner_id)
                    .await
                {
                    Ok(true) => "online".to_string(),
                    _ => sqlx::query_scalar::<_, String>(
                        "SELECT status FROM miner_nodes WHERE miner_id = ? AND node_id = ?",
                    )
                    .bind(&miner_id)
                    .bind(&verification_log.node_id)
                    .fetch_optional(self.persistence.pool())
                    .await
                    .ok()
                    .flatten()
                    .unwrap_or_else(|| "verified".to_string()),
                }
            }
        };

        info!(
            security = true,
            miner_uid = miner_uid,
            node_id = %node_result.node_id,
            validation_type = %node_result.validation_type,
            new_status = %status,
            "Status update based on validation type"
        );

        // Use transaction to ensure atomic updates
        let mut tx = self.persistence.pool().begin().await?;

        // Update node status
        if let Err(e) = sqlx::query(
            "UPDATE miner_nodes
             SET status = ?, last_health_check = ?, updated_at = ?
             WHERE node_id = ?",
        )
        .bind(&status)
        .bind(&now)
        .bind(&now)
        .bind(&verification_log.node_id)
        .execute(&mut *tx)
        .await
        {
            warn!("Failed to update node health status: {}", e);
            tx.rollback().await?;
            return Err(anyhow::anyhow!("Failed to update node status: {}", e));
        }

        // escape plan, if verification failed, clean up GPU assignments
        if !(success
            || node_result.validation_type == ValidationType::Lightweight
                && node_result.ssh_connection_successful)
        {
            self.cleanup_gpu_assignments(&verification_log.node_id, &miner_id, Some(&mut tx))
                .await?;
            tx.commit().await?;
            return Ok(());
        }

        tx.commit().await?;

        let gpu_infos = node_result
            .node_result
            .as_ref()
            .map(|er| er.gpu_infos.clone())
            .unwrap_or_default();

        match node_result.validation_type {
            ValidationType::Full => {
                info!(
                    security = true,
                    miner_uid = miner_uid,
                    node_id = %node_result.node_id,
                    validation_type = "full",
                    gpu_count = gpu_infos.len(),
                    action = "processing_full_validation",
                    "Processing full validation for miner {}, node {}",
                    miner_uid, node_result.node_id
                );

                self.ensure_miner_node_relationship(
                    miner_uid,
                    &node_result.node_id.to_string(),
                    &node_result.grpc_endpoint,
                    miner_info,
                )
                .await?;

                self.store_gpu_uuid_assignments(
                    miner_uid,
                    &node_result.node_id.to_string(),
                    &gpu_infos,
                )
                .await?;

                // Create/update GPU profile for this miner after successful verification
                let gpu_repo = GpuProfileRepository::new(self.persistence.pool().clone());

                // Get actual GPU counts from the just-stored assignments
                let miner_id = format!("miner_{}", miner_uid);
                let gpu_counts = self
                    .persistence
                    .get_miner_gpu_uuid_assignments(&miner_id)
                    .await?;
                let mut gpu_map: HashMap<String, u32> = HashMap::new();
                for (_, count, gpu_name, _) in gpu_counts {
                    let category = GpuCategory::from_str(&gpu_name).unwrap();
                    let model = category.to_string();
                    *gpu_map.entry(model).or_insert(0) += count;
                }

                let existing_count = self
                    .persistence
                    .get_miner_verification_count(&miner_id, 3)
                    .await?;
                let total_verification_count = existing_count + 1;

                let profile = MinerGpuProfile {
                    miner_uid: MinerUid::new(miner_uid),
                    gpu_counts: gpu_map,
                    total_score: node_result.verification_score,
                    verification_count: total_verification_count,
                    last_updated: Utc::now(),
                    last_successful_validation: Some(Utc::now()),
                };

                if let Err(e) = gpu_repo.upsert_gpu_profile(&profile).await {
                    warn!(
                            "Failed to update GPU profile for miner {} after successful verification: {}",
                            miner_uid, e
                        );
                } else {
                    info!(
                        "Successfully updated GPU profile for miner {}: {} GPUs",
                        miner_uid,
                        profile.gpu_counts.values().sum::<u32>()
                    );
                }
            }
            ValidationType::Lightweight => {
                info!(
                    security = true,
                    miner_uid = miner_uid,
                    node_id = %node_result.node_id,
                    validation_type = "lightweight",
                    gpu_count = gpu_infos.len(),
                    action = "processing_lightweight_validation",
                    "Processing lightweight validation for miner {}, node {}",
                    miner_uid, node_result.node_id
                );

                self.update_gpu_assignment_timestamps(
                    miner_uid,
                    &node_result.node_id.to_string(),
                    &gpu_infos,
                )
                .await?;
            }
        }

        info!(
            miner_uid = miner_uid,
            node_id = %node_result.node_id,
            verification_score = node_result.verification_score,
            validation_type = %node_result.validation_type,
            "Node verification result successfully stored to database for miner {}, node {}: score={:.2}",
            miner_uid, node_result.node_id, node_result.verification_score
        );

        Ok(())
    }

    /// Ensure miner-node relationship exists
    async fn ensure_miner_node_relationship(
        &self,
        miner_uid: u16,
        node_id: &str,
        node_grpc_endpoint: &str,
        miner_info: &super::types::MinerInfo,
    ) -> Result<()> {
        info!(
            miner_uid = miner_uid,
            node_id = node_id,
            "Ensuring miner-node relationship for miner {} and node {} with real data",
            miner_uid,
            node_id
        );

        let miner_id = format!("miner_{miner_uid}");

        // First ensure the miner exists in miners table with real data
        self.ensure_miner_exists_with_info(miner_info).await?;

        // Check if relationship already exists
        let query = "SELECT COUNT(*) as count FROM miner_nodes WHERE miner_id = ? AND node_id = ?";
        let row = sqlx::query(query)
            .bind(&miner_id)
            .bind(node_id)
            .fetch_one(self.persistence.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check miner-node relationship: {}", e))?;

        let count: i64 = row.get("count");

        if count == 0 {
            // Check if this grpc_address is already used by a different miner
            let existing_miner: Option<String> = sqlx::query_scalar(
                "SELECT miner_id FROM miner_nodes WHERE grpc_address = ? AND miner_id != ? LIMIT 1",
            )
            .bind(node_grpc_endpoint)
            .bind(&miner_id)
            .fetch_optional(self.persistence.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check grpc_address uniqueness: {}", e))?;

            if let Some(other_miner) = existing_miner {
                return Err(anyhow::anyhow!(
                    "Cannot create node relationship: grpc_address {} is already registered to {}",
                    node_grpc_endpoint,
                    other_miner
                ));
            }

            // Check if this is an node ID change for the same miner
            let old_node_id: Option<String> = sqlx::query_scalar(
                "SELECT node_id FROM miner_nodes WHERE grpc_address = ? AND miner_id = ?",
            )
            .bind(node_grpc_endpoint)
            .bind(&miner_id)
            .fetch_optional(self.persistence.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check for existing node: {}", e))?;

            if let Some(old_id) = old_node_id {
                info!(
                    "Miner {} is changing node ID from {} to {} for endpoint {}",
                    miner_id, old_id, node_id, node_grpc_endpoint
                );

                let mut tx = self.persistence.pool().begin().await?;

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

            // Insert new relationship with required fields
            let insert_query = r#"
                INSERT OR IGNORE INTO miner_nodes (
                    id, miner_id, node_id, grpc_address, gpu_count,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            "#;

            let relationship_id = format!("{miner_id}_{node_id}");

            sqlx::query(insert_query)
                .bind(&relationship_id)
                .bind(&miner_id)
                .bind(node_id)
                .bind(node_grpc_endpoint)
                // -- these will be updated from verification details
                .bind(0) // gpu_count
                //---------
                .bind("online") // status - online until verification completes
                .execute(self.persistence.pool())
                .await
                .map_err(|e| anyhow::anyhow!("Failed to insert miner-node relationship: {}", e))?;

            info!(
                miner_uid = miner_uid,
                node_id = node_id,
                "Created miner-node relationship: {} -> {} with endpoint {}",
                miner_id,
                node_id,
                node_grpc_endpoint
            );
        } else {
            debug!(
                miner_uid = miner_uid,
                node_id = node_id,
                "Miner-node relationship already exists: {} -> {}",
                miner_id,
                node_id
            );

            // Even if relationship exists, check for duplicates with same grpc_address
            let duplicate_check_query: &'static str =
                "SELECT id, node_id FROM miner_nodes WHERE grpc_address = ? AND id != ?";
            let relationship_id = format!("{miner_id}_{node_id}");

            let duplicates = sqlx::query(duplicate_check_query)
                .bind(node_grpc_endpoint)
                .bind(&relationship_id)
                .fetch_all(self.persistence.pool())
                .await
                .map_err(|e| anyhow::anyhow!("Failed to check for duplicate nodes: {}", e))?;

            if !duplicates.is_empty() {
                let duplicate_count = duplicates.len();
                warn!(
                    "Found {} duplicate nodes with same grpc_address {} for miner {}",
                    duplicate_count, node_grpc_endpoint, miner_id
                );

                // Delete the duplicates to clean up fraudulent registrations
                for duplicate in duplicates {
                    let dup_id: String = duplicate.get("id");
                    let dup_node_id: String = duplicate.get("node_id");

                    warn!(
                        "Marking duplicate node {} (id: {}) as offline with same grpc_address as {} for miner {}",
                        dup_node_id, dup_id, node_id, miner_id
                    );

                    sqlx::query("UPDATE miner_nodes SET status = 'offline', last_health_check = datetime('now'), updated_at = datetime('now') WHERE id = ?")
                        .bind(&dup_id)
                        .execute(self.persistence.pool())
                        .await
                        .map_err(|e| {
                            anyhow::anyhow!("Failed to update duplicate node status: {}", e)
                        })?;

                    // Also clean up associated GPU assignments for the duplicate
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
                    "Cleaned up {} duplicate nodes for miner {} with grpc_address {}",
                    duplicate_count, miner_id, node_grpc_endpoint
                );
            }
        }

        Ok(())
    }

    /// Store GPU UUID assignments for an node
    async fn store_gpu_uuid_assignments(
        &self,
        miner_uid: u16,
        node_id: &str,
        gpu_infos: &[GpuInfo],
    ) -> Result<()> {
        let miner_id = format!("miner_{miner_uid}");
        let now = chrono::Utc::now().to_rfc3339();

        // Collect all valid GPU UUIDs being reported
        let reported_gpu_uuids: Vec<String> = gpu_infos
            .iter()
            .filter(|g| !g.gpu_uuid.is_empty() && g.gpu_uuid != "Unknown UUID")
            .map(|g| g.gpu_uuid.clone())
            .collect();

        // Clean up GPU assignments based on what's reported
        if !reported_gpu_uuids.is_empty() {
            // Some GPUs reported - clean up any that are no longer reported
            let placeholders = reported_gpu_uuids
                .iter()
                .map(|_| "?")
                .collect::<Vec<_>>()
                .join(", ");
            let query = format!(
                "DELETE FROM gpu_uuid_assignments
                 WHERE miner_id = ? AND node_id = ?
                 AND gpu_uuid NOT IN ({placeholders})"
            );

            let mut q = sqlx::query(&query).bind(&miner_id).bind(node_id);

            for uuid in &reported_gpu_uuids {
                q = q.bind(uuid);
            }

            let deleted = q.execute(self.persistence.pool()).await?;

            if deleted.rows_affected() > 0 {
                info!(
                    "Cleaned up {} stale GPU assignments for {}/{}",
                    deleted.rows_affected(),
                    miner_id,
                    node_id
                );
            }
        } else {
            // No GPUs reported - clean up all assignments for this node
            let deleted_rows = self
                .cleanup_gpu_assignments(node_id, &miner_id, None)
                .await?;

            if deleted_rows > 0 {
                info!(
                    "Cleaned up {} GPU assignments for {}/{} (no GPUs reported)",
                    deleted_rows, miner_id, node_id
                );
            }
        }

        for gpu_info in gpu_infos {
            // Skip invalid UUIDs
            if gpu_info.gpu_uuid.is_empty() || gpu_info.gpu_uuid == "Unknown UUID" {
                continue;
            }

            // Check if this GPU UUID already exists
            let existing = sqlx::query(
                "SELECT miner_id, node_id FROM gpu_uuid_assignments WHERE gpu_uuid = ?",
            )
            .bind(&gpu_info.gpu_uuid)
            .fetch_optional(self.persistence.pool())
            .await?;

            if let Some(row) = existing {
                let existing_miner_id: String = row.get("miner_id");
                let existing_node_id: String = row.get("node_id");

                if existing_miner_id != miner_id || existing_node_id != node_id {
                    // Check if the existing node is still active
                    let node_status_query =
                        "SELECT status FROM miner_nodes WHERE node_id = ? AND miner_id = ?";
                    let status_row = sqlx::query(node_status_query)
                        .bind(&existing_node_id)
                        .bind(&existing_miner_id)
                        .fetch_optional(self.persistence.pool())
                        .await?;

                    let can_reassign = if let Some(row) = status_row {
                        let status: String = row.get("status");
                        // Allow reassignment if node is offline, failed, or stale
                        status == "offline" || status == "failed" || status == "stale"
                    } else {
                        // Node doesn't exist in miner_nodes table - allow reassignment
                        true
                    };

                    if can_reassign {
                        // GPU reassignment allowed - previous node is inactive
                        info!(
                            security = true,
                            gpu_uuid = %gpu_info.gpu_uuid,
                            previous_miner_id = %existing_miner_id,
                            previous_node_id = %existing_node_id,
                            new_miner_id = %miner_id,
                            new_node_id = %node_id,
                            gpu_memory_gb = %gpu_info.gpu_memory_gb,
                            action = "gpu_assignment_reassigned",
                            reassignment_reason = "previous_node_inactive",
                            "GPU {} reassigned from {}/{} to {}/{} (previous node inactive)",
                            gpu_info.gpu_uuid,
                            existing_miner_id,
                            existing_node_id,
                            miner_id,
                            node_id
                        );

                        sqlx::query(
                            "UPDATE gpu_uuid_assignments
                             SET miner_id = ?, node_id = ?, gpu_index = ?, gpu_name = ?,
                                 gpu_memory_gb = ?, last_verified = ?, updated_at = ?
                             WHERE gpu_uuid = ?",
                        )
                        .bind(&miner_id)
                        .bind(node_id)
                        .bind(gpu_info.index as i32)
                        .bind(&gpu_info.gpu_name)
                        .bind(gpu_info.gpu_memory_gb)
                        .bind(&now)
                        .bind(&now)
                        .bind(&gpu_info.gpu_uuid)
                        .execute(self.persistence.pool())
                        .await?;
                    } else {
                        // Node is still active - reject the reassignment
                        warn!(
                            security = true,
                            gpu_uuid = %gpu_info.gpu_uuid,
                            existing_miner_id = %existing_miner_id,
                            existing_node_id = %existing_node_id,
                            attempting_miner_id = %miner_id,
                            attempting_node_id = %node_id,
                            action = "gpu_assignment_rejected",
                            rejection_reason = "already_owned_by_active_node",
                            "GPU UUID {} still owned by active node {}/{}, rejecting claim from {}/{}",
                            gpu_info.gpu_uuid,
                            existing_miner_id,
                            existing_node_id,
                            miner_id,
                            node_id
                        );
                        // Skip this GPU - don't store it for the new claimant
                        continue;
                    }
                } else {
                    // Same owner - just update last_verified
                    sqlx::query(
                        "UPDATE gpu_uuid_assignments
                         SET gpu_memory_gb = ?, last_verified = ?, updated_at = ?
                         WHERE gpu_uuid = ?",
                    )
                    .bind(gpu_info.gpu_memory_gb)
                    .bind(&now)
                    .bind(&now)
                    .bind(&gpu_info.gpu_uuid)
                    .execute(self.persistence.pool())
                    .await?;
                }
            } else {
                // New GPU UUID - insert
                sqlx::query(
                    "INSERT INTO gpu_uuid_assignments
                     (gpu_uuid, gpu_index, node_id, miner_id, gpu_name, gpu_memory_gb, last_verified, created_at, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                )
                .bind(&gpu_info.gpu_uuid)
                .bind(gpu_info.index as i32)
                .bind(node_id)
                .bind(&miner_id)
                .bind(&gpu_info.gpu_name)
                .bind(gpu_info.gpu_memory_gb)
                .bind(&now)
                .bind(&now)
                .bind(&now)
                .execute(self.persistence.pool())
                .await?;

                info!(
                    security = true,
                    gpu_uuid = %gpu_info.gpu_uuid,
                    gpu_index = gpu_info.index,
                    node_id = %node_id,
                    miner_id = %miner_id,
                    gpu_name = %gpu_info.gpu_name,
                    gpu_memory_gb = %gpu_info.gpu_memory_gb,
                    action = "gpu_assignment_created",
                    "Registered new GPU {} (index {}) for {}/{}",
                    gpu_info.gpu_uuid, gpu_info.index, miner_id, node_id
                );
            }
        }

        // Update gpu_count in miner_nodes based on actual GPU assignments
        let gpu_count = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM gpu_uuid_assignments WHERE miner_id = ? AND node_id = ?",
        )
        .bind(&miner_id)
        .bind(node_id)
        .fetch_one(self.persistence.pool())
        .await?;

        // Status hierarchy: "online" > "verified" > "offline"
        let current_status = sqlx::query_scalar::<_, String>(
            "SELECT status FROM miner_nodes WHERE miner_id = ? AND node_id = ?",
        )
        .bind(&miner_id)
        .bind(node_id)
        .fetch_one(self.persistence.pool())
        .await?;

        let new_status = match (current_status.as_str(), gpu_count > 0) {
            ("online", true) => "online",   // Keep online status if GPUs present
            ("verified", true) => "online", // Promote verified back to online if GPUs present
            ("online", false) => "offline", // Downgrade to offline if no GPUs
            (_, true) => "verified",        // Set verified if GPUs present and not online
            (_, false) => "offline",        // Set offline if no GPUs
        };

        sqlx::query(
            "UPDATE miner_nodes SET gpu_count = ?, status = ?, updated_at = datetime('now')
             WHERE miner_id = ? AND node_id = ?",
        )
        .bind(gpu_count as i32)
        .bind(new_status)
        .bind(&miner_id)
        .bind(node_id)
        .execute(self.persistence.pool())
        .await?;

        if gpu_count > 0 {
            info!(
                security = true,
                node_id = %node_id,
                miner_id = %miner_id,
                gpu_count = gpu_count,
                new_status = %new_status,
                action = "node_gpu_verification_success",
                "Node {}/{} verified with {} GPUs, status: {}",
                miner_id, node_id, gpu_count, new_status
            );
        } else {
            warn!(
                security = true,
                node_id = %node_id,
                miner_id = %miner_id,
                gpu_count = 0,
                new_status = %new_status,
                action = "node_gpu_verification_failure",
                "Node {}/{} has no GPUs, marking as {}",
                miner_id, node_id, new_status
            );
        }

        // Validate that the GPU count matches the expected count
        let expected_gpu_count = gpu_infos
            .iter()
            .filter(|g| !g.gpu_uuid.is_empty() && g.gpu_uuid != "Unknown UUID")
            .count() as i64;

        if gpu_count != expected_gpu_count {
            warn!(
                "GPU assignment mismatch for {}/{}: stored {} GPUs but expected {}",
                miner_id, node_id, gpu_count, expected_gpu_count
            );
        }

        // Fail verification if node claims GPUs but none were stored
        if expected_gpu_count > 0 && gpu_count == 0 {
            error!(
                "Failed to store GPU assignments for {}/{}: expected {} GPUs but stored 0",
                miner_id, node_id, expected_gpu_count
            );
            return Err(anyhow::anyhow!(
                "GPU assignment validation failed: no valid GPU UUIDs stored despite {} GPUs reported",
                expected_gpu_count
            ));
        }

        Ok(())
    }

    /// Update last_verified timestamp for existing GPU assignments
    async fn update_gpu_assignment_timestamps(
        &self,
        miner_uid: u16,
        node_id: &str,
        gpu_infos: &[GpuInfo],
    ) -> Result<()> {
        let miner_id = format!("miner_{miner_uid}");
        let now = chrono::Utc::now().to_rfc3339();

        let reported_gpu_uuids: Vec<String> = gpu_infos
            .iter()
            .filter(|g| !g.gpu_uuid.is_empty() && g.gpu_uuid != "Unknown UUID")
            .map(|g| g.gpu_uuid.clone())
            .collect();

        if reported_gpu_uuids.is_empty() {
            debug!(
                "No valid GPU UUIDs reported for {}/{} in lightweight validation",
                miner_id, node_id
            );
            return Ok(());
        }

        let placeholders = reported_gpu_uuids
            .iter()
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(", ");

        let query = format!(
            "UPDATE gpu_uuid_assignments
             SET last_verified = ?, updated_at = ?
             WHERE miner_id = ? AND node_id = ? AND gpu_uuid IN ({placeholders})"
        );

        let mut q = sqlx::query(&query)
            .bind(&now)
            .bind(&now)
            .bind(&miner_id)
            .bind(node_id);

        for uuid in &reported_gpu_uuids {
            q = q.bind(uuid);
        }

        let result = q.execute(self.persistence.pool()).await?;
        let updated_count = result.rows_affected();

        if updated_count > 0 {
            info!(
                security = true,
                miner_uid = miner_uid,
                node_id = %node_id,
                validation_type = "lightweight",
                updated_assignments = updated_count,
                action = "gpu_assignment_timestamp_updated",
                "Updated {} GPU assignment timestamps for {}/{} (lightweight validation)",
                updated_count, miner_id, node_id
            );
        } else {
            debug!(
                security = true,
                miner_uid = miner_uid,
                node_id = %node_id,
                validation_type = "lightweight",
                "No GPU assignments found to update for {}/{} with {} reported UUIDs",
                miner_id,
                node_id,
                reported_gpu_uuids.len()
            );
            if self
                .persistence
                .has_active_rental(node_id, &miner_id)
                .await
                .unwrap_or(false)
            {
                self.store_gpu_uuid_assignments(miner_uid, node_id, gpu_infos)
                    .await?;
            } else {
                debug!(
                    security = true,
                    miner_uid = miner_uid,
                    node_id = %node_id,
                    validation_type = "lightweight",
                    "Skipping GPU assignment creation in lightweight (no active rental)"
                );
            }
        }

        Ok(())
    }

    /// Ensure miner exists in miners table
    ///
    /// This function handles three scenarios:
    /// 1. if UID already exists with same hotkey -> Update data
    /// 2. if UID already exists with different hotkey -> Update to new hotkey (recycled UID)
    /// 3. if UID doesn't exist but hotkey does -> on re-registration, migrate the UID
    /// 4. if neither UID nor hotkey exist -> Create new miner
    async fn ensure_miner_exists_with_info(
        &self,
        miner_info: &super::types::MinerInfo,
    ) -> Result<()> {
        let new_miner_uid = format!("miner_{}", miner_info.uid.as_u16());
        let hotkey = miner_info.hotkey.to_string();

        // Step 1: handle recycled UIDs
        let existing_by_uid = self.check_miner_by_uid(&new_miner_uid).await?;

        if let Some((_, existing_hotkey)) = existing_by_uid {
            return self
                .handle_recycled_miner_uid(&new_miner_uid, &hotkey, &existing_hotkey, miner_info)
                .await;
        }

        // Step 2: handle UID changes when a hotkey moves to a new UID (re-registration)
        let existing_by_hotkey = self.check_miner_by_hotkey(&hotkey).await?;

        if let Some(old_miner_uid) = existing_by_hotkey {
            return self
                .handle_uid_change(&old_miner_uid, &new_miner_uid, &hotkey, miner_info)
                .await;
        }

        // Step 3: handle new miners when neither UID nor hotkey exist - create new miner
        self.create_new_miner(&new_miner_uid, &hotkey, miner_info)
            .await
    }

    /// Check if a miner with the given UID exists
    async fn check_miner_by_uid(&self, miner_uid: &str) -> Result<Option<(String, String)>> {
        let query = "SELECT id, hotkey FROM miners WHERE id = ?";
        let result = sqlx::query(query)
            .bind(miner_uid)
            .fetch_optional(self.persistence.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check miner by uid: {}", e))?;

        Ok(result.map(|row| {
            let id: String = row.get("id");
            let hotkey: String = row.get("hotkey");
            (id, hotkey)
        }))
    }

    /// Check if a miner with the given hotkey exists
    async fn check_miner_by_hotkey(&self, hotkey: &str) -> Result<Option<String>> {
        let query = "SELECT id FROM miners WHERE hotkey = ?";
        let result = sqlx::query(query)
            .bind(hotkey)
            .fetch_optional(self.persistence.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to check miner by hotkey: {}", e))?;

        Ok(result.map(|row| row.get("id")))
    }

    /// Handle case where miner UID already exists
    async fn handle_recycled_miner_uid(
        &self,
        miner_uid: &str,
        new_hotkey: &str,
        existing_hotkey: &str,
        miner_info: &super::types::MinerInfo,
    ) -> Result<()> {
        if existing_hotkey != new_hotkey {
            // Case: Recycled UID - same UID but different hotkey
            info!(
                "Miner {} exists with old hotkey {}, updating to new hotkey {}",
                miner_uid, existing_hotkey, new_hotkey
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
                .execute(self.persistence.pool())
                .await
                .map_err(|e| anyhow::anyhow!("Failed to update miner with new hotkey: {}", e))?;

            debug!("Updated miner {} with new hotkey and data", miner_uid);
        } else {
            // Case: Same miner, same hotkey - just update the data
            self.update_miner_data(miner_uid, miner_info).await?;
        }

        Ok(())
    }

    /// Handle case where hotkey exists but with different ID (UID change)
    async fn handle_uid_change(
        &self,
        old_miner_id: &str,
        new_miner_id: &str,
        hotkey: &str,
        miner_info: &super::types::MinerInfo,
    ) -> Result<()> {
        info!(
            "Detected UID change for hotkey {}: {} -> {}",
            hotkey, old_miner_id, new_miner_id
        );

        // Migrate the miner UID
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

    /// Update existing miner data
    async fn update_miner_data(
        &self,
        miner_id: &str,
        miner_info: &super::types::MinerInfo,
    ) -> Result<()> {
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
            .execute(self.persistence.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to update miner: {}", e))?;

        debug!("Updated miner record: {} with latest data", miner_id);
        Ok(())
    }

    /// Create a new miner record
    async fn create_new_miner(
        &self,
        miner_uid: &str,
        hotkey: &str,
        miner_info: &super::types::MinerInfo,
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
            .bind(100.0) // uptime_percentage
            .bind("{}") // node_info
            .execute(self.persistence.pool())
            .await
            .map_err(|e| anyhow::anyhow!("Failed to insert miner: {}", e))?;

        info!(
            "Created miner record: {} with hotkey {} and endpoint {}",
            miner_uid, hotkey, miner_info.endpoint
        );

        Ok(())
    }

    /// Migrate miner UID when it changes in the network
    async fn migrate_miner_uid(
        &self,
        old_miner_uid: &str,
        new_miner_uid: &str,
        miner_info: &super::types::MinerInfo,
    ) -> Result<()> {
        info!(
            "Starting UID migration: {} -> {} for hotkey {}",
            old_miner_uid, new_miner_uid, miner_info.hotkey
        );

        // Use a transaction to ensure atomicity
        let mut tx = self
            .persistence
            .pool()
            .begin()
            .await
            .map_err(|e| anyhow::anyhow!("Failed to begin transaction: {}", e))?;

        // 1. First, get the old miner data
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

        // 2. Check if any miner with this hotkey exists (including the target)
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

        // Find if any of them is NOT the old miner
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
                // The new miner record already exists, just need to delete old
                debug!("New miner record already exists with correct ID");
                false
            } else {
                // Another miner exists with this hotkey but different ID
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

        // Extract old miner data we'll need
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

        // 3. Get all related data before deletion
        debug!("Fetching related node data");
        let get_nodes = "SELECT * FROM miner_nodes WHERE miner_id = ?";
        let nodes = sqlx::query(get_nodes)
            .bind(old_miner_uid)
            .fetch_all(&mut *tx)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to fetch nodes: {}", e))?;

        debug!("Found {} nodes to migrate", nodes.len());

        // 4. Delete old miner record (this will CASCADE delete miner_nodes and verification_requests)
        debug!("Deleting old miner record: {}", old_miner_uid);
        let delete_old_miner = "DELETE FROM miners WHERE id = ?";
        sqlx::query(delete_old_miner)
            .bind(old_miner_uid)
            .execute(&mut *tx)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to delete old miner record: {}", e))?;

        debug!("Deleted old miner record and related data");

        // 5. Create new miner record if needed
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

        // 6. Re-create node relationships
        let mut node_count = 0;
        for node_row in nodes {
            let node_id: String = node_row.get("node_id");
            let grpc_address: String = node_row.get("grpc_address");
            let gpu_count: i32 = node_row.get("gpu_count");
            let status: String = node_row
                .try_get("status")
                .unwrap_or_else(|_| "unknown".to_string());
            // Check if this grpc_address is already in use by another miner
            let existing_check = sqlx::query(
                "SELECT COUNT(*) as count FROM miner_nodes WHERE grpc_address = ? AND miner_id != ?"
            )
            .bind(&grpc_address)
            .bind(new_miner_uid)
            .fetch_one(&mut *tx)
            .await?;

            let existing_count: i64 = existing_check.get("count");
            if existing_count > 0 {
                warn!(
                    "Skipping node {} during UID migration: grpc_address {} already in use by another miner",
                    node_id, grpc_address
                );
                continue;
            }

            let new_id = format!("{new_miner_uid}_{node_id}");

            let insert_node = r#"
                INSERT INTO miner_nodes (
                    id, miner_id, node_id, grpc_address, gpu_count,
                    status, last_health_check,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, datetime('now'), datetime('now'))
            "#;

            sqlx::query(insert_node)
                .bind(&new_id)
                .bind(new_miner_uid)
                .bind(&node_id)
                .bind(&grpc_address)
                .bind(gpu_count)
                .bind(&status)
                .execute(&mut *tx)
                .await
                .map_err(|e| anyhow::anyhow!("Failed to recreate node relationship: {}", e))?;

            node_count += 1;
        }

        debug!("Recreated {} node relationships", node_count);

        // 7. Migrate GPU UUID assignments
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

        // Commit the transaction
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

    /// Sync miners from metagraph to database
    pub async fn sync_miners_from_metagraph(&self, miners: &[MinerInfo]) -> Result<()> {
        info!("Syncing {} miners from metagraph to database", miners.len());

        for miner in miners {
            // Discovery already filters out miners without valid axon endpoints
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

    /// Create authenticated miner client
    fn create_authenticated_client(&self) -> Result<MinerClient> {
        Ok(
            if let Some(ref bittensor_service) = self.bittensor_service {
                let signer = Box::new(super::miner_client::BittensorServiceSigner::new(
                    bittensor_service.clone(),
                ));
                MinerClient::with_signer(
                    self.miner_client_config.clone(),
                    self.validator_hotkey.clone(),
                    signer,
                )
            } else {
                MinerClient::new(
                    self.miner_client_config.clone(),
                    self.validator_hotkey.clone(),
                )
            },
        )
    }

    /// Get whether dynamic discovery is enabled
    pub fn use_dynamic_discovery(&self) -> bool {
        self.use_dynamic_discovery
    }

    /// Get SSH key manager reference
    pub fn ssh_key_manager(&self) -> &Option<Arc<ValidatorSshKeyManager>> {
        &self.ssh_key_manager
    }

    /// Get bittensor service reference
    pub fn bittensor_service(&self) -> &Option<Arc<bittensor::Service>> {
        &self.bittensor_service
    }

    /// Get SSH key path reference
    pub fn ssh_key_path(&self) -> &Option<PathBuf> {
        &self.ssh_key_path
    }

    /// Create VerificationEngine with SSH automation components (new preferred method)
    #[allow(clippy::too_many_arguments)]
    pub fn with_ssh_automation(
        config: VerificationConfig,
        miner_client_config: MinerClientConfig,
        validator_hotkey: Hotkey,
        ssh_client: Arc<ValidatorSshClient>,
        persistence: Arc<SimplePersistence>,
        use_dynamic_discovery: bool,
        ssh_key_manager: Option<Arc<ValidatorSshKeyManager>>,
        bittensor_service: Option<Arc<bittensor::Service>>,
        metrics: Option<Arc<ValidatorMetrics>>,
    ) -> Result<Self> {
        // Validate required components for dynamic discovery
        if use_dynamic_discovery && ssh_key_manager.is_none() {
            return Err(anyhow::anyhow!(
                "SSH key manager is required when dynamic discovery is enabled"
            ));
        }

        Ok(Self {
            config: config.clone(),
            miner_client_config,
            validator_hotkey,
            persistence: persistence.clone(),
            use_dynamic_discovery,
            ssh_key_path: None, // Not used when SSH key manager is available
            bittensor_service,
            ssh_key_manager: ssh_key_manager.clone(),
            validation_strategy_selector: Arc::new(ValidationStrategySelector::new(
                config.clone(),
                persistence.clone(),
            )),
            validation_node: Arc::new(tokio::sync::RwLock::new(ValidationNode::new(
                config.clone(),
                ssh_client.clone(),
                metrics,
                persistence.clone(),
            ))),
            worker_queue: None, // Will be set separately if needed
        })
    }

    /// Initialize validation server mode
    pub async fn initialize_validation_server(&mut self) -> Result<()> {
        info!("Initializing validation server mode for VerificationEngine");
        let mut node = self.validation_node.write().await;
        node.initialize_server_mode(&self.config.binary_validation)
            .await?;
        info!("Validation server mode initialized successfully");
        Ok(())
    }

    /// Shutdown validation server cleanly
    pub async fn shutdown_validation_server(&self) -> Result<()> {
        let node = self.validation_node.read().await;
        node.shutdown_server_mode().await
    }

    /// Check if SSH automation is properly configured
    pub fn is_ssh_automation_ready(&self) -> bool {
        if self.use_dynamic_discovery() {
            self.ssh_key_manager().is_some()
        } else {
            // Static configuration requires either key manager or fallback key path
            self.ssh_key_manager().is_some() || self.ssh_key_path().is_some()
        }
    }

    /// Get SSH automation status
    pub fn get_ssh_automation_status(&self) -> SshAutomationStatus {
        SshAutomationStatus {
            dynamic_discovery_enabled: self.use_dynamic_discovery(),
            ssh_key_manager_available: self.ssh_key_manager().is_some(),
            bittensor_service_available: self.bittensor_service().is_some(),
            fallback_key_path: self.ssh_key_path().clone(),
        }
    }

    /// Get configuration summary for debugging
    pub fn get_config_summary(&self) -> String {
        format!(
            "VerificationEngine[dynamic_discovery={}, ssh_key_manager={}, bittensor_service={}, worker_queue={}]",
            self.use_dynamic_discovery(),
            self.ssh_key_manager().is_some(),
            self.bittensor_service().is_some(),
            self.worker_queue.is_some()
        )
    }

    /// Set worker queue for decoupled execution
    pub fn set_worker_queue(&mut self, queue: Arc<ValidationWorkerQueue>) {
        self.worker_queue = Some(queue);
    }

    /// Check if worker queue is enabled
    pub fn has_worker_queue(&self) -> bool {
        self.worker_queue.is_some()
    }

    /// Initialize and start worker queue
    pub async fn init_worker_queue(&mut self) -> Result<()> {
        let config = WorkerQueueConfig::default();
        let queue = Arc::new(ValidationWorkerQueue::new(config, Arc::new(self.clone())));

        queue.start().await?;
        self.worker_queue = Some(queue);

        info!("Worker queue initialized and started");
        Ok(())
    }

    /// Clean up nodes that have consecutive failed validations
    /// This is called periodically (every 15 minutes) to remove nodes that:
    /// 1. Are offline and still have GPU assignments (immediate cleanup)
    /// 2. Have had 2+ consecutive failed validations with no successes (delete)
    /// 3. Have been offline for 30+ minutes (stale cleanup)
    pub async fn cleanup_failed_nodes_after_failures(
        &self,
        consecutive_failures_threshold: i32,
    ) -> Result<()> {
        info!(
            "Running node cleanup - checking for {} consecutive failures",
            consecutive_failures_threshold
        );

        // Step 1: Clean up any GPU assignments for offline nodes (immediate fix)
        let offline_with_gpus_query = r#"
            SELECT DISTINCT me.node_id, me.miner_id, COUNT(ga.gpu_uuid) as gpu_count
            FROM miner_nodes me
            INNER JOIN gpu_uuid_assignments ga ON me.node_id = ga.node_id AND me.miner_id = ga.miner_id
            WHERE me.status = 'offline'
            GROUP BY me.node_id, me.miner_id
        "#;

        let offline_with_gpus = sqlx::query(offline_with_gpus_query)
            .fetch_all(self.persistence.pool())
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

        // Step 1b: Clean up nodes with mismatched GPU counts
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
            .fetch_all(self.persistence.pool())
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

            // Reset GPU count to 0 to reflect reality
            sqlx::query(
                "UPDATE miner_nodes SET gpu_count = 0, updated_at = datetime('now')
                 WHERE node_id = ? AND miner_id = ?",
            )
            .bind(&node_id)
            .bind(&miner_id)
            .execute(self.persistence.pool())
            .await?;

            // Mark offline if they claim GPUs but have none
            if status == "online" || status == "verified" {
                sqlx::query(
                    "UPDATE miner_nodes SET status = 'offline', updated_at = datetime('now')
                     WHERE node_id = ? AND miner_id = ?",
                )
                .bind(&node_id)
                .bind(&miner_id)
                .execute(self.persistence.pool())
                .await?;

                info!(
                    "Marked node {} as offline (claimed {} GPUs but has 0 assignments)",
                    node_id, gpu_count
                );
            }
        }

        // Step 1c: Clean up stale GPU assignments (GPUs that haven't been verified recently)
        // Increased threshold from 1 hour to 6 hours to reduce aggressive cleanup
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
            .execute(self.persistence.pool())
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

        // Step 1d: Clean up GPU assignments from nodes offline
        // Increased minimum cleanup time from 30 minutes to 2 hours
        let cleanup_minutes = self
            .config
            .gpu_assignment_cleanup_ttl
            .map(|d| d.as_secs() / 60)
            .unwrap_or(120)
            .max(120); // Ensure minimum 2 hours to reduce aggressive cleanup

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
            .fetch_all(self.persistence.pool())
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

        // Step 2: Find and delete nodes with consecutive failures
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
            .fetch_all(self.persistence.pool())
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

            // Use transaction to ensure atomic deletion
            let mut tx = self.persistence.pool().begin().await?;

            // Clean up any remaining GPU assignments
            self.cleanup_gpu_assignments(&node_id, &miner_id, Some(&mut tx))
                .await?;

            // Delete the node record
            sqlx::query("DELETE FROM miner_nodes WHERE node_id = ? AND miner_id = ?")
                .bind(&node_id)
                .bind(&miner_id)
                .execute(&mut *tx)
                .await?;

            tx.commit().await?;
            deleted += 1;

            // Clean up any active SSH sessions
            self.cleanup_active_session(&node_id).await;
        }

        // Step 3: Delete stale offline nodes
        let cleanup_minutes = self
            .config
            .gpu_assignment_cleanup_ttl
            .map(|d| d.as_secs() / 60)
            .unwrap_or(120)
            .max(120);

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
            .execute(self.persistence.pool())
            .await?;

        let stale_deleted = stale_result.rows_affected();

        // Step 4: Update GPU profiles for all miners with wrong gpu count profile
        let affected_miners_query = r#"
            SELECT DISTINCT miner_uid
            FROM miner_gpu_profiles
            WHERE miner_uid IN (
                -- Miners with offline nodes
                SELECT DISTINCT CAST(SUBSTR(miner_id, 7) AS INTEGER)
                FROM miner_nodes
                WHERE status = 'offline'

                UNION

                -- Miners with non-empty GPU profiles but no active nodes
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
            .fetch_all(self.persistence.pool())
            .await?;

        for row in affected_miners {
            let miner_uid: i64 = row.try_get("miner_uid")?;
            let miner_id = format!("miner_{}", miner_uid);

            let gpu_counts = self
                .persistence
                .get_miner_gpu_uuid_assignments(&miner_id)
                .await?;

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
                .execute(self.persistence.pool())
                .await?;

            if result.rows_affected() > 0 {
                info!(
                    "Updated GPU profile for miner {} after cleanup: {}",
                    miner_uid, gpu_json
                );
            }
        }

        // Log summary
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

    /// Enhanced verify node with SSH automation and binary validation
    pub async fn verify_node(
        &self,
        miner_endpoint: &str,
        node_info: &NodeInfoDetailed,
        miner_uid: u16,
        miner_hotkey: &str,
        intended_strategy: ValidationType,
    ) -> Result<NodeVerificationResult> {
        info!(
            miner_uid = miner_uid,
            node_id = %node_info.id,
            miner_endpoint = %miner_endpoint,
            "[EVAL_FLOW] Starting node verification"
        );

        // Step 1: Determine validation strategy
        let strategy = match self
            .validation_strategy_selector
            .determine_validation_strategy(&node_info.id.to_string(), miner_uid)
            .await
        {
            Ok(s) => s,
            Err(e) => {
                error!(
                    miner_uid = miner_uid,
                    node_id = %node_info.id,
                    error = %e,
                    "[EVAL_FLOW] Failed to determine validation strategy, defaulting to full"
                );
                super::validation_strategy::ValidationStrategy::Full
            }
        };

        // Strategy filtering: skip if strategy doesn't match pipeline
        let strategy_matches = matches!(
            (&strategy, &intended_strategy),
            (ValidationStrategy::Full, ValidationType::Full)
                | (
                    ValidationStrategy::Lightweight { .. },
                    ValidationType::Lightweight
                )
        );

        if !strategy_matches {
            debug!(
                node_id = %node_info.id,
                determined_strategy = ?strategy,
                intended = ?intended_strategy,
                "[EVAL_FLOW] Strategy mismatch - node needs different validation type"
            );

            return Err(anyhow::anyhow!(
                "Strategy mismatch: node needs different validation type"
            ));
        }

        // Step 2: Establish miner connection first
        let client = self.create_authenticated_client()?;
        let _connection = client
            .connect_and_authenticate(miner_endpoint, miner_hotkey)
            .await?;

        // Step 3: Session management is now handled at connection level
        // Direct SSH connections are established as needed

        // Get SSH connection details for direct node connection
        let ssh_details = if let Some(ref key_manager) = self.ssh_key_manager {
            // Get node's SSH credentials from grpc_endpoint
            // grpc_endpoint format is expected to be "user@host:port" or similar
            let endpoint_parts: Vec<&str> = node_info.grpc_endpoint.split('@').collect();
            let (username, host_port) = if endpoint_parts.len() == 2 {
                (endpoint_parts[0], endpoint_parts[1])
            } else {
                ("root", node_info.grpc_endpoint.as_str())
            };

            let host_port_parts: Vec<&str> = host_port.split(':').collect();
            let (host, port) = if host_port_parts.len() == 2 {
                (
                    host_port_parts[0],
                    host_port_parts[1].parse::<u16>().unwrap_or(22),
                )
            } else {
                (host_port, 22)
            };

            let (_, private_key_path) = key_manager
                .get_persistent_key()
                .ok_or_else(|| anyhow::anyhow!("No persistent validator SSH key available"))?;

            basilica_common::ssh::SshConnectionDetails {
                host: host.to_string(),
                port,
                username: username.to_string(),
                private_key_path: private_key_path.clone(),
                timeout: std::time::Duration::from_secs(30),
            }
        } else {
            // Session cleanup handled at connection level
            return Err(anyhow::anyhow!("SSH key manager not available"));
        };

        // Step 4: Execute validation based on strategy
        let result = match strategy {
            ValidationStrategy::Lightweight {
                previous_score,
                node_result,
                gpu_count,
                binary_validation_successful,
            } => {
                self.validation_node
                    .read()
                    .await
                    .execute_lightweight_validation(
                        miner_uid,
                        node_info,
                        &ssh_details,
                        &(), // session_info is not used in lightweight validation
                        previous_score,
                        node_result,
                        gpu_count,
                        binary_validation_successful,
                        &self.validator_hotkey,
                        &self.config,
                    )
                    .await
            }
            ValidationStrategy::Full => {
                let binary_config = &self.config.binary_validation;
                self.validation_node
                    .read()
                    .await
                    .execute_full_validation(
                        node_info,
                        &ssh_details,
                        &(), // session_info is not used in full validation
                        binary_config,
                        &self.validator_hotkey,
                        miner_uid,
                    )
                    .await
            }
        };

        // Step 5: No explicit cleanup needed for direct node connections
        // The SSH session manager handles connection tracking

        // Step 6: Session cleanup handled at connection level

        result
    }

    /// Convert database node data to NodeInfoDetailed
    fn convert_db_data_to_node_info(
        &self,
        db_data: Vec<(String, String, i32, String)>,
        _miner_uid: u16,
    ) -> Result<Vec<NodeInfoDetailed>> {
        let mut nodes = Vec::new();

        for (node_id, grpc_address, gpu_count, status) in db_data {
            let node_id_parsed = NodeId::from_str(&node_id)
                .map_err(|e| anyhow::anyhow!("Invalid node ID '{}': {}", node_id, e))?;

            nodes.push(NodeInfoDetailed {
                id: node_id_parsed,
                status,
                capabilities: if gpu_count > 0 {
                    vec!["gpu".to_string()]
                } else {
                    vec![]
                },
                grpc_endpoint: grpc_address,
            });
        }

        Ok(nodes)
    }

    /// Combine discovered and known node lists
    fn combine_node_lists(
        &self,
        discovered: Vec<NodeInfoDetailed>,
        known: Vec<NodeInfoDetailed>,
    ) -> Vec<NodeInfoDetailed> {
        let mut combined = Vec::new();
        let mut seen_ids = std::collections::HashSet::new();

        for node in discovered {
            if seen_ids.insert(node.id.to_string()) {
                combined.push(node);
            }
        }

        for node in known {
            if seen_ids.insert(node.id.to_string()) {
                combined.push(node);
            }
        }

        combined
    }
}

/// SSH automation status information
#[derive(Debug, Clone)]
pub struct SshAutomationStatus {
    pub dynamic_discovery_enabled: bool,
    pub ssh_key_manager_available: bool,
    pub bittensor_service_available: bool,
    pub fallback_key_path: Option<PathBuf>,
}

impl std::fmt::Display for SshAutomationStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "SSH Automation Status[dynamic={}, key_manager={}, bittensor={}, fallback_key={}]",
            self.dynamic_discovery_enabled,
            self.ssh_key_manager_available,
            self.bittensor_service_available,
            self.fallback_key_path
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or("none".to_string())
        )
    }
}

/// Verification step tracking
#[derive(Debug, Clone)]
pub struct VerificationStep {
    pub step_name: String,
    pub status: StepStatus,
    pub duration: Duration,
    pub details: String,
}

/// Step status tracking
#[derive(Debug, Clone)]
pub enum StepStatus {
    Pending,
    InProgress,
    Completed,
    Failed,
    PartialSuccess,
}

/// Enhanced verification result structure
#[derive(Debug, Clone)]
pub struct VerificationResult {
    pub miner_uid: u16,
    pub overall_score: f64,
    pub verification_steps: Vec<VerificationStep>,
    pub completed_at: chrono::DateTime<chrono::Utc>,
    pub error: Option<String>,
}
