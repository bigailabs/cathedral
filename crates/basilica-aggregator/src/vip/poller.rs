use crate::config::VipConfig;
use crate::models::format_vip_machine_id;
use crate::vip::{
    cache::VipCache,
    csv::{DataSourceError, VipDataSource},
    rental_ops::{
        apply_markup, close_vip_rental, delete_vip_rental, insert_vip_rental, prepare_vip_rental,
        update_vip_rental_metadata, PreparedVipRental, VipRentalError,
    },
    types::{ValidVipMachine, VipConnectionInfo, VipCsvRow, VipDisplayInfo, VipRentalRecord},
};
use basilica_billing::BillingClient;
use chrono::Utc;
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use rust_decimal::Decimal;
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
    #[error("Billing error: {0}")]
    Billing(String),
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
    pub skipped_conflict: usize,
    pub created: usize,
    pub updated: usize,
    pub removed: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProcessOutcome {
    Seen,
    Ignored,
}

#[derive(sqlx::FromRow)]
struct VipSnapshotRow {
    id: String,
    user_id: String,
    ip_address: Option<String>,
    connection_info: Option<serde_json::Value>,
    raw_response: Option<serde_json::Value>,
    instance_type: Option<String>,
    location_code: Option<String>,
}

#[derive(Debug, Clone)]
struct VipRentalSnapshot {
    rental_id: String,
    assigned_user: String,
    connection: VipConnectionInfo,
    display: VipDisplayInfo,
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
                valid_machines.push(validated);
                stats.active_rows += 1;
            } else {
                stats.skipped_invalid += 1;
            }
        }

        // 3. Process valid machines
        for machine in &valid_machines {
            match self.process_machine(machine, &mut stats).await {
                Ok(ProcessOutcome::Seen) => {
                    seen_ids.insert(machine.vip_machine_id.clone());
                }
                Ok(ProcessOutcome::Ignored) => {}
                Err(e) => {
                    tracing::error!(
                        vip_machine_id = %machine.vip_machine_id,
                        error = %e,
                        "Failed to process VIP machine"
                    );
                    // Be conservative: treat as seen to avoid stale removal on transient errors
                    seen_ids.insert(machine.vip_machine_id.clone());
                }
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
            skipped_conflict = stats.skipped_conflict,
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

    fn expected_marked_up_rate(&self, machine: &ValidVipMachine) -> Result<Decimal, PollerError> {
        Ok(apply_markup(
            machine.display.hourly_rate,
            self.markup_percent,
        )?)
    }

    /// Check if the user assignment matches (user changes require finalize+close)
    fn user_matches(&self, assigned_user: &str, machine: &ValidVipMachine) -> bool {
        assigned_user == machine.assigned_user
    }

    /// Check if metadata fields match (metadata changes can be updated in-place)
    fn metadata_matches(
        &self,
        connection: &VipConnectionInfo,
        display: &VipDisplayInfo,
        machine: &ValidVipMachine,
        expected_marked_up: Decimal,
    ) -> bool {
        if connection.ssh_host != machine.connection.ssh_host
            || connection.ssh_port != machine.connection.ssh_port
            || connection.ssh_user != machine.connection.ssh_user
        {
            return false;
        }
        if display.gpu_type != machine.display.gpu_type
            || display.gpu_count != machine.display.gpu_count
            || display.region != machine.display.region
            || display.vcpu_count != machine.display.vcpu_count
            || display.system_memory_gb != machine.display.system_memory_gb
            || display.notes != machine.display.notes
        {
            return false;
        }
        if display.hourly_rate != Decimal::ZERO && display.hourly_rate != expected_marked_up {
            return false;
        }
        true
    }

    fn parse_decimal(value: &serde_json::Value) -> Option<Decimal> {
        value
            .as_str()
            .and_then(|s| s.parse::<Decimal>().ok())
            .or_else(|| value.as_f64().and_then(Decimal::from_f64))
            .or_else(|| value.as_i64().and_then(Decimal::from_i64))
            .or_else(|| value.as_u64().and_then(Decimal::from_u64))
    }

    fn snapshot_from_row(&self, row: VipSnapshotRow) -> VipRentalSnapshot {
        let connection = if let Some(conn_json) = &row.connection_info {
            let ssh_host = conn_json
                .get("ssh_host")
                .and_then(|v| v.as_str())
                .unwrap_or(&row.ip_address.clone().unwrap_or_default())
                .to_string();
            let ssh_port = conn_json
                .get("ssh_port")
                .and_then(|v| v.as_u64())
                .unwrap_or(22) as u16;
            let ssh_user = conn_json
                .get("ssh_user")
                .and_then(|v| v.as_str())
                .unwrap_or("ubuntu")
                .to_string();
            VipConnectionInfo {
                ssh_host,
                ssh_port,
                ssh_user,
            }
        } else {
            VipConnectionInfo {
                ssh_host: row.ip_address.clone().unwrap_or_default(),
                ssh_port: 22,
                ssh_user: "ubuntu".to_string(),
            }
        };

        let display = if let Some(raw_json) = &row.raw_response {
            let gpu_type = raw_json
                .get("gpu_type")
                .and_then(|v| v.as_str())
                .unwrap_or(&row.instance_type.clone().unwrap_or_default())
                .to_string();
            let gpu_count = raw_json
                .get("gpu_count")
                .and_then(|v| v.as_u64())
                .unwrap_or(1) as u32;
            let vcpu_count = raw_json
                .get("vcpu_count")
                .and_then(|v| v.as_u64())
                .unwrap_or(0) as u32;
            let system_memory_gb = raw_json
                .get("system_memory_gb")
                .and_then(|v| v.as_u64())
                .unwrap_or(0) as u32;
            let notes = raw_json
                .get("notes")
                .and_then(|v| v.as_str())
                .map(String::from);
            let hourly_rate = raw_json
                .get("hourly_rate")
                .and_then(Self::parse_decimal)
                .unwrap_or(Decimal::ZERO);

            VipDisplayInfo {
                gpu_type,
                gpu_count,
                region: row.location_code.clone().unwrap_or_default(),
                hourly_rate,
                vcpu_count,
                system_memory_gb,
                notes,
            }
        } else {
            VipDisplayInfo {
                gpu_type: row.instance_type.clone().unwrap_or_default(),
                gpu_count: 1,
                region: row.location_code.clone().unwrap_or_default(),
                hourly_rate: Decimal::ZERO,
                vcpu_count: 0,
                system_memory_gb: 0,
                notes: None,
            }
        };

        VipRentalSnapshot {
            rental_id: row.id,
            assigned_user: row.user_id,
            connection,
            display,
        }
    }

    async fn fetch_active_snapshot(
        &self,
        vip_machine_id: &str,
    ) -> Result<Option<VipRentalSnapshot>, PollerError> {
        let prefixed_machine_id = format_vip_machine_id(vip_machine_id);
        let row: Option<VipSnapshotRow> = sqlx::query_as(
            r#"
            SELECT id, user_id, ip_address, connection_info, raw_response, instance_type, location_code
            FROM secure_cloud_rentals
            WHERE provider_instance_id = $1 AND is_vip = TRUE AND status != 'stopped'
            LIMIT 1
            "#,
        )
        .bind(&prefixed_machine_id)
        .fetch_optional(&self.db)
        .await?;

        Ok(row.map(|r| self.snapshot_from_row(r)))
    }

    async fn fetch_terminated_snapshot(
        &self,
        vip_machine_id: &str,
    ) -> Result<Option<VipRentalSnapshot>, PollerError> {
        let prefixed_machine_id = format_vip_machine_id(vip_machine_id);
        let row: Option<VipSnapshotRow> = sqlx::query_as(
            r#"
            SELECT id, user_id, ip_address, connection_info, raw_response, instance_type, location_code
            FROM terminated_secure_cloud_rentals
            WHERE provider_instance_id = $1
            ORDER BY stopped_at DESC
            LIMIT 1
            "#,
        )
        .bind(&prefixed_machine_id)
        .fetch_optional(&self.db)
        .await?;

        Ok(row.map(|r| self.snapshot_from_row(r)))
    }

    /// Process a single valid VIP machine
    async fn process_machine(
        &self,
        machine: &ValidVipMachine,
        stats: &mut PollStats,
    ) -> Result<ProcessOutcome, PollerError> {
        let expected_marked_up = self.expected_marked_up_rate(machine)?;

        // 1) Check cache first
        if let Some(cached) = self.cache.get(&machine.vip_machine_id).await {
            // User change requires finalize+close (reassignment to different user)
            if !self.user_matches(&cached.assigned_user, machine) {
                stats.skipped_conflict += 1;
                tracing::error!(
                    vip_machine_id = %machine.vip_machine_id,
                    old_user = %cached.assigned_user,
                    new_user = %machine.assigned_user,
                    "User assignment changed - treating as removal"
                );

                if let Err(e) = self
                    .finalize_and_close(&cached.secure_cloud_rental_id, &machine.vip_machine_id)
                    .await
                {
                    tracing::error!(
                        vip_machine_id = %machine.vip_machine_id,
                        error = %e,
                        "Failed to remove conflicting VIP rental"
                    );
                    return Ok(ProcessOutcome::Ignored);
                }

                stats.removed += 1;
                return Ok(ProcessOutcome::Ignored);
            }

            // Metadata change - update in-place without terminating the rental
            if !self.metadata_matches(
                &cached.connection,
                &cached.display,
                machine,
                expected_marked_up,
            ) {
                // Update database
                if let Err(e) = update_vip_rental_metadata(
                    &self.db,
                    &cached.secure_cloud_rental_id,
                    &machine.vip_machine_id,
                    &machine.connection,
                    &machine.display,
                )
                .await
                {
                    tracing::error!(
                        vip_machine_id = %machine.vip_machine_id,
                        error = %e,
                        "Failed to update VIP rental metadata"
                    );
                    return Ok(ProcessOutcome::Ignored);
                }

                // Update cache with new values
                let updated = VipRentalRecord {
                    vip_machine_id: cached.vip_machine_id.clone(),
                    assigned_user: cached.assigned_user.clone(),
                    secure_cloud_rental_id: cached.secure_cloud_rental_id.clone(),
                    connection: machine.connection.clone(),
                    display: VipDisplayInfo {
                        hourly_rate: expected_marked_up,
                        ..machine.display.clone()
                    },
                    last_seen_at: Utc::now(),
                };
                self.cache.insert(updated).await;

                stats.updated += 1;
                tracing::info!(
                    vip_machine_id = %machine.vip_machine_id,
                    "Updated VIP rental metadata"
                );
                return Ok(ProcessOutcome::Seen);
            }

            // Normalize cached hourly rate to marked-up value
            if cached.display.hourly_rate != expected_marked_up {
                let mut updated = cached.clone();
                updated.display.hourly_rate = expected_marked_up;
                updated.last_seen_at = Utc::now();
                self.cache.insert(updated).await;
            }

            return Ok(ProcessOutcome::Seen);
        }

        // 2) Check DB for active rental (cache miss / restart recovery)
        if let Some(active) = self.fetch_active_snapshot(&machine.vip_machine_id).await? {
            // User change requires finalize+close (reassignment to different user)
            if !self.user_matches(&active.assigned_user, machine) {
                stats.skipped_conflict += 1;
                tracing::error!(
                    vip_machine_id = %machine.vip_machine_id,
                    old_user = %active.assigned_user,
                    new_user = %machine.assigned_user,
                    "User assignment changed for existing rental - treating as removal"
                );

                if let Err(e) = self
                    .finalize_and_close(&active.rental_id, &machine.vip_machine_id)
                    .await
                {
                    tracing::error!(
                        vip_machine_id = %machine.vip_machine_id,
                        error = %e,
                        "Failed to remove conflicting VIP rental"
                    );
                    return Ok(ProcessOutcome::Ignored);
                }

                stats.removed += 1;
                return Ok(ProcessOutcome::Ignored);
            }

            // Metadata change - update in-place without terminating the rental
            let needs_metadata_update = !self.metadata_matches(
                &active.connection,
                &active.display,
                machine,
                expected_marked_up,
            );

            if needs_metadata_update {
                if let Err(e) = update_vip_rental_metadata(
                    &self.db,
                    &active.rental_id,
                    &machine.vip_machine_id,
                    &machine.connection,
                    &machine.display,
                )
                .await
                {
                    tracing::error!(
                        vip_machine_id = %machine.vip_machine_id,
                        error = %e,
                        "Failed to update VIP rental metadata"
                    );
                    return Ok(ProcessOutcome::Ignored);
                }
                stats.updated += 1;
                tracing::info!(
                    vip_machine_id = %machine.vip_machine_id,
                    "Updated VIP rental metadata during cache recovery"
                );
            }

            // Use updated values for cache if metadata was updated
            let (connection, display) = if needs_metadata_update {
                (
                    machine.connection.clone(),
                    VipDisplayInfo {
                        hourly_rate: expected_marked_up,
                        ..machine.display.clone()
                    },
                )
            } else {
                let mut display = active.display.clone();
                if display.hourly_rate == Decimal::ZERO {
                    display.hourly_rate = expected_marked_up;
                }
                (active.connection, display)
            };

            let record = VipRentalRecord {
                vip_machine_id: machine.vip_machine_id.clone(),
                assigned_user: active.assigned_user,
                secure_cloud_rental_id: active.rental_id,
                connection,
                display,
                last_seen_at: Utc::now(),
            };
            self.cache.insert(record).await;

            tracing::info!(
                vip_machine_id = %machine.vip_machine_id,
                "Re-linked existing VIP rental to cache"
            );

            return Ok(ProcessOutcome::Seen);
        }

        // 3) If this ID existed before with a different user, block reuse
        // (Metadata differences are allowed - only user reassignment blocks reuse)
        if let Some(terminated) = self
            .fetch_terminated_snapshot(&machine.vip_machine_id)
            .await?
        {
            if !self.user_matches(&terminated.assigned_user, machine) {
                stats.skipped_conflict += 1;
                tracing::error!(
                    vip_machine_id = %machine.vip_machine_id,
                    old_user = %terminated.assigned_user,
                    new_user = %machine.assigned_user,
                    "VIP row attempts to reuse ID with different user - ignoring"
                );
                return Ok(ProcessOutcome::Ignored);
            }
        }

        // 4) Create new rental
        let prepared = prepare_vip_rental(machine, self.markup_percent)?;

        // Insert into DB first (local operation) - if this fails, just skip
        if let Err(e) = insert_vip_rental(&self.db, &prepared).await {
            tracing::error!(
                vip_machine_id = %machine.vip_machine_id,
                error = %e,
                "Failed to insert VIP rental"
            );
            return Ok(ProcessOutcome::Ignored);
        }

        // Then register with billing - if this fails, delete the DB row to rollback
        if self.billing_client.is_some() {
            if let Err(e) = self.register_with_billing(&prepared).await {
                tracing::error!(
                    vip_machine_id = %machine.vip_machine_id,
                    rental_id = %prepared.rental_id,
                    error = %e,
                    "Failed to register VIP rental with billing - rolling back DB insert"
                );

                if let Err(del_err) =
                    delete_vip_rental(&self.db, &prepared.rental_id, &prepared.vip_machine_id).await
                {
                    tracing::error!(
                        rental_id = %prepared.rental_id,
                        error = %del_err,
                        "Failed to delete VIP rental after billing failure"
                    );
                }

                return Ok(ProcessOutcome::Ignored);
            }
        }

        let mut display = machine.display.clone();
        display.hourly_rate = prepared.marked_up_hourly_rate;

        let record = VipRentalRecord {
            vip_machine_id: machine.vip_machine_id.clone(),
            assigned_user: machine.assigned_user.clone(),
            secure_cloud_rental_id: prepared.rental_id.clone(),
            connection: machine.connection.clone(),
            display,
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

        Ok(ProcessOutcome::Seen)
    }

    /// Remove a stale VIP rental (no longer in CSV)
    async fn remove_stale_rental(&self, vip_machine_id: &str) -> Result<(), PollerError> {
        // Get rental info from cache
        if let Some(cached) = self.cache.get(vip_machine_id).await {
            self.finalize_and_close(&cached.secure_cloud_rental_id, vip_machine_id)
                .await?;

            tracing::info!(
                vip_machine_id = %vip_machine_id,
                rental_id = %cached.secure_cloud_rental_id,
                "Removed stale VIP rental"
            );
        }

        Ok(())
    }

    async fn finalize_and_close(
        &self,
        rental_id: &str,
        vip_machine_id: &str,
    ) -> Result<(), PollerError> {
        self.finalize_rental_billing(rental_id).await?;
        close_vip_rental(&self.db, rental_id, vip_machine_id).await?;
        self.cache.remove(vip_machine_id).await;
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
        let base_price_per_gpu = prepared.marked_up_hourly_rate.to_f64().ok_or_else(|| {
            format!(
                "Failed to convert marked_up_hourly_rate {} to f64 for billing",
                prepared.marked_up_hourly_rate
            )
        })? / prepared.gpu_count.max(1) as f64;

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

    /// Finalize billing for a VIP rental (must succeed before DB removal)
    async fn finalize_rental_billing(&self, rental_id: &str) -> Result<(), PollerError> {
        use basilica_protocol::billing::{FinalizeRentalRequest, RentalStatus};

        let billing_client = match self.billing_client.as_ref() {
            Some(client) => client,
            None => return Ok(()),
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
                Ok(())
            }
            Err(e) => {
                tracing::error!(
                    rental_id = %rental_id,
                    error = %e,
                    "Failed to finalize VIP rental billing"
                );
                Err(PollerError::Billing(e.to_string()))
            }
        }
    }
}
