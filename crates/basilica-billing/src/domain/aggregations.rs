use crate::error::{BillingError, Result};
use crate::metrics::BillingMetricsSystem;
use crate::storage::events::SqlEventRepository;
use crate::storage::rds::RdsConnection;
use chrono::{Datelike, Duration, Timelike, Utc};
use std::sync::Arc;
use tokio::time::interval;
use tracing::{error, info};

pub struct AggregationJobs {
    rds_connection: Arc<RdsConnection>,
    event_repository: Arc<SqlEventRepository>,
    metrics: Option<Arc<BillingMetricsSystem>>,
    retention_days: u32,
}

impl AggregationJobs {
    pub fn new(
        rds_connection: Arc<RdsConnection>,
        event_repository: Arc<SqlEventRepository>,
        metrics: Option<Arc<BillingMetricsSystem>>,
        retention_days: u32,
    ) -> Self {
        Self {
            rds_connection,
            event_repository,
            metrics,
            retention_days,
        }
    }

    pub async fn start_hourly_aggregation(&self, interval_seconds: u64) {
        let jobs = self.clone();
        tokio::spawn(async move {
            let mut ticker = interval(std::time::Duration::from_secs(interval_seconds));
            ticker.tick().await; // Skip first immediate tick

            info!(
                "Starting hourly aggregation job with interval {}s",
                interval_seconds
            );

            loop {
                ticker.tick().await;

                let timer = jobs
                    .metrics
                    .as_ref()
                    .map(|m| m.billing_metrics().start_aggregation_timer("hourly"));

                match jobs.run_hourly_aggregation().await {
                    Ok(events_processed) => {
                        info!(
                            "Hourly aggregation completed: {} events processed",
                            events_processed
                        );

                        if let Some(ref metrics) = jobs.metrics {
                            if let Some(timer) = timer {
                                metrics
                                    .billing_metrics()
                                    .record_aggregation_complete(
                                        timer,
                                        "hourly",
                                        true,
                                        events_processed,
                                    )
                                    .await;
                            }
                        }
                    }
                    Err(e) => {
                        error!("Hourly aggregation failed: {}", e);

                        if let Some(ref metrics) = jobs.metrics {
                            if let Some(timer) = timer {
                                metrics
                                    .billing_metrics()
                                    .record_aggregation_complete(timer, "hourly", false, 0)
                                    .await;
                            }
                        }
                    }
                }
            }
        });
    }

    pub async fn start_daily_aggregation(&self, interval_seconds: u64) {
        let jobs = self.clone();
        tokio::spawn(async move {
            let mut ticker = interval(std::time::Duration::from_secs(interval_seconds));
            ticker.tick().await;

            info!(
                "Starting daily aggregation job with interval {}s",
                interval_seconds
            );

            loop {
                ticker.tick().await;

                let timer = jobs
                    .metrics
                    .as_ref()
                    .map(|m| m.billing_metrics().start_aggregation_timer("daily"));

                match jobs.run_daily_aggregation().await {
                    Ok(days_processed) => {
                        info!(
                            "Daily aggregation completed: {} days processed",
                            days_processed
                        );

                        if let Some(ref metrics) = jobs.metrics {
                            if let Some(timer) = timer {
                                metrics
                                    .billing_metrics()
                                    .record_aggregation_complete(
                                        timer,
                                        "daily",
                                        true,
                                        days_processed,
                                    )
                                    .await;
                            }
                        }
                    }
                    Err(e) => {
                        error!("Daily aggregation failed: {}", e);

                        if let Some(ref metrics) = jobs.metrics {
                            if let Some(timer) = timer {
                                metrics
                                    .billing_metrics()
                                    .record_aggregation_complete(timer, "daily", false, 0)
                                    .await;
                            }
                        }
                    }
                }
            }
        });
    }

