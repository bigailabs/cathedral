use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

/// Raw row from CSV file
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VipCsvRow {
    pub vip_machine_id: String, // Column A
    pub assigned_user: String,  // Column B (Auth0 user ID)
    pub active: bool,           // Column C (0 = skip row, 1 = process)
    pub ssh_host: String,       // Column D
    pub ssh_port: u16,          // Column E
    pub ssh_user: String,       // Column F
    pub gpu_type: String,       // Column G
    pub gpu_count: u32,         // Column H
    pub region: String,         // Column I
    pub hourly_rate: Decimal,   // Column J
    pub vcpu_count: u32,        // Column K
    pub system_memory_gb: u32,  // Column L
    pub notes: Option<String>,  // Column M (optional)
}

impl VipCsvRow {
    /// Create a test machine row with sensible defaults
    pub fn test_machine(id: &str, user: &str) -> Self {
        Self {
            vip_machine_id: id.to_string(),
            assigned_user: user.to_string(),
            active: true,
            ssh_host: format!("{}.example.com", id),
            ssh_port: 22,
            ssh_user: "ubuntu".to_string(),
            gpu_type: "A100".to_string(),
            gpu_count: 1,
            region: "us-west-2".to_string(),
            hourly_rate: Decimal::new(500, 2), // $5.00
            vcpu_count: 64,
            system_memory_gb: 256,
            notes: None,
        }
    }
}

/// Connection info extracted from CSV row
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
    pub vcpu_count: u32,
    pub system_memory_gb: u32,
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
