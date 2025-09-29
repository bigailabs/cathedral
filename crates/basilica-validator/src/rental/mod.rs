//! Rental module for container deployment and management
//!
//! This module provides functionality for validators to rent GPU resources
//! and deploy containers on node machines.

use anyhow::Result;
use std::sync::Arc;
use uuid::Uuid;

pub mod container_client;
pub mod deployment;
pub mod monitoring;
pub mod types;

pub use container_client::ContainerClient;
pub use deployment::DeploymentManager;
pub use monitoring::{DatabaseHealthMonitor, LogStreamer};
pub use types::*;

use crate::metrics::ValidatorPrometheusMetrics;
use crate::miner_prover::miner_client::AuthenticatedMinerConnection;
use crate::persistence::{SimplePersistence, ValidatorPersistence};
use crate::ssh::ValidatorSshKeyManager;
// Removed: CloseSshSessionRequest no longer exists after node removal

/// Rental manager for coordinating container deployments
pub struct RentalManager {
    /// Persistence layer
    persistence: Arc<SimplePersistence>,
    /// Deployment manager
    deployment_manager: Arc<DeploymentManager>,
    /// Log streamer
    log_streamer: Arc<LogStreamer>,
    /// Health monitor
    health_monitor: Arc<DatabaseHealthMonitor>,
    /// SSH key manager for validator keys
    ssh_key_manager: Option<Arc<ValidatorSshKeyManager>>,
    /// Metrics for tracking rental status (required)
    metrics: Arc<ValidatorPrometheusMetrics>,
}

// /// Parse SSH host from credentials string format "user@host:port"
// fn parse_ssh_host(credentials: &str) -> Result<&str> {
//     let (_, host_port) = credentials
//         .split_once('@')
//         .context("Invalid SSH credentials format: missing '@' separator")?;

//     let host = host_port
//         .split(':')
//         .next()
//         .filter(|h| !h.is_empty())
//         .context("Invalid SSH credentials format: empty host")?;

//     Ok(host)
// }

/// Extract miner UID from miner_id format: "miner_{uid}"
pub(crate) fn extract_miner_uid(miner_id: &str) -> Option<u16> {
    if let Some(uid_str) = miner_id.strip_prefix("miner_") {
        return uid_str.parse().ok();
    }
    None
}

/// Get normalized GPU type from node details
pub(crate) fn get_gpu_type(node_details: &crate::api::types::NodeDetails) -> String {
    use crate::gpu::categorization::GpuCategory;
    use std::str::FromStr;

    node_details
        .gpu_specs
        .first()
        .map(|gpu| {
            let category = GpuCategory::from_str(&gpu.name).unwrap();
            category.to_string()
        })
        .unwrap_or_else(|| "unknown".to_string())
}

impl RentalManager {
    /// Helper function to create a ContainerClient with SSH credentials
    fn create_container_client(&self, ssh_credentials: &str) -> Result<ContainerClient> {
        let private_key_path = self
            .ssh_key_manager
            .as_ref()
            .and_then(|km| km.get_persistent_key())
            .map(|(_, path)| path.clone());

        ContainerClient::new(ssh_credentials.to_string(), private_key_path)
    }

    /// Create a new rental manager with SSH key manager
    pub fn new(
        persistence: Arc<SimplePersistence>,
        ssh_key_manager: Arc<ValidatorSshKeyManager>,
        metrics: Arc<ValidatorPrometheusMetrics>,
    ) -> Self {
        let deployment_manager = Arc::new(DeploymentManager::new());
        let log_streamer = Arc::new(LogStreamer::new());

        // Create health monitor with SSH key manager and metrics
        let health_monitor = Arc::new(DatabaseHealthMonitor::new(
            persistence.clone(),
            ssh_key_manager.clone(),
            metrics.clone(),
        ));

        Self {
            persistence,
            deployment_manager: deployment_manager.clone(),
            log_streamer: log_streamer.clone(),
            health_monitor,
            ssh_key_manager: Some(ssh_key_manager),
            metrics,
        }
    }

    // Start the monitoring loop
    pub fn start_monitor(&self) {
        self.health_monitor.start_monitoring_loop();
    }

    /// Initialize metrics for all existing rentals on startup
    pub async fn initialize_rental_metrics(&self) -> Result<()> {
        // Query all non-terminal rentals from persistence
        let rentals = self.persistence.query_non_terminated_rentals().await?;

        let rental_count = rentals.len();

        for rental in rentals {
            let miner_uid = extract_miner_uid(&rental.miner_id);

            if let Some(miner_uid) = miner_uid {
                let gpu_type = get_gpu_type(&rental.node_details);

                // Set metric based on rental state
                let is_rented = matches!(
                    rental.state,
                    RentalState::Active | RentalState::Provisioning | RentalState::Stopping
                );

                self.metrics.record_node_rental_status(
                    &rental.node_id,
                    miner_uid,
                    &gpu_type,
                    is_rented,
                );

                tracing::info!(
                    "Initialized rental metric for node {} (state: {:?}, is_rented: {})",
                    rental.node_id,
                    rental.state,
                    is_rented
                );
            }
        }

        tracing::info!("Initialized metrics for {} existing rentals", rental_count);
        Ok(())
    }

