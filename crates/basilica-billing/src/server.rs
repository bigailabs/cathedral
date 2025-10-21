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
    pricing_service: Option<Arc<PricingService>>,
    price_cache: Arc<PriceCache>,
}

impl BillingServer {
    pub fn new(rds_connection: Arc<RdsConnection>) -> Self {
        let price_cache = Arc::new(PriceCache::new(rds_connection.pool().clone()));
        Self {
            config: BillingConfig::default(),
            rds_connection,
            metrics: None,
            pricing_service: None,
            price_cache,
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

        // Create shared price cache
        let price_cache = Arc::new(PriceCache::new(rds_connection.pool().clone()));

        // Build shared pricing service if enabled
        let pricing_service = if config.pricing.enabled {
            info!("Dynamic pricing is enabled, building pricing service");

            match create_providers(&config.pricing) {
                Ok(providers) if !providers.is_empty() => {
                    info!("Created {} pricing providers", providers.len());
                    Some(Arc::new(PricingService::new(
                        providers,
                        price_cache.clone(),
                        config.pricing.clone(),
                    )))
                }
                Ok(_) => {
                    info!("No pricing providers configured, dynamic pricing will be disabled");
                    None
                }
                Err(e) => {
                    error!(
                        "Failed to create pricing providers: {}. Dynamic pricing will be disabled.",
                        e
                    );
                    None
                }
            }
        } else {
            info!("Dynamic pricing is disabled");
            None
        };

        Ok(Self {
            config,
            rds_connection,
            metrics,
            pricing_service,
            price_cache,
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

        let pricing_config = if self.config.pricing.enabled {
            Some(self.config.pricing.clone())
        } else {
            None
        };

        let billing_service = BillingServiceImpl::new_with_pricing(
            self.rds_connection.clone(),
            telemetry_ingester.clone(),
            telemetry_processor.clone(),
            self.metrics.clone(),
            self.pricing_service.clone(),
            pricing_config.clone(),
            self.price_cache.clone(),
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

        // Create package repository with shared pricing service
        let mut package_repository = SqlPackageRepository::new(self.rds_connection.pool().clone());
        if let Some(ref pricing_service) = self.pricing_service {
            package_repository = package_repository.with_pricing_service(pricing_service.clone());
        }
        let package_repository = Arc::new(package_repository);

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
        if let Some(ref pricing_service) = self.pricing_service {
            Self::start_price_sync_job(pricing_service.clone(), self.config.pricing.clone()).await;
        } else {
            info!("Pricing service not available, price sync job will not start");
        }

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

        // Start HTTP server
        let http_handle = tokio::spawn(async move {
            Self::start_http_server(http_listener, http_rx, rds_connection, metrics).await
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

    /// Start the background price sync job with shared pricing service
    pub async fn start_price_sync_job(
        pricing_service: Arc<PricingService>,
        pricing_config: crate::pricing::types::PricingConfig,
    ) {
        info!("Starting price sync background job");

        tokio::spawn(async move {
            let interval_seconds =
                Self::normalize_update_interval(pricing_config.update_interval_seconds);
            if pricing_config.update_interval_seconds == 0 {
                warn!(
                    "Configured update_interval_seconds was 0; defaulting price sync interval to 86400s"
                );
            }
            let interval_duration = std::time::Duration::from_secs(interval_seconds);

            info!(
                "Price sync job configured: update_interval={}s, cache_ttl={}s",
                interval_seconds, pricing_config.cache_ttl_seconds
            );

            loop {
                info!("Starting scheduled price sync");
                match pricing_service.sync_prices().await {
                    Ok(count) => {
                        info!(
                            "Price sync completed successfully: {} GPU prices synced",
                            count
                        );
                    }
                    Err(e) => {
                        error!("Price sync failed: {}", e);
                        if !pricing_config.fallback_to_static {
                            error!(
                                "Fallback to static pricing is disabled. This may impact billing."
                            );
                        }
                    }
                }

                tokio::time::sleep(interval_duration).await;
            }
        });

        info!("Price sync job started");
    }

    fn normalize_update_interval(update_interval_seconds: u64) -> u64 {
        if update_interval_seconds == 0 {
            86_400
        } else {
            update_interval_seconds
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
    ) -> anyhow::Result<()> {
        let addr = listener.local_addr()?;
        info!("Starting billing HTTP server on {}", addr);

        // Create app state
        let app_state = AppState {
            rds_connection,
            metrics,
        };

        let app = Router::new()
            .route("/health", get(health_check))
            .route("/metrics", get(metrics_handler))
            .with_state(app_state)
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

    #[test]
    fn test_normalize_update_interval_defaults_when_zero() {
        let normalized = BillingServer::normalize_update_interval(0);
        assert_eq!(
            normalized, 86_400,
            "Zero update interval should default to 86400 seconds (24h)"
        );
    }

    #[test]
    fn test_normalize_update_interval_preserves_value() {
        let normalized = BillingServer::normalize_update_interval(3_600);
        assert_eq!(
            normalized, 3_600,
            "Non-zero update interval should be preserved"
        );
    }

    #[test]
    fn test_normalize_update_interval_large_value() {
        let normalized = BillingServer::normalize_update_interval(200_000);
        assert_eq!(
            normalized, 200_000,
            "Arbitrary large interval should remain unchanged"
        );
    }
}
