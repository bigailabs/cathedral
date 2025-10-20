use crate::config::BillingConfig;
use crate::grpc::BillingServiceImpl;
use crate::metrics::BillingMetricsSystem;
use crate::pricing::cache::PriceCache;
use crate::pricing::providers::create_providers;
use crate::pricing::service::PricingService;
use crate::storage::rds::RdsConnection;
use crate::telemetry::{TelemetryIngester, TelemetryProcessor};

use axum::{http::StatusCode, response::Json, routing::get, Router};
use basilica_protocol::billing::billing_service_server::BillingServiceServer;
use chrono;
use serde_json::Value;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::sync::mpsc;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::Server;
use tower::ServiceBuilder;
use tower_http::cors::CorsLayer;
use tracing::{error, info, warn};

/// Billing server that hosts the gRPC service
pub struct BillingServer {
    config: BillingConfig,
    rds_connection: Arc<RdsConnection>,
    metrics: Option<Arc<BillingMetricsSystem>>,
}

impl BillingServer {
    pub fn new(rds_connection: Arc<RdsConnection>) -> Self {
        Self {
            config: BillingConfig::default(),
            rds_connection,
            metrics: None,
        }
    }

    pub async fn new_with_config(
        config: BillingConfig,
        metrics: Option<Arc<BillingMetricsSystem>>,
    ) -> anyhow::Result<Self> {
        // Only load AWS config if we're actually using AWS services
        let rds_connection = if config.aws.secrets_manager_enabled
            && config.aws.secret_name.is_some()
        {
            let aws_config = aws_config::load_defaults(aws_config::BehaviorVersion::latest()).await;
            let secret_name = config.aws.secret_name.as_deref();
            Arc::new(
                RdsConnection::new(config.database.clone(), &aws_config, secret_name)
                    .await
                    .map_err(|e| anyhow::anyhow!("Failed to connect to RDS: {}", e))?,
            )
        } else {
            // Use direct database connection without AWS
            Arc::new(
                RdsConnection::new_direct(config.database.clone())
                    .await
                    .map_err(|e| anyhow::anyhow!("Failed to connect to database: {}", e))?,
            )
        };

        Ok(Self {
            config,
            rds_connection,
            metrics,
        })
    }

    pub async fn run_migrations(&self) -> anyhow::Result<()> {
        info!("Running database migrations");

        let pool = self.rds_connection.pool();

        match sqlx::migrate!("./migrations").run(pool).await {
            Ok(_) => {
                info!("Database migrations completed successfully");
                Ok(())
            }
            Err(e) => {
                error!("Failed to run database migrations: {}", e);
                Err(anyhow::anyhow!("Migration failed: {}", e))
            }
        }
    }

