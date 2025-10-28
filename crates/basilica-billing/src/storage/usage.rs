use crate::domain::types::{CreditBalance, RentalId, UsageMetrics, UserId};
use crate::error::{BillingError, Result};
use crate::storage::rds::RdsConnection;
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use sqlx::Row;
use std::sync::Arc;

#[async_trait]
pub trait UsageRepository: Send + Sync {
    async fn get_usage_for_rental(&self, rental_id: &RentalId) -> Result<UsageMetrics>;
    async fn get_usage_for_user(
        &self,
        user_id: &UserId,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> Result<UsageMetrics>;
    async fn get_cost_for_period(
        &self,
        user_id: &UserId,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> Result<CreditBalance>;
}

pub struct SqlUsageRepository {
    connection: Arc<RdsConnection>,
}

impl SqlUsageRepository {
    pub fn new(connection: Arc<RdsConnection>) -> Self {
        Self { connection }
    }
}

#[async_trait]
impl UsageRepository for SqlUsageRepository {
    async fn get_usage_for_rental(&self, rental_id: &RentalId) -> Result<UsageMetrics> {
        let row = sqlx::query(
            r#"
            SELECT
                COALESCE(SUM((event_data->>'gpu_hours')::decimal), 0) as gpu_hours,
                COALESCE(MAX((event_data->'gpu_metrics'->>'gpu_count')::int), 1) as gpu_count,
                COALESCE(SUM((event_data->>'cpu_hours')::decimal), 0) as cpu_hours,
                COALESCE(SUM((event_data->>'memory_gb_hours')::decimal), 0) as memory_gb_hours,
                COALESCE(SUM((event_data->>'storage_gb_hours')::decimal), 0) as storage_gb_hours,
                COALESCE(SUM((event_data->>'network_gb')::decimal), 0) as network_gb
            FROM billing.usage_events
            WHERE rental_id = $1 AND event_type = 'telemetry'
            "#,
        )
        .bind(rental_id.as_uuid())
        .fetch_one(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "get_usage_for_rental".to_string(),
            source: Box::new(e),
        })?;

        Ok(UsageMetrics {
            gpu_hours: row.get("gpu_hours"),
            gpu_count: row.try_get::<i32, _>("gpu_count").unwrap_or(1) as u32,
            cpu_hours: row.get("cpu_hours"),
            memory_gb_hours: row.get("memory_gb_hours"),
            storage_gb_hours: row.get("storage_gb_hours"),
            network_gb: row.get("network_gb"),
            disk_io_gb: Decimal::ZERO,
        })
    }

    async fn get_usage_for_user(
        &self,
        user_id: &UserId,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> Result<UsageMetrics> {
        let row = sqlx::query(
            r#"
            SELECT
                COALESCE(SUM((ue.event_data->>'gpu_hours')::decimal), 0) as gpu_hours,
                COALESCE(MAX((ue.event_data->'gpu_metrics'->>'gpu_count')::int), 1) as gpu_count,
                COALESCE(SUM((ue.event_data->>'cpu_hours')::decimal), 0) as cpu_hours,
                COALESCE(SUM((ue.event_data->>'memory_gb_hours')::decimal), 0) as memory_gb_hours,
                COALESCE(SUM((ue.event_data->>'storage_gb_hours')::decimal), 0) as storage_gb_hours,
                COALESCE(SUM((ue.event_data->>'network_gb')::decimal), 0) as network_gb
            FROM billing.usage_events ue
            JOIN billing.active_rentals_facts ar ON ue.rental_id = ar.rental_id
            WHERE ar.user_id = $1
                AND ue.timestamp >= $2
                AND ue.timestamp <= $3
                AND ue.event_type = 'telemetry'
            "#,
        )
        .bind(user_id.as_str())
        .bind(start)
        .bind(end)
        .fetch_one(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "get_usage_for_user".to_string(),
            source: Box::new(e),
        })?;

        Ok(UsageMetrics {
            gpu_hours: row.get("gpu_hours"),
            gpu_count: row.try_get::<i32, _>("gpu_count").unwrap_or(1) as u32,
            cpu_hours: row.get("cpu_hours"),
            memory_gb_hours: row.get("memory_gb_hours"),
            storage_gb_hours: row.get("storage_gb_hours"),
            network_gb: row.get("network_gb"),
            disk_io_gb: Decimal::ZERO,
        })
    }

    async fn get_cost_for_period(
        &self,
        user_id: &UserId,
        start: DateTime<Utc>,
        end: DateTime<Utc>,
    ) -> Result<CreditBalance> {
        let row = sqlx::query(
            r#"
            SELECT COALESCE(SUM((cost_breakdown->>'total_cost')::decimal), 0) as total_cost
            FROM billing.active_rentals_facts
            WHERE user_id = $1
                AND started_at >= $2
                AND (ended_at <= $3 OR (ended_at IS NULL AND $3 >= NOW()))
            "#,
        )
        .bind(user_id.as_str())
        .bind(start)
        .bind(end)
        .fetch_one(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "get_cost_for_period".to_string(),
            source: Box::new(e),
        })?;

        Ok(CreditBalance::from_decimal(row.get("total_cost")))
    }
}
