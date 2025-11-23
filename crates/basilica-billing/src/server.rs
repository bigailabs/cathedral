use crate::config::BillingConfig;
use crate::grpc::BillingServiceImpl;
use crate::metrics::BillingMetricsSystem;
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
use tracing::{error, info};

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
        use crate::storage::{SqlCreditRepository, SqlRentalRepository};

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
        let audit_repository = Arc::new(crate::storage::SqlAuditRepository::new(
            self.rds_connection.clone(),
        ));
        let credit_repository = Arc::new(SqlCreditRepository::new(
            self.rds_connection.clone(),
            audit_repository,
        ));

        let billing_handlers: Arc<dyn crate::domain::processor::EventHandlers + Send + Sync> =
            Arc::new(BillingEventHandlers::new(
                rental_repository,
                credit_repository,
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
    #[allow(unused_imports)]
    use super::*;
}