    pub async fn run_with_listener(
        self,
        listener: tokio::net::TcpListener,
        shutdown_signal: tokio::sync::oneshot::Receiver<()>,
    ) -> anyhow::Result<()> {
        let addr = listener.local_addr()?;
        info!("Starting billing gRPC server on {}", addr);

        let buffer_size = self.config.telemetry.ingest_buffer_size.unwrap_or(10000);
        let (telemetry_ingester, telemetry_receiver) = TelemetryIngester::new(buffer_size);
        let telemetry_ingester = Arc::new(telemetry_ingester);
        let telemetry_processor = Arc::new(TelemetryProcessor::new(self.rds_connection.clone()));

        let billing_service = BillingServiceImpl::new(
            self.rds_connection.clone(),
            telemetry_ingester.clone(),
            telemetry_processor.clone(),
            self.metrics.clone(),
        )
        .await?;

        let processor = telemetry_processor.clone();
        let telemetry_handle = tokio::spawn(async move {
            Self::telemetry_consumer_loop(telemetry_receiver, processor).await;
        });

        use crate::domain::billing_handlers::BillingEventHandlers;
        use crate::domain::processor::EventProcessor;
        use crate::storage::{SqlCreditRepository, SqlPackageRepository, SqlRentalRepository};

        let event_repository = Arc::new(crate::storage::events::SqlEventRepository::new(
            self.rds_connection.clone(),
        ));
        let batch_repository = Arc::new(crate::storage::events::SqlBatchRepository::new(
            self.rds_connection.clone(),
        ));
        let event_store = Arc::new(crate::domain::events::EventStore::new(
            event_repository.clone(),
            batch_repository,
            1000,
            90,
        ));

        let rental_repository = Arc::new(SqlRentalRepository::new(self.rds_connection.clone()));
        let credit_repository = Arc::new(SqlCreditRepository::new(self.rds_connection.clone()));
        let package_repository = Arc::new(SqlPackageRepository::new(
            self.rds_connection.pool().clone(),
        ));
        package_repository
            .initialize()
            .await
            .map_err(|e| anyhow::anyhow!("Failed to initialize package repository: {}", e))?;

        let billing_handlers: Arc<dyn crate::domain::processor::EventHandlers + Send + Sync> =
            Arc::new(BillingEventHandlers::new(
                rental_repository,
                credit_repository,
                package_repository,
                event_repository.clone(),
            ));

        let batch_size = Some(self.config.aggregator.batch_size as i64);
        let processing_interval = self.config.processing_interval();

        let event_processor = Arc::new(EventProcessor::new(
            event_store,
            billing_handlers,
            batch_size,
            processing_interval,
            self.metrics.clone(),
        ));

        event_processor
            .start()
            .await
            .map_err(|e| anyhow::anyhow!("Failed to start event processor: {}", e))?;

        info!("Event processor started successfully");

        use crate::domain::aggregations::AggregationJobs;

        let aggregation_jobs = AggregationJobs::new(
            self.rds_connection.clone(),
            event_repository.clone(),
            self.metrics.clone(),
            self.config.aggregator.retention_days,
        );

        aggregation_jobs.start_hourly_aggregation(3600).await; // Run every hour
        aggregation_jobs.start_daily_aggregation(86400).await; // Run every day
        aggregation_jobs.start_monthly_aggregation(86400).await; // Run every day (checks for new month)
        aggregation_jobs
            .start_rental_sync(self.config.aggregator.processing_interval_seconds)
            .await;
        aggregation_jobs
            .start_cleanup_job(self.config.aggregator.batch_timeout_seconds)
            .await;

        info!("Aggregation jobs started successfully");

        // Start price sync job if dynamic pricing is enabled
        Self::start_price_sync_job(self.rds_connection.clone(), self.config.clone()).await;

        let mut server_builder = Server::builder();

        server_builder = server_builder
            .concurrency_limit_per_connection(
                self.config.grpc.max_concurrent_requests.unwrap_or(1000),
            )
            .timeout(std::time::Duration::from_secs(
                self.config.grpc.request_timeout_seconds.unwrap_or(60),
            ))
            .initial_stream_window_size(65536)
            .initial_connection_window_size(65536)
            .max_concurrent_streams(self.config.grpc.max_concurrent_streams);

        let mut router = server_builder.add_service(BillingServiceServer::new(billing_service));

        let (mut health_reporter, health_service) = tonic_health::server::health_reporter();
        health_reporter
            .set_serving::<BillingServiceServer<BillingServiceImpl>>()
            .await;
        router = router.add_service(health_service);

        let incoming = TcpListenerStream::new(listener);

        info!("gRPC server listening for shutdown signal");
        router
            .serve_with_incoming_shutdown(incoming, async {
                let _ = shutdown_signal.await;
            })
            .await
            .map_err(|e| anyhow::anyhow!("gRPC server error: {}", e))?;

        info!("Stopping event processor");
        if let Err(e) = event_processor.stop().await {
            error!("Error stopping event processor: {}", e);
        }

        info!("Stopping telemetry consumer task");
        telemetry_handle.abort();
        let _ = telemetry_handle.await;

        self.shutdown().await?;

        Ok(())
    }

