use basilica_billing::server::BillingServer;
use basilica_billing::storage::rds::RdsConnection;
use basilica_protocol::billing::billing_service_client::BillingServiceClient;
use sqlx::{Pool, Postgres, Row};
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;
use tonic::transport::Channel;

// Import the test database utilities from parent module
use crate::common;

pub struct TestContext {
    pub client: BillingServiceClient<Channel>,
    pub pool: Pool<Postgres>,
    pub server_handle: tokio::task::JoinHandle<()>,
    pub shutdown_tx: tokio::sync::oneshot::Sender<()>,
}

impl TestContext {
    pub async fn new() -> Self {
        // Use the test database pool from the singleton container
        let pool = common::test_db::get_test_pool()
            .await
            .expect("Failed to get test database pool");

        // Each test uses unique user IDs so they won't conflict

        // Get the database URL from the test container
        let database_url = common::test_db::get_test_database_url()
            .await
            .expect("Failed to get test database URL");

        let db_config = basilica_billing::config::DatabaseConfig {
            url: database_url.to_string(),
            max_connections: 5,
            min_connections: 2,
            connect_timeout_seconds: 30,
            acquire_timeout_seconds: 30,
            idle_timeout_seconds: 600,
            max_lifetime_seconds: 1800,
            enable_ssl: false,
            ssl_ca_cert_path: None,
        };
        let rds_connection = Arc::new(
            RdsConnection::new_direct(db_config)
                .await
                .expect("Failed to create RDS connection"),
        );

        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("Failed to bind listener");
        let addr = listener.local_addr().expect("Failed to get local address");

        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();

        let server = BillingServer::new(rds_connection);
        let server_handle = tokio::spawn(async move {
            server
                .run_with_listener(listener, shutdown_rx)
                .await
                .expect("Server failed to run");
        });

        tokio::time::sleep(Duration::from_millis(100)).await;

        let endpoint = format!("http://{}", addr);
        let client = BillingServiceClient::connect(endpoint)
            .await
            .expect("Failed to connect to server");

        TestContext {
            client,
            pool,
            server_handle,
            shutdown_tx,
        }
    }

    pub async fn create_test_user(&self, user_id: &str, initial_balance: &str) {
        let mut tx = self
            .pool
            .begin()
            .await
            .expect("Failed to start transaction");

        // First ensure user exists in users table
        let user_uuid = sqlx::query_scalar::<_, uuid::Uuid>(
            "INSERT INTO billing.users (external_id)
             VALUES ($1)
             ON CONFLICT (external_id) DO UPDATE SET updated_at = NOW()
             RETURNING user_id",
        )
        .bind(user_id)
        .fetch_one(&mut *tx)
        .await
        .expect("Failed to create user");

        // Then insert/update credits using ON CONFLICT to handle race conditions
        sqlx::query(
            "INSERT INTO billing.credits (user_id, balance, lifetime_spent, updated_at)
             VALUES ($1, $2, 0, NOW())
             ON CONFLICT (user_id) DO UPDATE SET
               balance = EXCLUDED.balance,
               updated_at = NOW()",
        )
        .bind(user_uuid)
        .bind(initial_balance.parse::<rust_decimal::Decimal>().unwrap())
        .execute(&mut *tx)
        .await
        .expect("Failed to create/update test user credits");

        tx.commit().await.expect("Failed to commit transaction");
    }

    pub async fn get_user_balance(&self, user_id: &str) -> rust_decimal::Decimal {
        sqlx::query_scalar::<_, rust_decimal::Decimal>(
            "SELECT c.balance FROM billing.credits c
             JOIN billing.users u ON c.user_id = u.user_id
             WHERE u.external_id = $1",
        )
        .bind(user_id)
        .fetch_one(&self.pool)
        .await
        .unwrap_or(rust_decimal::Decimal::ZERO)
    }

    #[allow(dead_code)]
    pub async fn count_active_rentals(&self, user_id: Option<&str>) -> i64 {
        let query = if let Some(uid) = user_id {
            sqlx::query_scalar::<_, i64>(
                "SELECT COUNT(*) FROM billing.rentals WHERE user_id = $1 AND status IN ('active', 'pending')"
            )
            .bind(uid)
        } else {
            sqlx::query_scalar::<_, i64>(
                "SELECT COUNT(*) FROM billing.rentals WHERE status IN ('active', 'pending')",
            )
        };

        query.fetch_one(&self.pool).await.unwrap_or(0)
    }

    pub async fn rental_exists(&self, rental_id: &str) -> bool {
        sqlx::query_scalar::<_, bool>(
            "SELECT EXISTS(SELECT 1 FROM billing.rentals WHERE rental_id = $1::uuid)",
        )
        .bind(rental_id)
        .fetch_one(&self.pool)
        .await
        .unwrap_or(false)
    }

    pub async fn get_rental_status(&self, rental_id: &str) -> Option<String> {
        sqlx::query_scalar::<_, String>(
            "SELECT status FROM billing.rentals WHERE rental_id = $1::uuid",
        )
        .bind(rental_id)
        .fetch_optional(&self.pool)
        .await
        .unwrap_or(None)
    }

    pub async fn count_usage_events(&self, rental_id: &str) -> i64 {
        sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM billing.usage_events WHERE rental_id = $1::uuid",
        )
        .bind(rental_id)
        .fetch_one(&self.pool)
        .await
        .unwrap_or(0)
    }

    pub async fn get_usage_for_rental(
        &self,
        rental_id: &str,
    ) -> basilica_billing::domain::types::UsageMetrics {
        use basilica_billing::domain::types::UsageMetrics;
        use rust_decimal::Decimal;

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
            WHERE rental_id = $1::uuid AND event_type = 'telemetry'
            "#,
        )
        .bind(rental_id)
        .fetch_one(&self.pool)
        .await
        .expect("Failed to fetch usage metrics");

        UsageMetrics {
            gpu_hours: row.get("gpu_hours"),
            gpu_count: row.try_get::<i32, _>("gpu_count").unwrap_or(1) as u32,
            cpu_hours: row.get("cpu_hours"),
            memory_gb_hours: row.get("memory_gb_hours"),
            storage_gb_hours: row.get("storage_gb_hours"),
            network_gb: row.get("network_gb"),
            disk_io_gb: Decimal::ZERO,
        }
    }

    pub async fn cleanup(self) {
        let _ = self.shutdown_tx.send(());
        let _ = tokio::time::timeout(Duration::from_secs(5), self.server_handle).await;
    }
}

pub mod scenarios;