    pub async fn start_monthly_aggregation(&self, interval_seconds: u64) {
        let jobs = self.clone();
        tokio::spawn(async move {
            let mut ticker = interval(std::time::Duration::from_secs(interval_seconds));
            ticker.tick().await;

            info!(
                "Starting monthly aggregation job with interval {}s",
                interval_seconds
            );

            loop {
                ticker.tick().await;

                let timer = jobs
                    .metrics
                    .as_ref()
                    .map(|m| m.billing_metrics().start_aggregation_timer("monthly"));

                match jobs.run_monthly_aggregation().await {
                    Ok(months_processed) => {
                        info!(
                            "Monthly aggregation completed: {} months processed",
                            months_processed
                        );

                        if let Some(ref metrics) = jobs.metrics {
                            if let Some(timer) = timer {
                                metrics
                                    .billing_metrics()
                                    .record_aggregation_complete(
                                        timer,
                                        "monthly",
                                        true,
                                        months_processed,
                                    )
                                    .await;
                            }
                        }
                    }
                    Err(e) => {
                        error!("Monthly aggregation failed: {}", e);

                        if let Some(ref metrics) = jobs.metrics {
                            if let Some(timer) = timer {
                                metrics
                                    .billing_metrics()
                                    .record_aggregation_complete(timer, "monthly", false, 0)
                                    .await;
                            }
                        }
                    }
                }
            }
        });
    }

    pub async fn start_rental_sync(&self, interval_seconds: u64) {
        let jobs = self.clone();
        tokio::spawn(async move {
            let mut ticker = interval(std::time::Duration::from_secs(interval_seconds));
            ticker.tick().await;

            info!(
                "Starting rental sync job with interval {}s",
                interval_seconds
            );

            loop {
                ticker.tick().await;

                let timer = jobs
                    .metrics
                    .as_ref()
                    .map(|m| m.billing_metrics().start_aggregation_timer("rental_sync"));

                match jobs.run_rental_sync().await {
                    Ok(rentals_synced) => {
                        info!("Rental sync completed: {} rentals synced", rentals_synced);

                        if let Some(ref metrics) = jobs.metrics {
                            if let Some(timer) = timer {
                                metrics
                                    .billing_metrics()
                                    .record_aggregation_complete(
                                        timer,
                                        "rental_sync",
                                        true,
                                        rentals_synced,
                                    )
                                    .await;
                            }
                        }
                    }
                    Err(e) => {
                        error!("Rental sync failed: {}", e);

                        if let Some(ref metrics) = jobs.metrics {
                            if let Some(timer) = timer {
                                metrics
                                    .billing_metrics()
                                    .record_aggregation_complete(timer, "rental_sync", false, 0)
                                    .await;
                            }
                        }
                    }
                }
            }
        });
    }

    pub async fn start_cleanup_job(&self, interval_seconds: u64) {
        let jobs = self.clone();
        tokio::spawn(async move {
            let mut ticker = interval(std::time::Duration::from_secs(interval_seconds));
            ticker.tick().await;

            info!("Starting cleanup job with interval {}s", interval_seconds);

            loop {
                ticker.tick().await;

                let timer = jobs
                    .metrics
                    .as_ref()
                    .map(|m| m.billing_metrics().start_aggregation_timer("cleanup"));

                match jobs.run_cleanup().await {
                    Ok(events_deleted) => {
                        info!("Cleanup completed: {} events deleted", events_deleted);

                        if let Some(ref metrics) = jobs.metrics {
                            if let Some(timer) = timer {
                                metrics
                                    .billing_metrics()
                                    .record_aggregation_complete(
                                        timer,
                                        "cleanup",
                                        true,
                                        events_deleted,
                                    )
                                    .await;
                            }
                        }
                    }
                    Err(e) => {
                        error!("Cleanup failed: {}", e);

                        if let Some(ref metrics) = jobs.metrics {
                            if let Some(timer) = timer {
                                metrics
                                    .billing_metrics()
                                    .record_aggregation_complete(timer, "cleanup", false, 0)
                                    .await;
                            }
                        }
                    }
                }
            }
        });
    }