    pub async fn serve(
        self,
        shutdown_signal: impl std::future::Future<Output = ()> + Send + 'static,
    ) -> anyhow::Result<()> {
        let grpc_addr: SocketAddr = format!(
            "{}:{}",
            self.config.grpc.listen_address, self.config.grpc.port
        )
        .parse()
        .map_err(|e| anyhow::anyhow!("Invalid gRPC server address: {}", e))?;

        let http_addr: SocketAddr = format!(
            "{}:{}",
            self.config.http.listen_address, self.config.http.port
        )
        .parse()
        .map_err(|e| anyhow::anyhow!("Invalid HTTP server address: {}", e))?;

        let grpc_listener = tokio::net::TcpListener::bind(grpc_addr)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to bind to {}: {}", grpc_addr, e))?;

        let http_listener = tokio::net::TcpListener::bind(http_addr)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to bind to {}: {}", http_addr, e))?;

        let (grpc_tx, grpc_rx) = tokio::sync::oneshot::channel();
        let (http_tx, http_rx) = tokio::sync::oneshot::channel();

        let rds_connection = self.rds_connection.clone();
        let metrics = self.metrics.clone();

        // Create pricing components for HTTP endpoints
        let price_cache = Arc::new(PriceCache::new(self.rds_connection.pool().clone()));
        let pricing_service = if self.config.pricing.enabled {
            info!("Dynamic pricing enabled - creating pricing service for HTTP endpoints");
            match crate::pricing::providers::create_providers(&self.config.pricing) {
                Ok(providers) if !providers.is_empty() => {
                    Some(Arc::new(PricingService::new(
                        providers,
                        price_cache.clone(),
                        self.config.pricing.clone(),
                    )))
                }
                Ok(_) => {
                    warn!("No pricing providers configured");
                    None
                }
                Err(e) => {
                    warn!("Failed to create pricing providers: {}", e);
                    None
                }
            }
        } else {
            None
        };

        // Start HTTP server
        let http_handle = tokio::spawn(async move {
            Self::start_http_server(
                http_listener,
                http_rx,
                rds_connection,
                metrics,
                pricing_service,
                price_cache,
            )
            .await
        });

        // Start gRPC server
        let grpc_handle =
            tokio::spawn(async move { self.run_with_listener(grpc_listener, grpc_rx).await });

        // Wait for shutdown signal and propagate to both servers
        tokio::spawn(async move {
            shutdown_signal.await;
            let _ = grpc_tx.send(());
            let _ = http_tx.send(());
        });

        // Wait for both servers to complete
        let (grpc_result, http_result) = tokio::try_join!(grpc_handle, http_handle)?;
        grpc_result?;
        http_result?;

        Ok(())
    }

    /// Graceful shutdown
    async fn shutdown(self) -> anyhow::Result<()> {
        info!("Shutting down billing server");

        info!("Closing database connections");

        info!("Billing server shutdown complete");
        Ok(())
    }

    /// Start the background price sync job
    pub async fn start_price_sync_job(
        rds_connection: Arc<RdsConnection>,
        config: BillingConfig,
    ) {
        if !config.pricing.enabled {
            info!("Dynamic pricing is disabled, price sync job will not start");
            return;
        }

        info!("Starting price sync background job");

        tokio::spawn(async move {
            let pricing_config = config.pricing.clone();
            let pool = rds_connection.pool().clone();

            // Create pricing service components
            let cache = Arc::new(PriceCache::new(pool));

            let providers = match create_providers(&pricing_config) {
                Ok(p) => p,
                Err(e) => {
                    error!("Failed to create price providers: {}. Price sync disabled.", e);
                    return;
                }
            };

            if providers.is_empty() {
                info!("No price providers configured, price sync job will not run");
                return;
            }

            let pricing_service = Arc::new(PricingService::new(providers, cache, pricing_config.clone()));

            info!(
                "Price sync job configured: sync_hour_utc={:?}, update_interval={}s",
                pricing_config.sync_hour_utc, pricing_config.update_interval_seconds
            );

            loop {
                // Calculate next sync time
                let next_sync = Self::calculate_next_sync_time(pricing_config.sync_hour_utc);
                let now = chrono::Utc::now();
                let wait_duration = (next_sync - now).to_std().unwrap_or(std::time::Duration::from_secs(60));

                info!(
                    "Next price sync scheduled for {} (in {} seconds)",
                    next_sync.format("%Y-%m-%d %H:%M:%S UTC"),
                    wait_duration.as_secs()
                );

                // Sleep until sync time
                tokio::time::sleep(wait_duration).await;

                // Run price sync
                info!("Starting scheduled price sync");
                match pricing_service.sync_prices().await {
                    Ok(count) => {
                        info!("Price sync completed successfully: {} GPU prices synced", count);
                    }
                    Err(e) => {
                        error!("Price sync failed: {}", e);
                        if !pricing_config.fallback_to_static {
                            error!("Fallback to static pricing is disabled. This may impact billing.");
                        }
                    }
                }
            }
        });

        info!("Price sync job started");
    }

