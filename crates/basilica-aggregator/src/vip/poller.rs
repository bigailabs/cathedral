use crate::config::VipConfig;
use crate::vip::{
    cache::VipCache,
    csv::{DataSourceError, VipDataSource},
    rental_ops::{
        close_vip_rental, get_vip_rental_by_machine_id, insert_vip_rental, prepare_vip_rental,
        update_vip_rental_metadata, PreparedVipRental, VipRentalError,
    },
    types::{ValidVipMachine, VipConnectionInfo, VipCsvRow, VipDisplayInfo, VipRentalRecord},
};
use basilica_billing::BillingClient;
use chrono::Utc;
use rust_decimal::prelude::ToPrimitive;
use sqlx::PgPool;
use std::collections::HashSet;
use std::sync::Arc;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum PollerError {
    #[error("Data source error: {0}")]
    DataSource(#[from] DataSourceError),
    #[error("Rental error: {0}")]
    Rental(#[from] VipRentalError),
    #[error("Database error: {0}")]
    Database(#[from] sqlx::Error),
}

/// Statistics from a single poll cycle
#[derive(Debug, Default, Clone)]
pub struct PollStats {
    pub total_rows: usize,
    pub active_rows: usize,
    pub skipped_inactive: usize,
    pub skipped_invalid: usize,
    pub created: usize,
    pub updated: usize,
    pub removed: usize,
}

/// VIP Poller that syncs rentals from a data source (CSV file, S3, or mock)
pub struct VipPoller<D: VipDataSource> {
    #[allow(dead_code)] // Retained for future use (e.g., feature flags)
    config: VipConfig,
    data_source: D,
    cache: Arc<VipCache>,
    db: PgPool,
    /// Markup percentage for VIP rentals (same as secure cloud)
    markup_percent: f64,
    /// Billing client for registering/finalizing VIP rentals
    billing_client: Option<Arc<BillingClient>>,
}

impl<D: VipDataSource> VipPoller<D> {
    pub fn new(
        config: VipConfig,
        data_source: D,
        cache: Arc<VipCache>,
        db: PgPool,
        markup_percent: f64,
        billing_client: Option<Arc<BillingClient>>,
    ) -> Self {
        Self {
            config,
            data_source,
            cache,
            db,
            markup_percent,
            billing_client,
        }
    }

    /// Perform a single poll cycle
    /// On CSV fetch failure, returns error WITHOUT mutating cache or rentals
    pub async fn poll_once(&self) -> Result<PollStats, PollerError> {
        let start_time = std::time::Instant::now();
        let mut stats = PollStats::default();

        // 1. Fetch all rows from data source
        let rows = match self.data_source.fetch_vip_rows().await {
            Ok(rows) => rows,
            Err(e) => {
                tracing::error!(
                    poll_success = false,
                    error = %e,
                    "VIP poll cycle failed - data source fetch error"
                );
                return Err(e.into());
            }
        };
        stats.total_rows = rows.len();

        // 2. Filter to active rows and validate
        let mut valid_machines: Vec<ValidVipMachine> = Vec::new();
        let mut seen_ids: HashSet<String> = HashSet::new();

        for row in rows {
            // Check if row is active
            if !row.active {
                stats.skipped_inactive += 1;
                tracing::debug!(
                    vip_machine_id = %row.vip_machine_id,
                    "Skipping row - inactive"
                );
                continue;
            }

            // Validate required fields
            if let Some(validated) = self.validate_row(&row) {
                seen_ids.insert(validated.vip_machine_id.clone());
                valid_machines.push(validated);
                stats.active_rows += 1;
            } else {
                stats.skipped_invalid += 1;
            }
        }

        // 3. Process valid machines
        for machine in &valid_machines {
            if let Err(e) = self.process_machine(machine, &mut stats).await {
                tracing::error!(
                    vip_machine_id = %machine.vip_machine_id,
                    error = %e,
                    "Failed to process VIP machine"
                );
            }
        }

        // 4. Find and remove stale entries (in cache but not in CSV)
        let stale_ids = self.cache.get_ids_not_in(&seen_ids).await;
        for stale_id in stale_ids {
            if let Err(e) = self.remove_stale_rental(&stale_id).await {
                tracing::error!(
                    vip_machine_id = %stale_id,
                    error = %e,
                    "Failed to remove stale VIP rental"
                );
            } else {
                stats.removed += 1;
            }
        }

        let elapsed = start_time.elapsed();
        let cache_size = self.cache.len().await;

        tracing::info!(
            poll_success = true,
            poll_duration_secs = elapsed.as_secs_f64(),
            total_rows = stats.total_rows,
            active_rows = stats.active_rows,
            skipped_inactive = stats.skipped_inactive,
            skipped_invalid = stats.skipped_invalid,
            created = stats.created,
            updated = stats.updated,
            removed = stats.removed,
            active_rentals = cache_size,
            "VIP poll cycle completed"
        );

        Ok(stats)
    }

    /// Validate a CSV row and convert to ValidVipMachine
    fn validate_row(&self, row: &VipCsvRow) -> Option<ValidVipMachine> {
        // Check required fields
        if row.vip_machine_id.is_empty() {
            tracing::warn!(row = ?row, "Invalid row: missing vip_machine_id");
            return None;
        }
        if row.assigned_user.is_empty() {
            tracing::warn!(vip_machine_id = %row.vip_machine_id, "Invalid row: missing assigned_user");
            return None;
        }
        if row.ssh_host.is_empty() {
            tracing::warn!(vip_machine_id = %row.vip_machine_id, "Invalid row: missing ssh_host");
            return None;
        }

        Some(ValidVipMachine {
            vip_machine_id: row.vip_machine_id.clone(),
            assigned_user: row.assigned_user.clone(),
            connection: VipConnectionInfo {
                ssh_host: row.ssh_host.clone(),
                ssh_port: row.ssh_port,
                ssh_user: row.ssh_user.clone(),
            },
            display: VipDisplayInfo {
                gpu_type: row.gpu_type.clone(),
                gpu_count: row.gpu_count,
                region: row.region.clone(),
                hourly_rate: row.hourly_rate,
                vcpu_count: row.vcpu_count,
                system_memory_gb: row.system_memory_gb,
                notes: row.notes.clone(),
            },
        })
    }

    /// Process a single valid VIP machine
    async fn process_machine(
        &self,
        machine: &ValidVipMachine,
        stats: &mut PollStats,
    ) -> Result<(), PollerError> {
        // Check if we have it in cache
        if let Some(cached) = self.cache.get(&machine.vip_machine_id).await {
            // Existing rental - check for changes
            if cached.assigned_user != machine.assigned_user {
                // User reassignment not supported - log and skip
                tracing::warn!(
                    vip_machine_id = %machine.vip_machine_id,
                    old_user = %cached.assigned_user,
                    new_user = %machine.assigned_user,
                    "User reassignment detected - skipping (not supported)"
                );
                return Ok(());
            }

            // Check if metadata changed
            let metadata_changed = self.metadata_differs(&cached, machine);
            if metadata_changed {
                // Update metadata in DB
                update_vip_rental_metadata(
                    &self.db,
                    &cached.secure_cloud_rental_id,
                    &machine.vip_machine_id,
                    &machine.connection,
                    &machine.display,
                )
                .await?;

                // Update cache
                let updated_record = VipRentalRecord {
                    vip_machine_id: machine.vip_machine_id.clone(),
                    assigned_user: machine.assigned_user.clone(),
                    secure_cloud_rental_id: cached.secure_cloud_rental_id.clone(),
                    connection: machine.connection.clone(),
                    display: machine.display.clone(),
                    last_seen_at: Utc::now(),
                };
                self.cache.insert(updated_record).await;

                stats.updated += 1;
                tracing::info!(
                    vip_machine_id = %machine.vip_machine_id,
                    "Updated VIP rental metadata"
                );
            }
        } else {
            // New machine - check if rental already exists in DB (restart recovery)
            let existing = get_vip_rental_by_machine_id(&self.db, &machine.vip_machine_id).await?;

            if let Some((rental_id, user_id)) = existing {
                // Rental exists in DB but not in cache - re-link
                if user_id != machine.assigned_user {
                    tracing::warn!(
                        vip_machine_id = %machine.vip_machine_id,
                        db_user = %user_id,
                        csv_user = %machine.assigned_user,
                        "User mismatch between DB and CSV - keeping DB user"
                    );
                }

                let record = VipRentalRecord {
                    vip_machine_id: machine.vip_machine_id.clone(),
                    assigned_user: user_id,
                    secure_cloud_rental_id: rental_id,
                    connection: machine.connection.clone(),
                    display: machine.display.clone(),
                    last_seen_at: Utc::now(),
                };
                self.cache.insert(record).await;

                tracing::info!(
                    vip_machine_id = %machine.vip_machine_id,
                    "Re-linked existing VIP rental to cache"
                );
            } else {
                // Create new rental
                let prepared = prepare_vip_rental(machine, self.markup_percent)?;

                // Register with billing service FIRST (before DB insert)
                if self.billing_client.is_some() {
                    if let Err(e) = self.register_with_billing(&prepared).await {
                        tracing::error!(
                            vip_machine_id = %machine.vip_machine_id,
                            error = %e,
                            "Failed to register VIP rental with billing - skipping"
                        );
                        return Ok(()); // Skip this machine, retry next poll
                    }
                }

                // Now insert to DB (billing registration succeeded)
                insert_vip_rental(&self.db, &prepared).await?;

                let record = VipRentalRecord {
                    vip_machine_id: machine.vip_machine_id.clone(),
                    assigned_user: machine.assigned_user.clone(),
                    secure_cloud_rental_id: prepared.rental_id.clone(),
                    connection: machine.connection.clone(),
                    display: machine.display.clone(),
                    last_seen_at: Utc::now(),
                };
                self.cache.insert(record).await;

                stats.created += 1;
                tracing::info!(
                    vip_machine_id = %machine.vip_machine_id,
                    rental_id = %prepared.rental_id,
                    user_id = %machine.assigned_user,
                    "Created new VIP rental"
                );
            }
        }

        Ok(())
    }

    /// Check if metadata has changed between cached and new machine data
    fn metadata_differs(&self, cached: &VipRentalRecord, new: &ValidVipMachine) -> bool {
        cached.connection.ssh_host != new.connection.ssh_host
            || cached.connection.ssh_port != new.connection.ssh_port
            || cached.connection.ssh_user != new.connection.ssh_user
            || cached.display.gpu_type != new.display.gpu_type
            || cached.display.gpu_count != new.display.gpu_count
            || cached.display.region != new.display.region
            || cached.display.vcpu_count != new.display.vcpu_count
            || cached.display.system_memory_gb != new.display.system_memory_gb
            || cached.display.notes != new.display.notes
    }

    /// Remove a stale VIP rental (no longer in CSV)
    async fn remove_stale_rental(&self, vip_machine_id: &str) -> Result<(), PollerError> {
        // Get rental info from cache
        if let Some(cached) = self.cache.get(vip_machine_id).await {
            // Finalize billing first (best-effort, don't block on failure)
            if self.billing_client.is_some() {
                self.finalize_rental_billing(&cached.secure_cloud_rental_id)
                    .await;
            }

            // Close the rental in DB
            close_vip_rental(&self.db, &cached.secure_cloud_rental_id, vip_machine_id).await?;

            // Remove from cache
            self.cache.remove(vip_machine_id).await;

            tracing::info!(
                vip_machine_id = %vip_machine_id,
                rental_id = %cached.secure_cloud_rental_id,
                "Removed stale VIP rental"
            );
        }

        Ok(())
    }

    /// Register a new VIP rental with the billing service
    async fn register_with_billing(
        &self,
        prepared: &PreparedVipRental,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        use basilica_protocol::billing::{
            track_rental_request::CloudType, GpuSpec, ResourceSpec, SecureCloudData,
            TrackRentalRequest,
        };

        let billing_client = self
            .billing_client
            .as_ref()
            .ok_or("No billing client configured")?;

        let resource_spec = Some(ResourceSpec {
            cpu_cores: prepared.vcpu_count,
            memory_mb: u64::from(prepared.system_memory_gb) * 1024,
            gpus: vec![GpuSpec {
                model: prepared.gpu_type.clone(),
                memory_mb: 0, // Not tracked for VIP
                count: prepared.gpu_count,
            }],
            disk_gb: 0,
            network_bandwidth_mbps: 0,
        });

        // Calculate per-GPU price from total marked-up rate
        let base_price_per_gpu = prepared.marked_up_hourly_rate.to_f64().unwrap_or(0.0)
            / prepared.gpu_count.max(1) as f64;

        let track_request = TrackRentalRequest {
            rental_id: prepared.rental_id.clone(),
            user_id: prepared.assigned_user.clone(),
            resource_spec,
            start_time: Some(prost_types::Timestamp::from(std::time::SystemTime::now())),
            metadata: std::collections::HashMap::new(),
            cloud_type: Some(CloudType::Secure(SecureCloudData {
                provider_instance_id: format!("vip:{}", prepared.vip_machine_id),
                provider: "vip".to_string(),
                offering_id: format!("vip-{}", prepared.vip_machine_id),
                base_price_per_gpu,
                gpu_count: prepared.gpu_count,
            })),
        };

        billing_client.track_rental(track_request).await?;

        tracing::info!(
            rental_id = %prepared.rental_id,
            vip_machine_id = %prepared.vip_machine_id,
            user_id = %prepared.assigned_user,
            base_price_per_gpu = %base_price_per_gpu,
            "Registered VIP rental with billing"
        );

        Ok(())
    }

    /// Finalize billing for a VIP rental (best-effort, logs on failure)
    async fn finalize_rental_billing(&self, rental_id: &str) {
        use basilica_protocol::billing::{FinalizeRentalRequest, RentalStatus};

        let billing_client = match self.billing_client.as_ref() {
            Some(client) => client,
            None => return,
        };

        let end_time = prost_types::Timestamp::from(std::time::SystemTime::now());

        let finalize_request = FinalizeRentalRequest {
            rental_id: rental_id.to_string(),
            end_time: Some(end_time),
            termination_reason: "vip_removed_from_csv".to_string(),
            target_status: RentalStatus::Stopped.into(),
        };

        match billing_client.finalize_rental(finalize_request).await {
            Ok(response) => {
                tracing::info!(
                    rental_id = %rental_id,
                    total_cost = %response.total_cost,
                    "Finalized VIP rental billing"
                );
            }
            Err(e) => {
                tracing::warn!(
                    rental_id = %rental_id,
                    error = %e,
                    "Failed to finalize VIP rental billing - proceeding with closure"
                );
            }
        }
    }
}