    async fn run_hourly_aggregation(&self) -> Result<u64> {
        let now = Utc::now();
        let last_hour = now - Duration::hours(1);

        let hour_start = last_hour
            .with_minute(0)
            .and_then(|t| t.with_second(0))
            .and_then(|t| t.with_nanosecond(0))
            .ok_or_else(|| BillingError::InvalidState {
                message: "Failed to calculate hour start".to_string(),
            })?;

        let hour_end = hour_start + Duration::hours(1);

        info!(
            "Running hourly aggregation for period: {} to {}",
            hour_start, hour_end
        );

        let pool = self.rds_connection.pool();

        let result = sqlx::query(
            r#"
            INSERT INTO billing.usage_aggregations_hourly
                (rental_id, user_id, hour_start, hour_key, date_key,
                 cpu_usage_avg, cpu_usage_max, memory_usage_avg_gb, memory_usage_max_gb,
                 gpu_usage_avg, gpu_usage_max, network_ingress_gb, network_egress_gb,
                 disk_read_gb, disk_write_gb, cost_for_period, data_points_count, created_at)
            SELECT
                rental_id,
                user_id,
                $1 as hour_start,
                EXTRACT(HOUR FROM $1) as hour_key,
                EXTRACT(EPOCH FROM DATE_TRUNC('day', $1))::int as date_key,
                AVG(cpu_usage_avg) as cpu_usage_avg,
                MAX(cpu_usage_avg) as cpu_usage_max,
                AVG(memory_usage_avg_gb) as memory_usage_avg_gb,
                MAX(memory_usage_avg_gb) as memory_usage_max_gb,
                AVG(gpu_usage_avg) as gpu_usage_avg,
                MAX(gpu_usage_avg) as gpu_usage_max,
                SUM(network_ingress_gb) as network_ingress_gb,
                SUM(network_egress_gb) as network_egress_gb,
                SUM(disk_read_gb) as disk_read_gb,
                SUM(disk_write_gb) as disk_write_gb,
                SUM(cost_for_period) as cost_for_period,
                SUM(data_points_count) as data_points_count,
                NOW() as created_at
            FROM billing.usage_aggregations
            WHERE created_at >= $1 AND created_at < $2
            GROUP BY rental_id, user_id
            ON CONFLICT (rental_id, hour_start) DO UPDATE SET
                cpu_usage_avg = EXCLUDED.cpu_usage_avg,
                cpu_usage_max = EXCLUDED.cpu_usage_max,
                memory_usage_avg_gb = EXCLUDED.memory_usage_avg_gb,
                memory_usage_max_gb = EXCLUDED.memory_usage_max_gb,
                gpu_usage_avg = EXCLUDED.gpu_usage_avg,
                gpu_usage_max = EXCLUDED.gpu_usage_max,
                network_ingress_gb = EXCLUDED.network_ingress_gb,
                network_egress_gb = EXCLUDED.network_egress_gb,
                disk_read_gb = EXCLUDED.disk_read_gb,
                disk_write_gb = EXCLUDED.disk_write_gb,
                cost_for_period = EXCLUDED.cost_for_period,
                data_points_count = EXCLUDED.data_points_count,
                updated_at = NOW()
            "#,
        )
        .bind(hour_start)
        .bind(hour_end)
        .execute(pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "hourly_aggregation".to_string(),
            source: Box::new(e),
        })?;