    /// Calculate the next sync time based on configured hour (UTC)
    pub fn calculate_next_sync_time(sync_hour_utc: Option<u8>) -> chrono::DateTime<chrono::Utc> {
        use chrono::{Timelike, Utc};

        let now = Utc::now();
        let sync_hour = sync_hour_utc.unwrap_or(2) as u32; // Default to 2 AM UTC

        // Try today at sync_hour
        let today_sync = now
            .date_naive()
            .and_hms_opt(sync_hour, 0, 0)
            .expect("Invalid sync hour")
            .and_utc();

        if now < today_sync {
            // Sync time hasn't happened yet today
            today_sync
        } else {
            // Sync time already passed today, schedule for tomorrow
            (now + chrono::Duration::days(1))
                .date_naive()
                .and_hms_opt(sync_hour, 0, 0)
                .expect("Invalid sync hour")
                .and_utc()
        }
    }

    async fn telemetry_consumer_loop(
        mut receiver: mpsc::Receiver<basilica_protocol::billing::TelemetryData>,
        processor: Arc<TelemetryProcessor>,
    ) {
        info!("Starting telemetry consumer loop");

        while let Some(telemetry_data) = receiver.recv().await {
            if let Err(e) = processor.process_telemetry(telemetry_data).await {
                error!("Failed to process buffered telemetry: {}", e);
            }
        }

        info!("Telemetry consumer loop stopped");
    }

    async fn start_http_server(
        listener: tokio::net::TcpListener,
        shutdown_signal: tokio::sync::oneshot::Receiver<()>,
        rds_connection: Arc<RdsConnection>,
        metrics: Option<Arc<BillingMetricsSystem>>,
        pricing_service: Option<Arc<PricingService>>,
        price_cache: Arc<PriceCache>,
    ) -> anyhow::Result<()> {
        let addr = listener.local_addr()?;
        info!("Starting billing HTTP server on {}", addr);

        // Create app state
        let app_state = AppState {
            rds_connection,
            metrics,
            pricing_service: pricing_service.clone(),
            price_cache: price_cache.clone(),
        };

        // Create pricing routes with their state
        let pricing_state = crate::http::pricing::PricingState {
            pricing_service,
            price_cache,
        };
        let pricing_routes = crate::http::pricing_routes(pricing_state);

        let app = Router::new()
            .route("/health", get(health_check))
            .route("/metrics", get(metrics_handler))
            .with_state(app_state)
            .merge(pricing_routes)
            .layer(
                ServiceBuilder::new()
                    .layer(CorsLayer::permissive())
                    .into_inner(),
            );

        let server = axum::serve(listener, app);

        server
            .with_graceful_shutdown(async {
                let _ = shutdown_signal.await;
            })
            .await
            .map_err(|e| anyhow::anyhow!("HTTP server error: {}", e))?;

        info!("HTTP server stopped gracefully");
        Ok(())
    }
}

#[derive(Clone)]
struct AppState {
    rds_connection: Arc<RdsConnection>,
    metrics: Option<Arc<BillingMetricsSystem>>,
    pricing_service: Option<Arc<PricingService>>,
    price_cache: Arc<PriceCache>,
}

