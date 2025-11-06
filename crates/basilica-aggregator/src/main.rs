use basilica_aggregator::{
    api, background::BackgroundRefreshTask, config::Config, db::Database,
    service::AggregatorService,
};
use std::sync::Arc;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Initialize tracing
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "basilica_aggregator=debug,tower_http=debug".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    tracing::info!("Basilica GPU Price Aggregator starting...");

    // Load configuration
    let config_path = std::env::args()
        .nth(1)
        .map(std::path::PathBuf::from)
        .or_else(|| {
            let path = std::path::PathBuf::from("config/aggregator.toml");
            if path.exists() {
                Some(path)
            } else {
                None
            }
        });

    let config = Config::load(config_path)?;
    config.validate()?;

    tracing::info!("Configuration loaded successfully");

    // Initialize database
    let db = Arc::new(Database::new(&config.database.path).await?);
    tracing::info!("Database initialized at {}", config.database.path);

    // Initialize service
    let service = Arc::new(AggregatorService::new(db, config.clone())?);
    tracing::info!(
        "Service initialized with {} provider(s)",
        if service.get_provider_health().await?.is_empty() {
            0
        } else {
            1
        }
    );

    // Start background refresh task
    let refresh_task =
        BackgroundRefreshTask::new(service.clone(), config.cache.ttl_seconds).start();
    tracing::info!(
        "Background refresh task started (interval: {}s)",
        config.cache.ttl_seconds
    );

    // Create API router
    let app = api::create_router(service);

    // Start HTTP server
    let addr = format!("{}:{}", config.server.host, config.server.port);
    let listener = tokio::net::TcpListener::bind(&addr).await?;

    tracing::info!("Server listening on {}", addr);

    // Handle graceful shutdown
    let server = axum::serve(listener, app);

    // Setup graceful shutdown signal handling
    tokio::select! {
        result = server => {
            if let Err(e) = result {
                tracing::error!("Server error: {}", e);
            }
        }
        _ = tokio::signal::ctrl_c() => {
            tracing::info!("Received shutdown signal, stopping...");
        }
    }

    // Abort background task on shutdown
    refresh_task.abort();
    tracing::info!("Background refresh task stopped");

    Ok(())
}
