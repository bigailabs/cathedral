use crate::config::VipConfig;
use crate::vip::{
    cache::VipCache,
    rental_ops::{
        close_vip_rental, get_vip_rental_by_machine_id, insert_vip_rental, prepare_vip_rental,
        update_vip_rental_metadata, VipRentalError,
    },
    sheets::{GoogleSheetsClient, SheetsError},
    types::{ValidVipMachine, VipConnectionInfo, VipDisplayInfo, VipRentalRecord, VipSheetRow},
};
use chrono::Utc;
use sqlx::PgPool;
use std::collections::HashSet;
use std::sync::Arc;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum PollerError {
    #[error("Sheets error: {0}")]
    Sheets(#[from] SheetsError),
    #[error("Rental error: {0}")]
    Rental(#[from] VipRentalError),
    #[error("Database error: {0}")]
    Database(#[from] sqlx::Error),
}

/// Statistics from a single poll cycle
#[derive(Debug, Default, Clone)]
pub struct PollStats {
    pub total_rows: usize,
    pub ready_rows: usize,
    pub skipped_not_ready: usize,
    pub skipped_invalid: usize,
    pub created: usize,
    pub updated: usize,
    pub removed: usize,
}

/// VIP Poller that syncs rentals from Google Sheet
pub struct VipPoller {
    config: VipConfig,
    sheets_client: GoogleSheetsClient,
    cache: Arc<VipCache>,
    db: PgPool,
}

impl VipPoller {
    pub fn new(
        config: VipConfig,
        sheets_client: GoogleSheetsClient,
        cache: Arc<VipCache>,
        db: PgPool,
    ) -> Self {
        Self {
            config,
            sheets_client,
            cache,
            db,
        }
    }

    /// Perform a single poll cycle
    /// On sheet fetch failure, returns error WITHOUT mutating cache or rentals
    pub async fn poll_once(&self) -> Result<PollStats, PollerError> {
        let start_time = std::time::Instant::now();
        let mut stats = PollStats::default();

        // 1. Fetch all rows from sheet
        let rows = match self.sheets_client.fetch_vip_rows().await {
            Ok(rows) => rows,
            Err(e) => {
                tracing::error!(
                    poll_success = false,
                    error = %e,
                    "VIP poll cycle failed - sheet fetch error"
                );
                return Err(e.into());
            }
        };
        stats.total_rows = rows.len();

        // 2. Filter to ready rows and validate
        let mut valid_machines: Vec<ValidVipMachine> = Vec::new();
        let mut seen_ids: HashSet<String> = HashSet::new();

        for row in rows {
            // Check readiness gate
            if row.ready != self.config.ready_value {
                stats.skipped_not_ready += 1;
                tracing::debug!(
                    vip_machine_id = %row.vip_machine_id,
                    ready_value = %row.ready,
                    expected = %self.config.ready_value,
                    "Skipping row - not ready"
                );
                continue;
            }

            // Validate required fields
            if let Some(validated) = self.validate_row(&row) {
                seen_ids.insert(validated.vip_machine_id.clone());
                valid_machines.push(validated);
                stats.ready_rows += 1;
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

        // 4. Find and remove stale entries (in cache but not in sheet)
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
            ready_rows = stats.ready_rows,
            skipped_not_ready = stats.skipped_not_ready,
            skipped_invalid = stats.skipped_invalid,
            created = stats.created,
            updated = stats.updated,
            removed = stats.removed,
            active_rentals = cache_size,
            "VIP poll cycle completed"
        );

        Ok(stats)
    }

    /// Validate a sheet row and convert to ValidVipMachine
    fn validate_row(&self, row: &VipSheetRow) -> Option<ValidVipMachine> {
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
                        sheet_user = %machine.assigned_user,
                        "User mismatch between DB and sheet - keeping DB user"
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
                let prepared = prepare_vip_rental(machine)?;
                insert_vip_rental(&self.db, &prepared).await?;

                // TODO: Call billing service to track rental
                // This needs to be done in basilica-api where BillingClient is available
                // For now, the rental is created in DB, billing will be handled when
                // the VIP poller is wired up in basilica-api

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
            || cached.display.notes != new.display.notes
    }

    /// Remove a stale VIP rental (no longer in sheet)
    async fn remove_stale_rental(&self, vip_machine_id: &str) -> Result<(), PollerError> {
        // Get rental info from cache
        if let Some(cached) = self.cache.get(vip_machine_id).await {
            // Close the rental in DB
            close_vip_rental(&self.db, &cached.secure_cloud_rental_id, vip_machine_id).await?;

            // TODO: Finalize billing - needs BillingClient from basilica-api

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
}