    /// Initialize metrics for all nodes on startup
    pub async fn initialize_node_metrics(&self) -> Result<()> {
        use crate::gpu::categorization::GpuCategory;
        use std::str::FromStr;

        // Get all nodes with their GPU and rental data in a single query
        let node_metrics = self.persistence.get_all_nodes_for_metrics().await?;

        let node_count = node_metrics.len();
        tracing::info!("Initializing metrics for {} nodes", node_count);

        for metric_data in node_metrics {
            // Convert GPU name to category
            let gpu_type = metric_data
                .gpu_name
                .and_then(|name| GpuCategory::from_str(&name).ok())
                .map(|category| category.to_string())
                .unwrap_or_else(|| "unknown".to_string());

            self.metrics.record_node_rental_status(
                &metric_data.node_id,
                metric_data.miner_uid,
                &gpu_type,
                metric_data.has_active_rental,
            );

            tracing::debug!(
                "Initialized node metric: node={}, miner_uid={}, gpu_type={}, is_rented={}",
                metric_data.node_id,
                metric_data.miner_uid,
                gpu_type,
                metric_data.has_active_rental
            );
        }

        tracing::info!("Successfully initialized metrics for {} nodes", node_count);
        Ok(())
    }

    /// Start a new rental
    pub async fn start_rental(
        &self,
        request: RentalRequest,
        _miner_connection: &mut AuthenticatedMinerConnection,
    ) -> Result<RentalResponse> {
        // Generate rental ID
        let rental_id = format!("rental-{}", Uuid::new_v4());

        let (_validator_public_key, _validator_private_key_path) = self
            .ssh_key_manager
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("SSH key manager is required for rentals"))?
            .get_persistent_key()
            .ok_or_else(|| anyhow::anyhow!("No persistent validator SSH key available"))?
            .clone();

        // For direct node connections, we'll use the node's node_ssh_endpoint as SSH credentials
        // Format is expected to be "user@host:port"
        let ssh_credentials = format!("root@{}", request.node_id);
        let container_client = self.create_container_client(&ssh_credentials)?;

        // Deploy container with end-user's SSH public key
        let container_info = match self
            .deployment_manager
            .deploy_container(
                &container_client,
                &request.container_spec,
                &rental_id,
                &request.ssh_public_key,
            )
            .await
        {
            Ok(info) => info,
            Err(e) => {
                // No explicit cleanup needed for direct node connections
                tracing::error!(
                    "Failed to deploy container on node {}: {}",
                    request.node_id,
                    e
                );
                return Err(e);
            }
        };

        // Check if SSH port is mapped and construct proper SSH credentials for end-user
        let ssh_credentials = container_info
            .mapped_ports
            .iter()
            .find(|p| p.container_port == 22)
            .map(|ssh_mapping| {
                // For direct node connections, extract host from node_id
                let host = request.node_id.split(':').next().unwrap_or("localhost");
                // Always use root as username for containers with the mapped port
                format!("root@{}:{}", host, ssh_mapping.host_port)
            });

        // Fetch node details from persistence
        let node_details = match self
            .persistence
            .get_node_details(&request.node_id, &request.miner_id)
            .await
        {
            Ok(Some(details)) => details,
            Ok(None) => {
                tracing::warn!(
                    "Node details not found for node_id: {}, using defaults",
                    request.node_id
                );
                // Provide default node details
                crate::api::types::NodeDetails {
                    id: request.node_id.clone(),
                    gpu_specs: vec![],
                    cpu_specs: crate::api::types::CpuSpec {
                        cores: 0,
                        model: "Unknown".to_string(),
                        memory_gb: 0,
                    },
                    location: None,
                    network_speed: None,
                }
            }
            Err(e) => {
                tracing::error!(
                    "Failed to fetch node details for node_id {}: {}",
                    request.node_id,
                    e
                );
                return Err(anyhow::anyhow!("Failed to fetch node details: {}", e));
            }
        };

        // Store rental info
        let rental_info = RentalInfo {
            rental_id: rental_id.clone(),
            validator_hotkey: request.validator_hotkey.clone(),
            node_id: request.node_id.clone(),
            container_id: container_info.container_id.clone(),
            ssh_session_id: format!("direct-{}", rental_id), // Direct connection, no session ID
            ssh_credentials: ssh_credentials.clone().unwrap_or_default(), // Store SSH credentials for operations
            state: RentalState::Active,
            created_at: chrono::Utc::now(),
            container_spec: request.container_spec.clone(),
            miner_id: request.miner_id.clone(),
            node_details,
        };

