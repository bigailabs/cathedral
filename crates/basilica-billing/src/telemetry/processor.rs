use crate::domain::idempotency::generate_idempotency_key;
use crate::domain::types::{RentalId, UsageMetrics};
use crate::error::{BillingError, Result};
use crate::storage::events::{EventType, UsageEvent};
use crate::storage::rds::RdsConnection;
use crate::storage::{RentalRepository, SqlRentalRepository};

use basilica_protocol::billing::TelemetryData;
use chrono::{self, DateTime, Utc};
use rust_decimal::prelude::*;
use serde_json::json;
use std::sync::Arc;
use tracing::{debug, error, warn};
use uuid::Uuid;

pub struct TelemetryProcessor {
    event_store: Arc<crate::domain::events::EventStore>,
    rental_repository: Arc<dyn RentalRepository + Send + Sync>,
    rds_connection: Arc<RdsConnection>,
}

impl TelemetryProcessor {
    pub fn new(rds_connection: Arc<RdsConnection>) -> Self {
        let rental_repository = Arc::new(SqlRentalRepository::new(rds_connection.clone()));

        let event_repository = Arc::new(crate::storage::events::SqlEventRepository::new(
            rds_connection.clone(),
        ));
        let batch_repository = Arc::new(crate::storage::events::SqlBatchRepository::new(
            rds_connection.clone(),
        ));

        Self {
            event_store: Arc::new(crate::domain::events::EventStore::new(
                event_repository,
                batch_repository,
                1000,
                30,
            )),
            rental_repository,
            rds_connection,
        }
    }

    async fn get_last_telemetry_timestamp(
        &self,
        rental_id: &Uuid,
    ) -> Result<Option<DateTime<Utc>>> {
        let pool = self.rds_connection.pool();

        let row = sqlx::query_scalar::<_, DateTime<Utc>>(
            r#"
            SELECT timestamp
            FROM billing.usage_events
            WHERE rental_id = $1 AND event_type = 'telemetry'
            ORDER BY timestamp DESC
            LIMIT 1
            "#,
        )
        .bind(rental_id)
        .fetch_optional(pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "get_last_telemetry_timestamp".to_string(),
            source: Box::new(e),
        })?;

