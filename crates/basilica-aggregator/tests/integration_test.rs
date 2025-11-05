use basilica_aggregator::{
    config::*, db::Database, models::*, service::AggregatorService,
};
use std::sync::Arc;

#[tokio::test]
async fn test_service_initialization() {
    // Create in-memory database
    let db = Arc::new(Database::new(":memory:").await.unwrap());

    // Create test config
    let config = Config {
        server: ServerConfig {
            host: "127.0.0.1".to_string(),
            port: 8080,
        },
        cache: CacheConfig { ttl_seconds: 45 },
        providers: ProvidersConfig {
            datacrunch: ProviderConfig {
                enabled: false, // Disabled for test
                ..Default::default()
            },
            hyperstack: ProviderConfig::default(),
            lambda: ProviderConfig::default(),
        },
        database: DatabaseConfig {
            path: ":memory:".to_string(),
        },
    };

    // Should fail - no providers enabled
    let result = AggregatorService::new(db, config);
    assert!(result.is_err());
}

#[tokio::test]
async fn test_database_operations() {
    let db = Database::new(":memory:").await.unwrap();

    // Test provider status update
    db.update_provider_status(Provider::DataCrunch, true, None)
        .await
        .unwrap();

    let health = db.get_provider_health(Provider::DataCrunch).await.unwrap();
    assert!(health.is_healthy);
    assert!(health.last_success_at.is_some());
}