async fn health_check(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> Result<Json<Value>, StatusCode> {
    let pool = state.rds_connection.pool();
    match sqlx::query("SELECT 1").fetch_one(pool).await {
        Ok(_) => Ok(Json(serde_json::json!({
            "status": "healthy",
            "service": "basilica-billing",
            "timestamp": chrono::Utc::now().to_rfc3339(),
            "database": "connected"
        }))),
        Err(e) => {
            error!("Health check database error: {}", e);
            Err(StatusCode::SERVICE_UNAVAILABLE)
        }
    }
}

async fn metrics_handler(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> Result<String, StatusCode> {
    match state.metrics {
        Some(metrics) => Ok(metrics.render_prometheus()),
        None => Ok("# Metrics collection disabled\n".to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Timelike, TimeZone, Utc};

    #[test]
    fn test_calculate_next_sync_time_before_sync_hour() {
        // Mock current time: 2024-01-15 01:00:00 UTC
        // Sync hour: 2 AM UTC
        // Expected: Today at 2 AM

        let sync_hour = Some(2);

        // Since we can't easily mock the current time, we'll test the logic indirectly
        // by checking that the result is either today or tomorrow at the sync hour

        let next_sync = BillingServer::calculate_next_sync_time(sync_hour);

        // Verify it's at the correct hour
        assert_eq!(next_sync.hour(), 2);
        assert_eq!(next_sync.minute(), 0);
        assert_eq!(next_sync.second(), 0);

        // Verify it's in the future
        let now = Utc::now();
        assert!(next_sync > now);

        // Verify it's within the next 24 hours
        let max_wait = now + chrono::Duration::days(1);
        assert!(next_sync <= max_wait);
    }

    #[test]
    fn test_calculate_next_sync_time_after_sync_hour() {
        // Test with a sync hour that's definitely in the past (e.g., hour 0)
        let sync_hour = Some(0);

        let next_sync = BillingServer::calculate_next_sync_time(sync_hour);

        // Verify it's at midnight
        assert_eq!(next_sync.hour(), 0);
        assert_eq!(next_sync.minute(), 0);
        assert_eq!(next_sync.second(), 0);

        // Verify it's in the future
        let now = Utc::now();
        assert!(next_sync > now);
    }

    #[test]
    fn test_calculate_next_sync_time_default_hour() {
        // Test with None (should default to 2 AM)
        let next_sync = BillingServer::calculate_next_sync_time(None);

        // Verify it's at 2 AM
        assert_eq!(next_sync.hour(), 2);
        assert_eq!(next_sync.minute(), 0);
        assert_eq!(next_sync.second(), 0);

        // Verify it's in the future
        let now = Utc::now();
        assert!(next_sync > now);
    }

    #[test]
    fn test_calculate_next_sync_time_various_hours() {
        // Test several different hours
        for hour in [0u8, 6, 12, 18, 23] {
            let next_sync = BillingServer::calculate_next_sync_time(Some(hour));

            assert_eq!(next_sync.hour(), hour as u32);
            assert_eq!(next_sync.minute(), 0);
            assert_eq!(next_sync.second(), 0);

            let now = Utc::now();
            assert!(next_sync > now);

            // Should be within next 24 hours
            let max_wait = now + chrono::Duration::days(1);
            assert!(next_sync <= max_wait);
        }
    }

    #[test]
    fn test_calculate_next_sync_time_exactly_at_sync_hour() {
        // This tests the edge case where current time might be exactly at sync hour
        // The function should schedule for tomorrow in this case
        let sync_hour = Some(Utc::now().hour() as u8);

        let next_sync = BillingServer::calculate_next_sync_time(sync_hour);

        let now = Utc::now();

        // Should be in the future (either today if minutes/seconds are less, or tomorrow)
        assert!(next_sync > now);

        // Should be at the correct hour
        assert_eq!(next_sync.hour(), sync_hour.unwrap() as u32);
    }
}
