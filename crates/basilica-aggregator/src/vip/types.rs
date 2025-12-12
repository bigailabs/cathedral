use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

/// Raw row from Google Sheet (fixed column positions A-K)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VipSheetRow {
    pub vip_machine_id: String,      // Column A
    pub assigned_user: String,        // Column B (Auth0 user ID)
    pub ready: String,                // Column C
    pub ssh_host: String,             // Column D
    pub ssh_port: u16,                // Column E
    pub ssh_user: String,             // Column F
    pub gpu_type: String,             // Column G
    pub gpu_count: u32,               // Column H
    pub region: String,               // Column I
    pub hourly_rate: Decimal,         // Column J
    pub notes: Option<String>,        // Column K (optional)
}

/// Connection info extracted from sheet row
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VipConnectionInfo {
    pub ssh_host: String,
    pub ssh_port: u16,
    pub ssh_user: String,
}

/// Display info for `basilica ps`
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VipDisplayInfo {
    pub gpu_type: String,
    pub gpu_count: u32,
    pub region: String,
    pub hourly_rate: Decimal,
    pub notes: Option<String>,
}

/// Validated VIP machine ready for rental creation
#[derive(Debug, Clone)]
pub struct ValidVipMachine {
    pub vip_machine_id: String,
    pub assigned_user: String,
    pub connection: VipConnectionInfo,
    pub display: VipDisplayInfo,
}

/// Cache entry for an active VIP rental
#[derive(Debug, Clone)]
pub struct VipRentalRecord {
    pub vip_machine_id: String,
    pub assigned_user: String,
    pub secure_cloud_rental_id: String,
    pub connection: VipConnectionInfo,
    pub display: VipDisplayInfo,
    pub last_seen_at: DateTime<Utc>,
}
