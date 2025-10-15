//! Billing telemetry collection and streaming
//!
//! This module provides a separate monitoring loop for collecting resource usage
//! telemetry and streaming it to the billing service.

use anyhow::{Context, Result};
use std::sync::Arc;
use std::time::Duration;
use tokio::time::interval;
use tokio_util::sync::CancellationToken;
use tracing::{debug, error, info, warn};

use super::container_client::ContainerClient;
use crate::billing::{resource_usage_to_telemetry, BillingClient};
use crate::config::BillingConfig;
use crate::persistence::{SimplePersistence, ValidatorPersistence};
use crate::ssh::ValidatorSshKeyManager;

/// Billing telemetry monitor for rentals
#[derive(Clone)]
pub struct RentalBillingMonitor {
    /// Persistence layer for database operations
    persistence: Arc<SimplePersistence>,
    /// SSH key manager for validator keys
    ssh_key_manager: Arc<ValidatorSshKeyManager>,
    /// Billing client for telemetry streaming
    billing_client: Arc<BillingClient>,
    /// Billing collection interval
    collection_interval: Duration,
    /// Cancellation token for the monitoring loop
    cancellation_token: CancellationToken,
}

impl RentalBillingMonitor {
    /// Create a new billing monitor
    pub fn new(
        persistence: Arc<SimplePersistence>,
        ssh_key_manager: Arc<ValidatorSshKeyManager>,
        billing_client: Arc<BillingClient>,
        config: &BillingConfig,
    ) -> Self {
        Self {
            persistence,
            ssh_key_manager,
            billing_client,
            collection_interval: Duration::from_secs(config.collection_interval_secs),
            cancellation_token: CancellationToken::new(),
        }
    }

    /// Start the billing monitoring loop
    pub fn start(&self) {
        let monitor = self.clone();
        tokio::spawn(async move {
            monitor.monitoring_loop().await;
        });
    }

    /// Stop the billing monitoring loop
    pub fn stop(&self) {
        self.cancellation_token.cancel();
    }

    /// Main monitoring loop for billing telemetry collection
    async fn monitoring_loop(&self) {
        let mut check_interval = interval(self.collection_interval);
        info!("Billing telemetry monitor started");

        loop {
            tokio::select! {
                _ = self.cancellation_token.cancelled() => {
                    info!("Billing telemetry monitor stopped");
                    break;
                }
                _ = check_interval.tick() => {
                    if let Err(e) = self.collect_and_stream_telemetry().await {
                        error!("Error collecting billing telemetry: {}", e);
                    }
                }
            }
        }
    }

    /// Collect and stream telemetry for all active rentals
    async fn collect_and_stream_telemetry(&self) -> Result<()> {
        let rentals = self
            .persistence
            .query_non_terminated_rentals()
            .await
            .context("Failed to query non-terminal rentals")?;

        debug!("Collecting telemetry for {} rentals", rentals.len());

        for rental in rentals {
            if let Err(e) = self.collect_rental_telemetry(&rental).await {
                warn!(
                    "Failed to collect telemetry for rental {}: {}",
                    rental.rental_id, e
                );
            }
        }

        Ok(())
    }

    /// Collect and stream telemetry for a single rental
    async fn collect_rental_telemetry(
        &self,
        rental: &crate::rental::types::RentalInfo,
    ) -> Result<()> {
        let validator_private_key_path = self
            .ssh_key_manager
            .get_persistent_key()
            .ok_or_else(|| anyhow::anyhow!("No persistent validator SSH key available"))?
            .1
            .clone();

        let container_client = ContainerClient::new(
            rental.ssh_credentials.clone(),
            Some(validator_private_key_path),
        )?;

        let mut usage = container_client
            .get_resource_usage(&rental.container_id)
            .await
            .context("Failed to get resource usage")?;

        let gpu_count = self
            .persistence
            .get_active_rental_gpu_count(&rental.rental_id)
            .await
            .context("Failed to query GPU count from database")?;

        usage.gpu_usage = (0..gpu_count)
            .map(|index| crate::rental::types::GpuUsage {
                gpu_index: index,
                utilization_percent: 0.0,
                memory_mb: 0,
                temperature_celsius: 0.0,
            })
            .collect();

        debug!(
            rental_id = %rental.rental_id,
            gpu_count = gpu_count,
            "Injected GPU count from database into telemetry"
        );

        let telemetry =
            resource_usage_to_telemetry(rental.rental_id.clone(), rental.node_id.clone(), usage)
                .context("Failed to convert resource usage to telemetry")?;

        self.billing_client
            .stream_telemetry(telemetry)
            .await
            .context("Failed to stream telemetry")?;

        debug!(
            "Successfully collected and streamed telemetry for rental {}",
            rental.rental_id
        );

        Ok(())
    }
}
