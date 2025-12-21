use crate::models::extract_vip_machine_id;
use crate::vip::types::{VipConnectionInfo, VipDisplayInfo, VipRentalRecord};
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use sqlx::PgPool;
use std::collections::{HashMap, HashSet};
use tokio::sync::RwLock;

/// In-memory cache for VIP rental records, keyed by vip_machine_id
pub struct VipCache {
    entries: RwLock<HashMap<String, VipRentalRecord>>,
}

impl VipCache {
    /// Create a new empty cache
    pub fn new() -> Self {
        Self {
            entries: RwLock::new(HashMap::new()),
        }
    }

    /// Get a VIP rental record by vip_machine_id
    pub async fn get(&self, vip_machine_id: &str) -> Option<VipRentalRecord> {
        let entries = self.entries.read().await;
        entries.get(vip_machine_id).cloned()
    }

    /// Check if a vip_machine_id exists in the cache
    pub async fn contains(&self, vip_machine_id: &str) -> bool {
        let entries = self.entries.read().await;
        entries.contains_key(vip_machine_id)
    }

    /// Insert or update a VIP rental record
    pub async fn insert(&self, record: VipRentalRecord) {
        let mut entries = self.entries.write().await;
        entries.insert(record.vip_machine_id.clone(), record);
    }

    /// Remove a VIP rental record by vip_machine_id
    pub async fn remove(&self, vip_machine_id: &str) -> Option<VipRentalRecord> {
        let mut entries = self.entries.write().await;
        entries.remove(vip_machine_id)
    }

    /// List all VIP rental records
    pub async fn list_all(&self) -> Vec<VipRentalRecord> {
        let entries = self.entries.read().await;
        entries.values().cloned().collect()
    }

    /// Get all vip_machine_ids that are NOT in the provided set
    /// Used to find cached entries that were removed from CSV
    pub async fn get_ids_not_in(&self, seen_ids: &HashSet<String>) -> Vec<String> {
        let entries = self.entries.read().await;
        entries
            .keys()
            .filter(|id| !seen_ids.contains(*id))
            .cloned()
            .collect()
    }

    /// Get the count of cached entries
    pub async fn len(&self) -> usize {
        let entries = self.entries.read().await;
        entries.len()
    }

    /// Check if cache is empty
    pub async fn is_empty(&self) -> bool {
        let entries = self.entries.read().await;
        entries.is_empty()
    }

    /// Clear all entries from the cache
    pub async fn clear(&self) {
        let mut entries = self.entries.write().await;
        entries.clear();
    }

    /// Rebuild the cache from existing VIP rentals in the database
    /// Called on startup to restore state after a restart
    pub async fn rebuild_from_db(&self, pool: &PgPool) -> Result<usize, sqlx::Error> {
        // Query all active VIP rentals
        // VIP machine ID is stored in provider_instance_id with 'vip:' prefix
        let rows: Vec<VipRentalDbRow> = sqlx::query_as(
            r#"
            SELECT
                id, user_id, provider_instance_id, ip_address,
                connection_info, raw_response, instance_type,
                location_code, created_at
            FROM secure_cloud_rentals
            WHERE is_vip = TRUE AND status != 'stopped' AND provider_instance_id LIKE 'vip:%'
            "#,
        )
        .fetch_all(pool)
        .await?;

        let count = rows.len();

        // Convert to VipRentalRecord and insert into cache
        let mut entries = self.entries.write().await;
        entries.clear(); // Clear any stale data

        for row in rows {
            // Extract vip_machine_id from provider_instance_id (remove 'vip:' prefix)
            if let Some(vip_machine_id) = row
                .provider_instance_id
                .as_ref()
                .and_then(|id| extract_vip_machine_id(id))
            {
                let vip_machine_id = vip_machine_id.to_string();

                // Parse connection_info JSON
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

                // Parse raw_response JSON for display info
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
                        .and_then(|v| v.as_str())
                        .and_then(|s| s.parse::<Decimal>().ok())
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

                let record = VipRentalRecord {
                    vip_machine_id: vip_machine_id.clone(),
                    assigned_user: row.user_id,
                    secure_cloud_rental_id: row.id,
                    connection,
                    display,
                    last_seen_at: Utc::now(), // Use current time since we're rebuilding
                };

                entries.insert(vip_machine_id, record);
            }
        }

        tracing::info!(count = count, "Rebuilt VIP cache from database");

        Ok(count)
    }
}

/// Database row structure for VIP rental query
#[derive(sqlx::FromRow)]
struct VipRentalDbRow {
    id: String,
    user_id: String,
    provider_instance_id: Option<String>,
    ip_address: Option<String>,
    connection_info: Option<serde_json::Value>,
    raw_response: Option<serde_json::Value>,
    instance_type: Option<String>,
    location_code: Option<String>,
    #[allow(dead_code)]
    created_at: DateTime<Utc>,
}

impl Default for VipCache {
    fn default() -> Self {
        Self::new()
    }
}