        // Save to persistence
        self.persistence.save_rental(&rental_info).await?;

        // Record rental metrics
        let miner_uid = extract_miner_uid(&rental_info.miner_id);

        if let Some(miner_uid) = miner_uid {
            let gpu_type = get_gpu_type(&rental_info.node_details);

            // Record rental status
            self.metrics.record_node_rental_status(
                &request.node_id,
                miner_uid,
                &gpu_type,
                true, // is_rented = true
            );

            // Record rental creation
            self.metrics.record_rental_created(miner_uid, &gpu_type);

            tracing::debug!(
                "Recorded rental start for node {} (miner_uid: {}, gpu_type: {})",
                request.node_id,
                miner_uid,
                gpu_type
            );
        }

        // Health monitoring happens automatically via the database monitor loop

        Ok(RentalResponse {
            rental_id,
            ssh_credentials,
            container_info,
        })
    }

    /// Get rental status
    pub async fn get_rental_status(&self, rental_id: &str) -> Result<RentalStatus> {
        let rental_info = self
            .persistence
            .load_rental(rental_id)
            .await?
            .ok_or_else(|| anyhow::anyhow!("Rental not found"))?;

        // Get container status using validator SSH credentials
        let container_client = self.create_container_client(&rental_info.ssh_credentials)?;

        let container_status = container_client
            .get_container_status(&rental_info.container_id)
            .await?;

        // Get resource usage
        let resource_usage = container_client
            .get_resource_usage(&rental_info.container_id)
            .await?;

        Ok(RentalStatus {
            rental_id: rental_id.to_string(),
            state: rental_info.state.clone(),
            container_status,
            created_at: rental_info.created_at,
            resource_usage,
        })
    }

    /// Stop a rental
    pub async fn stop_rental(&self, rental_id: &str, force: bool) -> Result<()> {
        let rental_info = self
            .persistence
            .load_rental(rental_id)
            .await?
            .ok_or_else(|| anyhow::anyhow!("Rental not found"))?;

        // Stop container using validator SSH credentials
        let container_client = self.create_container_client(&rental_info.ssh_credentials)?;

        self.deployment_manager
            .stop_container(&container_client, &rental_info.container_id, force)
            .await?;

        // Update rental state
        let mut updated_rental = rental_info.clone();
        updated_rental.state = RentalState::Stopped;
        self.persistence.save_rental(&updated_rental).await?;

        // Clear rental metric
        let miner_uid = extract_miner_uid(&rental_info.miner_id);

        if let Some(miner_uid) = miner_uid {
            let gpu_type = get_gpu_type(&rental_info.node_details);
            self.metrics.record_node_rental_status(
                &rental_info.node_id,
                miner_uid,
                &gpu_type,
                false, // is_rented = false
            );
            tracing::debug!(
                "Cleared rental metric for node {} (miner_uid: {}, gpu_type: {})",
                rental_info.node_id,
                miner_uid,
                gpu_type
            );
        }

        Ok(())
    }

    /// Stream container logs
    pub async fn stream_logs(
        &self,
        rental_id: &str,
        follow: bool,
        tail_lines: Option<u32>,
    ) -> Result<tokio::sync::mpsc::Receiver<LogEntry>> {
        let rental_info = self
            .persistence
            .load_rental(rental_id)
            .await?
            .ok_or_else(|| anyhow::anyhow!("Rental not found"))?;

        let container_client = self.create_container_client(&rental_info.ssh_credentials)?;

        self.log_streamer
            .stream_logs(
                &container_client,
                &rental_info.container_id,
                follow,
                tail_lines,
            )
            .await
    }

    pub async fn list_rentals(&self, validator_hotkey: &str) -> Result<Vec<RentalInfo>> {
        self.persistence
            .list_validator_rentals(validator_hotkey)
            .await
    }
}

impl Drop for RentalManager {
    fn drop(&mut self) {
        self.health_monitor.stop();
        tracing::debug!("Stopped health monitor for RentalManager");
    }
}

// #[cfg(test)]
// mod tests {
//     use super::*;

//     #[test]
//     fn test_parse_ssh_host() {
//         // Valid formats
//         assert_eq!(
//             parse_ssh_host("user@example.com:22").unwrap(),
//             "example.com"
//         );
//         assert_eq!(
//             parse_ssh_host("root@192.168.1.1:2222").unwrap(),
//             "192.168.1.1"
//         );
//         assert_eq!(parse_ssh_host("admin@host").unwrap(), "host");

//         // Invalid formats should return errors
//         assert!(parse_ssh_host("no-at-sign").is_err());
//         assert!(parse_ssh_host("@:22").is_err());
//         assert!(parse_ssh_host("user@").is_err());
//         assert!(parse_ssh_host("user@:22").is_err());
//         assert!(parse_ssh_host("").is_err());
//     }
// }