        Ok(row)
    }

    /// Process a single telemetry data point
    pub async fn process_telemetry(&self, data: TelemetryData) -> Result<()> {
        debug!(
            "Processing telemetry for rental {} from node {}",
            data.rental_id, data.node_id
        );

        let rental_id =
            RentalId::from_str(&data.rental_id).map_err(|e| BillingError::ValidationError {
                field: "rental_id".to_string(),
                message: format!("Invalid rental ID: {}", e),
            })?;

        let rental = self
            .rental_repository
            .get_rental(&rental_id)
            .await
            .map_err(|e| {
                warn!("Failed to fetch rental {} for telemetry: {}", rental_id, e);
                BillingError::ValidationError {
                    field: "rental_id".to_string(),
                    message: format!("Failed to fetch rental: {}", e),
                }
            })?
            .ok_or_else(|| {
                warn!("Rental {} not found for telemetry", rental_id);
                BillingError::ValidationError {
                    field: "rental_id".to_string(),
                    message: format!("Rental not found: {}", rental_id),
                }
            })?;

        let telemetry_timestamp = data
            .timestamp
            .as_ref()
            .and_then(|ts| DateTime::<Utc>::from_timestamp(ts.seconds, ts.nanos as u32))
            .unwrap_or_else(chrono::Utc::now);

        let last_timestamp = self
            .get_last_telemetry_timestamp(&rental_id.as_uuid())
            .await?;

        let interval_seconds = match last_timestamp {
            Some(last) => {
                let elapsed = (telemetry_timestamp - last).num_seconds();
                elapsed.clamp(1, 300)
            }
            None => 60,
        };

        debug!(
            "Rental {} telemetry interval: {}s (last: {:?}, current: {})",
            rental_id, interval_seconds, last_timestamp, telemetry_timestamp
        );

        let usage_metrics = if let Some(ref usage) = data.resource_usage {
            let gpu_count = usage.gpu_usage.len() as u32;
            let gpu_hours = Decimal::from(interval_seconds) / Decimal::from(3600);

            UsageMetrics {
                gpu_hours,
                gpu_count,
                cpu_hours: Decimal::from_f64(usage.cpu_percent / 100.0).unwrap_or(Decimal::ZERO),
                memory_gb_hours: Decimal::from(usage.memory_mb) / Decimal::from(1024),
                network_gb: (Decimal::from(usage.network_rx_bytes + usage.network_tx_bytes))
                    / Decimal::from(1_073_741_824u64),
                storage_gb_hours: Decimal::ZERO,
                disk_io_gb: (Decimal::from(usage.disk_read_bytes + usage.disk_write_bytes))
                    / Decimal::from(1_073_741_824u64),
            }
        } else {
            UsageMetrics::zero()
        };

        let (cpu_percent, memory_mb, rx_bytes, tx_bytes, read_bytes, write_bytes) =
            if let Some(u) = data.resource_usage.as_ref() {
                (
                    u.cpu_percent,
                    u.memory_mb,
                    u.network_rx_bytes,
                    u.network_tx_bytes,
                    u.disk_read_bytes,
                    u.disk_write_bytes,
                )
            } else {
                (0.0, 0u64, 0u64, 0u64, 0u64, 0u64)
            };

        let event_data = json!({
            "gpu_hours": usage_metrics.gpu_hours.to_f64(),
            "cpu_percent": cpu_percent,
            "memory_mb": memory_mb,
            "memory_gb": (memory_mb as f64) / 1024.0,
            "network_rx_bytes": rx_bytes,
            "network_tx_bytes": tx_bytes,
            "network_gb": ((rx_bytes + tx_bytes) as f64) / 1_073_741_824.0,
            "disk_read_bytes": read_bytes,
            "disk_write_bytes": write_bytes,
            "disk_io_gb": ((read_bytes + write_bytes) as f64) / 1_073_741_824.0,
            "gpu_metrics": data.resource_usage.as_ref()
                .map(|u| json!({
                    "gpu_count": u.gpu_usage.len(),
                    "utilization": u.gpu_usage.iter().map(|g| g.utilization_percent).collect::<Vec<_>>(),
                    "memory_used": u.gpu_usage.iter().map(|g| g.memory_used_mb).collect::<Vec<_>>(),
                })),
            "custom_metrics": data.custom_metrics,
            "timestamp": telemetry_timestamp.timestamp_millis().to_string(),
        });

        let idempotency_key = generate_idempotency_key(rental_id.as_uuid(), &event_data);

        // Extract validator_id from metadata (for community cloud rentals, None for secure cloud)
        let validator_id = rental.validator_id().map(|s| s.to_string());

        let telemetry_event = UsageEvent {
            event_id: Uuid::new_v4(),
            rental_id: rental_id.as_uuid(),
            user_id: rental.user_id.to_string(),
            node_id: data.node_id.clone(),
            validator_id: validator_id.clone(),
            event_type: EventType::Telemetry,
            event_data,
            timestamp: telemetry_timestamp,
            processed: false,
            processed_at: None,
            batch_id: None,
            idempotency_key: Some(idempotency_key),
        };

        self.event_store
            .append_usage_event(&telemetry_event)
            .await
            .map_err(|e| {
                error!(
                    "Failed to store telemetry event: {} | rental_id={} node_id={} validator_id={:?} event_type={:?} idempotency_key={:?}",
                    e,
                    rental_id,
                    data.node_id,
                    validator_id,
                    telemetry_event.event_type,
                    telemetry_event.idempotency_key
                );
                BillingError::TelemetryError {
                    source: Box::new(e),
                }
            })?;

        Ok(())
    }

    /// Process a batch of telemetry data
    pub async fn process_batch(&self, batch: Vec<TelemetryData>) -> Result<Vec<Result<()>>> {
        let mut results = Vec::with_capacity(batch.len());

        for data in batch {
            results.push(self.process_telemetry(data).await);
        }

        Ok(results)
    }

    /// Get aggregated metrics for a rental
    pub async fn get_rental_metrics(&self, rental_id: &RentalId) -> Result<UsageMetrics> {
        let events = self
            .event_store
            .get_events_by_entity(&rental_id.to_string(), None)
            .await
            .map_err(|e| BillingError::EventStoreError {
                message: format!("Failed to get events for rental {}", rental_id),
                source: Box::new(e),
            })?;

        let mut total_metrics = UsageMetrics::zero();
        let mut max_gpu_count = 0u32;

        for event in events {
            if event.event_type == "telemetry_update" {
                if let Ok(data) = serde_json::from_value::<serde_json::Value>(event.event_data) {
                    total_metrics.cpu_hours +=
                        Decimal::from_f64(data["cpu_percent"].as_f64().unwrap_or(0.0))
                            .unwrap_or(Decimal::ZERO);

                    total_metrics.memory_gb_hours +=
                        Decimal::from_f64(data["memory_gb"].as_f64().unwrap_or(0.0))
                            .unwrap_or(Decimal::ZERO);

                    total_metrics.gpu_hours +=
                        Decimal::from_f64(data["gpu_hours"].as_f64().unwrap_or(0.0))
                            .unwrap_or(Decimal::ZERO);

                    total_metrics.network_gb +=
                        Decimal::from_f64(data["network_gb"].as_f64().unwrap_or(0.0))
                            .unwrap_or(Decimal::ZERO);

                    if let Some(gpu_metrics) = data.get("gpu_metrics") {
                        if let Some(gpu_count) = gpu_metrics["gpu_count"].as_u64() {
                            max_gpu_count = max_gpu_count.max(gpu_count as u32);
                        }
                    }
                }
            }
        }

        total_metrics.gpu_count = max_gpu_count;

        Ok(total_metrics)
    }
}

use std::str::FromStr;
