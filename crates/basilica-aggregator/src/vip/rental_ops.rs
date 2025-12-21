use crate::models::format_vip_machine_id;
use crate::vip::types::{ValidVipMachine, VipConnectionInfo, VipDisplayInfo};
use chrono::Utc;
use rust_decimal::prelude::FromPrimitive;
use rust_decimal::Decimal;
use sqlx::PgPool;
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Error)]
pub enum VipRentalError {
    #[error("Database error: {0}")]
    Database(#[from] sqlx::Error),
    #[error("Price conversion error: {0}")]
    PriceConversion(String),
}

/// Apply markup to the hourly rate (same formula as Secure Cloud in balance.rs)
pub fn apply_markup(base_rate: Decimal, markup_percent: f64) -> Result<Decimal, VipRentalError> {
    let multiplier = Decimal::from_f64(1.0 + (markup_percent / 100.0)).ok_or_else(|| {
        VipRentalError::PriceConversion(format!("Invalid markup percent: {}", markup_percent))
    })?;

    base_rate
        .checked_mul(multiplier)
        .ok_or_else(|| VipRentalError::PriceConversion("Markup calculation overflow".into()))
}

/// VIP rental data prepared for database insertion
/// This struct is returned by prepare_vip_rental and can be used by basilica-api
/// to insert the rental and register with billing
#[derive(Debug, Clone)]
pub struct PreparedVipRental {
    pub rental_id: String,
    pub vip_machine_id: String,
    pub assigned_user: String,
    pub ssh_host: String,
    pub ssh_port: u16,
    pub ssh_user: String,
    pub gpu_type: String,
    pub gpu_count: u32,
    pub region: String,
    pub marked_up_hourly_rate: Decimal,
    pub vcpu_count: u32,
    pub system_memory_gb: u32,
    pub notes: Option<String>,
}

/// Prepare a VIP rental for creation - generates rental ID and applies markup
/// This allows the calling code (basilica-api) to coordinate database insertion
/// and billing registration as needed
pub fn prepare_vip_rental(
    vip_machine: &ValidVipMachine,
    markup_percent: f64,
) -> Result<PreparedVipRental, VipRentalError> {
    let rental_id = Uuid::new_v4().to_string();
    let marked_up_hourly_rate = apply_markup(vip_machine.display.hourly_rate, markup_percent)?;

    Ok(PreparedVipRental {
        rental_id,
        vip_machine_id: vip_machine.vip_machine_id.clone(),
        assigned_user: vip_machine.assigned_user.clone(),
        ssh_host: vip_machine.connection.ssh_host.clone(),
        ssh_port: vip_machine.connection.ssh_port,
        ssh_user: vip_machine.connection.ssh_user.clone(),
        gpu_type: vip_machine.display.gpu_type.clone(),
        gpu_count: vip_machine.display.gpu_count,
        region: vip_machine.display.region.clone(),
        marked_up_hourly_rate,
        vcpu_count: vip_machine.display.vcpu_count,
        system_memory_gb: vip_machine.display.system_memory_gb,
        notes: vip_machine.display.notes.clone(),
    })
}

/// Insert a VIP rental into the database
/// This is typically called from basilica-api after billing registration succeeds
pub async fn insert_vip_rental(
    pool: &PgPool,
    prepared: &PreparedVipRental,
) -> Result<(), VipRentalError> {
    let now = Utc::now();
    let prefixed_machine_id = format_vip_machine_id(&prepared.vip_machine_id);

    // Insert into secure_cloud_rentals table
    // VIP rentals don't have a real provider or offering_id - use special values
    // VIP machine ID is stored in provider_instance_id with 'vip:' prefix
    // Note: ssh_public_key is NULL for VIP rentals (SSH access managed externally by VIP team)
    sqlx::query(
        r#"
        INSERT INTO secure_cloud_rentals (
            id, user_id, provider, provider_instance_id, offering_id, instance_type,
            location_code, status, hostname, ssh_public_key, ip_address, connection_info,
            raw_response, error_message, created_at, updated_at, is_vip
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17
        )
        "#,
    )
    .bind(&prepared.rental_id)
    .bind(&prepared.assigned_user)
    .bind("vip") // Provider marker for VIP
    .bind(&prefixed_machine_id) // provider_instance_id with 'vip:' prefix
    .bind(format!("vip-{}", prepared.vip_machine_id)) // offering_id - use vip_machine_id for reference
    .bind(&prepared.gpu_type) // instance_type
    .bind(&prepared.region) // location_code
    .bind("running") // status - VIP machines are always running
    .bind(format!("vip-{}", prepared.vip_machine_id)) // hostname
    .bind::<Option<&str>>(None) // ssh_public_key - VIP doesn't use registered SSH keys (team manages manually)
    .bind(&prepared.ssh_host) // ip_address
    .bind(serde_json::json!({
        "ssh_host": prepared.ssh_host,
        "ssh_port": prepared.ssh_port,
        "ssh_user": prepared.ssh_user,
    })) // connection_info
    .bind(serde_json::json!({
        "notes": prepared.notes,
        "gpu_type": prepared.gpu_type,
        "gpu_count": prepared.gpu_count,
        "vcpu_count": prepared.vcpu_count,
        "system_memory_gb": prepared.system_memory_gb,
        "hourly_rate": prepared.marked_up_hourly_rate.to_string(),
    })) // raw_response - store VIP metadata
    .bind::<Option<&str>>(None) // error_message
    .bind(now) // created_at
    .bind(now) // updated_at
    .bind(true) // is_vip
    .execute(pool)
    .await?;

    tracing::info!(
        rental_id = %prepared.rental_id,
        vip_machine_id = %prepared.vip_machine_id,
        user_id = %prepared.assigned_user,
        hourly_rate = %prepared.marked_up_hourly_rate,
        "Inserted VIP rental into database"
    );

    Ok(())
}

/// Delete a VIP rental from the database (used for rollback when billing registration fails).
/// Unlike close_vip_rental, this does not archive - the rental never actually started.
pub async fn delete_vip_rental(
    pool: &PgPool,
    rental_id: &str,
    vip_machine_id: &str,
) -> Result<(), VipRentalError> {
    let prefixed_machine_id = format_vip_machine_id(vip_machine_id);

    sqlx::query(
        r#"
        DELETE FROM secure_cloud_rentals
        WHERE id = $1 AND provider_instance_id = $2 AND is_vip = TRUE
        "#,
    )
    .bind(rental_id)
    .bind(&prefixed_machine_id)
    .execute(pool)
    .await?;

    tracing::info!(
        rental_id = %rental_id,
        vip_machine_id = %vip_machine_id,
        "Deleted VIP rental (billing registration failed)"
    );

    Ok(())
}

/// Close a VIP rental - finalize in database and archive
/// Note: Billing finalization must be done by the caller (basilica-api has access to BillingClient)
pub async fn close_vip_rental(
    pool: &PgPool,
    rental_id: &str,
    vip_machine_id: &str,
) -> Result<(), VipRentalError> {
    let now = Utc::now();
    let prefixed_machine_id = format_vip_machine_id(vip_machine_id);

    // 1. Update the rental status to 'stopped'
    let result = sqlx::query(
        r#"
        UPDATE secure_cloud_rentals
        SET status = 'stopped',
            stopped_at = $1,
            updated_at = $1
        WHERE id = $2 AND provider_instance_id = $3 AND is_vip = TRUE
        "#,
    )
    .bind(now)
    .bind(rental_id)
    .bind(&prefixed_machine_id)
    .execute(pool)
    .await?;

    if result.rows_affected() == 0 {
        tracing::warn!(
            rental_id = %rental_id,
            vip_machine_id = %vip_machine_id,
            "No VIP rental found to close - may already be closed"
        );
    }

    // 2. Archive to terminated_secure_cloud_rentals (if the table exists)
    // This uses INSERT ... SELECT pattern to copy the record
    // Note: ssh_public_key replaces ssh_key_id after migration 017
    let archive_result = sqlx::query(
        r#"
        INSERT INTO terminated_secure_cloud_rentals (
            id, user_id, provider, provider_instance_id, offering_id, instance_type,
            location_code, status, hostname, ssh_public_key, ip_address, connection_info,
            raw_response, error_message, created_at, updated_at, stopped_at,
            stop_reason
        )
        SELECT
            id, user_id, provider, provider_instance_id, offering_id, instance_type,
            location_code, status, hostname, ssh_public_key, ip_address, connection_info,
            raw_response, error_message, created_at, updated_at, stopped_at,
            'vip_removed_from_csv'
        FROM secure_cloud_rentals
        WHERE id = $1 AND provider_instance_id = $2 AND is_vip = TRUE
        ON CONFLICT (id) DO NOTHING
        "#,
    )
    .bind(rental_id)
    .bind(&prefixed_machine_id)
    .execute(pool)
    .await;

    if let Err(e) = archive_result {
        // Log but don't fail - archiving is best effort
        tracing::warn!(
            rental_id = %rental_id,
            error = %e,
            "Failed to archive VIP rental (non-fatal)"
        );
    }

    // 3. Delete from secure_cloud_rentals
    sqlx::query(
        r#"
        DELETE FROM secure_cloud_rentals
        WHERE id = $1 AND provider_instance_id = $2 AND is_vip = TRUE
        "#,
    )
    .bind(rental_id)
    .bind(&prefixed_machine_id)
    .execute(pool)
    .await?;

    tracing::info!(
        rental_id = %rental_id,
        vip_machine_id = %vip_machine_id,
        "Closed VIP rental"
    );

    Ok(())
}

/// Get a VIP rental by vip_machine_id for checking existence
pub async fn get_vip_rental_by_machine_id(
    pool: &PgPool,
    vip_machine_id: &str,
) -> Result<Option<(String, String)>, VipRentalError> {
    let prefixed_machine_id = format_vip_machine_id(vip_machine_id);
    // Returns (rental_id, user_id) if found
    let row: Option<(String, String)> = sqlx::query_as(
        r#"
        SELECT id, user_id
        FROM secure_cloud_rentals
        WHERE provider_instance_id = $1 AND is_vip = TRUE AND status != 'stopped'
        "#,
    )
    .bind(&prefixed_machine_id)
    .fetch_optional(pool)
    .await?;

    Ok(row)
}

/// Update VIP rental metadata when CSV data changes (not user reassignment)
pub async fn update_vip_rental_metadata(
    pool: &PgPool,
    rental_id: &str,
    vip_machine_id: &str,
    connection: &VipConnectionInfo,
    display: &VipDisplayInfo,
) -> Result<(), VipRentalError> {
    let now = Utc::now();
    let prefixed_machine_id = format_vip_machine_id(vip_machine_id);

    sqlx::query(
        r#"
        UPDATE secure_cloud_rentals
        SET
            ip_address = $1,
            connection_info = $2,
            raw_response = $3,
            instance_type = $4,
            location_code = $5,
            updated_at = $6
        WHERE id = $7 AND provider_instance_id = $8 AND is_vip = TRUE
        "#,
    )
    .bind(&connection.ssh_host)
    .bind(serde_json::json!({
        "ssh_host": connection.ssh_host,
        "ssh_port": connection.ssh_port,
        "ssh_user": connection.ssh_user,
    }))
    .bind(serde_json::json!({
        "notes": display.notes,
        "gpu_type": display.gpu_type,
        "gpu_count": display.gpu_count,
        "vcpu_count": display.vcpu_count,
        "system_memory_gb": display.system_memory_gb,
        "hourly_rate": display.hourly_rate.to_string(),
    }))
    .bind(&display.gpu_type)
    .bind(&display.region)
    .bind(now)
    .bind(rental_id)
    .bind(&prefixed_machine_id)
    .execute(pool)
    .await?;

    tracing::debug!(
        rental_id = %rental_id,
        vip_machine_id = %vip_machine_id,
        "Updated VIP rental metadata"
    );

    Ok(())
}