        Ok(result.rows_affected())
    }

    async fn run_daily_aggregation(&self) -> Result<u64> {
        let now = Utc::now();
        let yesterday = now - Duration::days(1);

        let day_start = yesterday
            .with_hour(0)
            .and_then(|t| t.with_minute(0))
            .and_then(|t| t.with_second(0))
            .and_then(|t| t.with_nanosecond(0))
            .ok_or_else(|| BillingError::InvalidState {
                message: "Failed to calculate day start".to_string(),
            })?;

        let day_end = day_start + Duration::days(1);

        info!(
            "Running daily aggregation for period: {} to {}",
            day_start, day_end
        );

        let pool = self.rds_connection.pool();

        let result = sqlx::query(
            r#"
            INSERT INTO billing.usage_aggregations_daily
                (rental_id, user_id, day_start, year, month, day,
                 cpu_usage_avg, cpu_usage_max, memory_usage_avg_gb, memory_usage_max_gb,
                 gpu_usage_avg, gpu_usage_max, network_ingress_gb, network_egress_gb,
                 disk_read_gb, disk_write_gb, cost_for_day, hours_count, created_at)
            SELECT
                rental_id,
                user_id,
                $1 as day_start,
                EXTRACT(YEAR FROM $1) as year,
                EXTRACT(MONTH FROM $1) as month,
                EXTRACT(DAY FROM $1) as day,
                AVG(cpu_usage_avg) as cpu_usage_avg,
                MAX(cpu_usage_max) as cpu_usage_max,
                AVG(memory_usage_avg_gb) as memory_usage_avg_gb,
                MAX(memory_usage_max_gb) as memory_usage_max_gb,
                AVG(gpu_usage_avg) as gpu_usage_avg,
                MAX(gpu_usage_max) as gpu_usage_max,
                SUM(network_ingress_gb) as network_ingress_gb,
                SUM(network_egress_gb) as network_egress_gb,
                SUM(disk_read_gb) as disk_read_gb,
                SUM(disk_write_gb) as disk_write_gb,
                SUM(cost_for_period) as cost_for_day,
                COUNT(*) as hours_count,
                NOW() as created_at
            FROM billing.usage_aggregations_hourly
            WHERE hour_start >= $1 AND hour_start < $2
            GROUP BY rental_id, user_id
            ON CONFLICT (rental_id, day_start) DO UPDATE SET
                cpu_usage_avg = EXCLUDED.cpu_usage_avg,
                cpu_usage_max = EXCLUDED.cpu_usage_max,
                memory_usage_avg_gb = EXCLUDED.memory_usage_avg_gb,
                memory_usage_max_gb = EXCLUDED.memory_usage_max_gb,
                gpu_usage_avg = EXCLUDED.gpu_usage_avg,
                gpu_usage_max = EXCLUDED.gpu_usage_max,
                network_ingress_gb = EXCLUDED.network_ingress_gb,
                network_egress_gb = EXCLUDED.network_egress_gb,
                disk_read_gb = EXCLUDED.disk_read_gb,
                disk_write_gb = EXCLUDED.disk_write_gb,
                cost_for_day = EXCLUDED.cost_for_day,
                hours_count = EXCLUDED.hours_count,
                updated_at = NOW()
            "#,
        )
        .bind(day_start)
        .bind(day_end)
        .execute(pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "daily_aggregation".to_string(),
            source: Box::new(e),
        })?;

        Ok(result.rows_affected())
    }

    async fn run_monthly_aggregation(&self) -> Result<u64> {
        let now = Utc::now();
        let last_month = now - Duration::days(30);

        let month_start = last_month
            .with_day(1)
            .and_then(|t| t.with_hour(0))
            .and_then(|t| t.with_minute(0))
            .and_then(|t| t.with_second(0))
            .and_then(|t| t.with_nanosecond(0))
            .ok_or_else(|| BillingError::InvalidState {
                message: "Failed to calculate month start".to_string(),
            })?;

        let month_end = if month_start.month() == 12 {
            month_start
                .with_year(month_start.year() + 1)
                .and_then(|t| t.with_month(1))
        } else {
            month_start.with_month(month_start.month() + 1)
        }
        .ok_or_else(|| BillingError::InvalidState {
            message: "Failed to calculate month end".to_string(),
        })?;

        info!(
            "Running monthly aggregation for period: {} to {}",
            month_start, month_end
        );

        let pool = self.rds_connection.pool();

        let result = sqlx::query(
            r#"
            INSERT INTO billing.usage_aggregations_monthly
                (rental_id, user_id, month_start, year, month,
                 cpu_usage_avg, cpu_usage_max, memory_usage_avg_gb, memory_usage_max_gb,
                 gpu_usage_avg, gpu_usage_max, network_ingress_gb, network_egress_gb,
                 disk_read_gb, disk_write_gb, cost_for_month, days_count, created_at)
            SELECT
                rental_id,
                user_id,
                $1 as month_start,
                EXTRACT(YEAR FROM $1) as year,
                EXTRACT(MONTH FROM $1) as month,
                AVG(cpu_usage_avg) as cpu_usage_avg,
                MAX(cpu_usage_max) as cpu_usage_max,
                AVG(memory_usage_avg_gb) as memory_usage_avg_gb,
                MAX(memory_usage_max_gb) as memory_usage_max_gb,
                AVG(gpu_usage_avg) as gpu_usage_avg,
                MAX(gpu_usage_max) as gpu_usage_max,
                SUM(network_ingress_gb) as network_ingress_gb,
                SUM(network_egress_gb) as network_egress_gb,
                SUM(disk_read_gb) as disk_read_gb,
                SUM(disk_write_gb) as disk_write_gb,
                SUM(cost_for_day) as cost_for_month,
                COUNT(*) as days_count,
                NOW() as created_at
            FROM billing.usage_aggregations_daily
            WHERE day_start >= $1 AND day_start < $2
            GROUP BY rental_id, user_id
            ON CONFLICT (rental_id, month_start) DO UPDATE SET
                cpu_usage_avg = EXCLUDED.cpu_usage_avg,
                cpu_usage_max = EXCLUDED.cpu_usage_max,
                memory_usage_avg_gb = EXCLUDED.memory_usage_avg_gb,
                memory_usage_max_gb = EXCLUDED.memory_usage_max_gb,
                gpu_usage_avg = EXCLUDED.gpu_usage_avg,
                gpu_usage_max = EXCLUDED.gpu_usage_max,
                network_ingress_gb = EXCLUDED.network_ingress_gb,
                network_egress_gb = EXCLUDED.network_egress_gb,
                disk_read_gb = EXCLUDED.disk_read_gb,
                disk_write_gb = EXCLUDED.disk_write_gb,
                cost_for_month = EXCLUDED.cost_for_month,
                days_count = EXCLUDED.days_count,
                updated_at = NOW()
            "#,
        )
        .bind(month_start)
        .bind(month_end)
        .execute(pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "monthly_aggregation".to_string(),
            source: Box::new(e),
        })?;

        Ok(result.rows_affected())
    }

    async fn run_rental_sync(&self) -> Result<u64> {
        info!("Running rental sync to update costs from aggregations");

        let pool = self.rds_connection.pool();

        let result = sqlx::query(
            r#"
            UPDATE billing.rentals r
            SET
                total_cost = COALESCE(agg.total_cost, r.total_cost),
                updated_at = NOW()
            FROM (
                SELECT
                    rental_id,
                    SUM(cost_for_period) as total_cost
                FROM billing.usage_aggregation_facts
                GROUP BY rental_id
            ) agg
            WHERE r.rental_id = agg.rental_id
              AND r.status IN ('active', 'completed')
              AND (r.total_cost IS NULL OR r.total_cost < agg.total_cost)
            "#,
        )
        .execute(pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "rental_sync".to_string(),
            source: Box::new(e),
        })?;

        Ok(result.rows_affected())
    }

    async fn run_cleanup(&self) -> Result<u64> {
        let cutoff_date = Utc::now() - Duration::days(self.retention_days as i64);

        info!(
            "Running cleanup for events older than {} (retention: {} days)",
            cutoff_date, self.retention_days
        );

        let pool = self.rds_connection.pool();

        let result = sqlx::query(
            r#"
            DELETE FROM billing.usage_events
            WHERE processed = true
              AND processed_at < $1
            "#,
        )
        .bind(cutoff_date)
        .execute(pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "cleanup_old_events".to_string(),
            source: Box::new(e),
        })?;

        let deleted_count = result.rows_affected();

        if deleted_count > 0 {
            info!(
                "Deleted {} processed events older than {}",
                deleted_count, cutoff_date
            );
        }

        Ok(deleted_count)
    }
}

impl Clone for AggregationJobs {
    fn clone(&self) -> Self {
        Self {
            rds_connection: self.rds_connection.clone(),
            event_repository: self.event_repository.clone(),
            metrics: self.metrics.clone(),
            retention_days: self.retention_days,
        }
    }
}
