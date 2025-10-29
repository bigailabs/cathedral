//! Test database utilities using Docker PostgreSQL container
//!
//! Provides a singleton PostgreSQL container for faster test execution.

#![allow(dead_code)]

use bollard::container::{
    Config, CreateContainerOptions, RemoveContainerOptions, StartContainerOptions,
};
use bollard::models::{HostConfig, PortBinding};
use bollard::Docker;
use once_cell::sync::Lazy;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::{PgPool, Postgres};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;

/// PostgreSQL container for integration testing
pub struct PostgresContainer {
    docker: Docker,
    container_id: String,
    port: u16,
    database_url: String,
}

impl PostgresContainer {
    /// Start a new PostgreSQL container for testing
    pub async fn start() -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let docker = Docker::connect_with_local_defaults().map_err(|e| {
            format!(
                "Failed to connect to Docker daemon: {}. Is Docker running?",
                e
            )
        })?;

        // Find an available port
        let port = portpicker::pick_unused_port().ok_or("No available ports found")?;

        // Pull postgres image if needed
        Self::ensure_postgres_image(&docker).await?;

        // Create container configuration
        let container_name = format!("basilica-billing-test-postgres-{}", port);

        let mut port_bindings = HashMap::new();
        port_bindings.insert(
            "5432/tcp".to_string(),
            Some(vec![PortBinding {
                host_ip: Some("127.0.0.1".to_string()),
                host_port: Some(port.to_string()),
            }]),
        );

        let host_config = HostConfig {
            port_bindings: Some(port_bindings),
            auto_remove: Some(true),
            ..Default::default()
        };

        let mut env = vec![
            "POSTGRES_PASSWORD=postgres",
            "POSTGRES_USER=postgres",
            "POSTGRES_DB=basilica_billing_test",
        ];

        // For CI environments, disable fsync for faster tests
        if std::env::var("CI").is_ok() {
            env.push("POSTGRES_FSYNC=off");
        }

        let config = Config {
            image: Some("postgres:15"),
            env: Some(env),
            host_config: Some(host_config),
            exposed_ports: Some(vec![("5432/tcp", HashMap::new())].into_iter().collect()),
            ..Default::default()
        };

        // Create and start container
        let container = docker
            .create_container(
                Some(CreateContainerOptions {
                    name: container_name.clone(),
                    platform: None,
                }),
                config,
            )
            .await
            .map_err(|e| format!("Failed to create container: {}", e))?;

        let container_id = container.id;

        docker
            .start_container(&container_id, None::<StartContainerOptions<String>>)
            .await
            .map_err(|e| format!("Failed to start container: {}", e))?;

        let database_url = format!(
            "postgres://postgres:postgres@127.0.0.1:{}/basilica_billing_test",
            port
        );

        let mut pg_container = Self {
            docker,
            container_id,
            port,
            database_url,
        };

        // Wait for PostgreSQL to be ready
        pg_container.wait_until_ready().await?;

        Ok(pg_container)
    }

    /// Ensure postgres:15 image is available locally
    async fn ensure_postgres_image(
        docker: &Docker,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        use bollard::image::CreateImageOptions;
        use futures_util::StreamExt;

        println!("Checking for postgres:15 image...");

        // Check if image exists
        match docker.inspect_image("postgres:15").await {
            Ok(_) => {
                println!("✓ postgres:15 image found");
                return Ok(());
            }
            Err(_) => {
                println!("Pulling postgres:15 image (this may take a minute)...");
            }
        }

        // Pull the image
        let options = CreateImageOptions {
            from_image: "postgres",
            tag: "15",
            ..Default::default()
        };

        let mut stream = docker.create_image(Some(options), None, None);

        while let Some(result) = stream.next().await {
            match result {
                Ok(info) => {
                    if let Some(status) = info.status {
                        if status.contains("Download") || status.contains("Pull complete") {
                            print!(".");
                            std::io::Write::flush(&mut std::io::stdout()).ok();
                        }
                    }
                }
                Err(e) => return Err(format!("Failed to pull postgres image: {}", e).into()),
            }
        }

        println!("\n✓ postgres:15 image pulled successfully");
        Ok(())
    }

    /// Wait for PostgreSQL to be ready to accept connections
    async fn wait_until_ready(&mut self) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        println!(
            "Waiting for PostgreSQL to be ready on port {}...",
            self.port
        );

        let max_attempts = 30;
        let mut attempt = 0;

        loop {
            attempt += 1;

            // Try to connect
            let options = PgConnectOptions::new()
                .host("127.0.0.1")
                .port(self.port)
                .username("postgres")
                .password("postgres")
                .database("basilica_billing_test");

            match sqlx::PgPool::connect_with(options.clone()).await {
                Ok(_) => {
                    println!("✓ PostgreSQL is ready!");
                    return Ok(());
                }
                Err(e) => {
                    if attempt >= max_attempts {
                        return Err(format!(
                            "PostgreSQL did not become ready after {} seconds: {}",
                            max_attempts, e
                        )
                        .into());
                    }
                    print!(".");
                    std::io::Write::flush(&mut std::io::stdout()).ok();
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
            }
        }
    }

    /// Create a connection pool to this PostgreSQL instance
    pub async fn create_pool(&self) -> Result<PgPool, sqlx::Error> {
        let pool = PgPoolOptions::new()
            .max_connections(5)
            .connect(&self.database_url)
            .await?;

        // Run migrations
        println!("Running database migrations...");
        sqlx::migrate!("./migrations")
            .run(&pool)
            .await
            .map_err(|e| {
                eprintln!("Failed to run migrations: {}", e);
                e
            })?;
        println!("✓ Migrations complete");

        Ok(pool)
    }

    /// Get the database URL for this container
    pub fn database_url(&self) -> &str {
        &self.database_url
    }
}

impl Drop for PostgresContainer {
    fn drop(&mut self) {
        let docker = self.docker.clone();
        let container_id = self.container_id.clone();

        std::thread::spawn(move || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async {
                // Try to stop container
                if let Err(e) = docker.stop_container(&container_id, None).await {
                    eprintln!("Warning: Failed to stop container during cleanup: {}", e);
                }

                // Force remove if still exists
                let remove_options = RemoveContainerOptions {
                    force: true,
                    ..Default::default()
                };

                if let Err(e) = docker
                    .remove_container(&container_id, Some(remove_options))
                    .await
                {
                    if !e.to_string().contains("No such container") {
                        eprintln!("Warning: Failed to remove container during cleanup: {}", e);
                    }
                }
            });
        });
    }
}

/// Global singleton PostgreSQL container
static POSTGRES_INSTANCE: Lazy<Arc<Mutex<Option<PostgresContainer>>>> =
    Lazy::new(|| Arc::new(Mutex::new(None)));

/// Get or create the singleton PostgreSQL container pool
pub async fn get_test_pool(
) -> Result<sqlx::Pool<Postgres>, Box<dyn std::error::Error + Send + Sync>> {
    // Check if we should use an existing database instead of Docker
    if let Ok(database_url) = std::env::var("TEST_DATABASE_URL") {
        println!("Using existing database from TEST_DATABASE_URL");
        let pool = PgPoolOptions::new()
            .max_connections(5)
            .connect(&database_url)
            .await?;

        // Run migrations on existing database
        println!("Running database migrations...");
        sqlx::migrate!("./migrations").run(&pool).await?;
        println!("✓ Migrations complete");

        return Ok(pool);
    }

    let mut instance = POSTGRES_INSTANCE.lock().await;

    if instance.is_none() {
        println!("\n🐘 Starting shared PostgreSQL container for BDD tests...");
        let container = PostgresContainer::start().await?;
        let pool = container.create_pool().await?;
        println!("✓ PostgreSQL ready for BDD tests\n");
        *instance = Some(container);
        Ok(pool)
    } else {
        let container = instance.as_ref().unwrap();
        let pool = PgPoolOptions::new()
            .max_connections(5)
            .connect(container.database_url())
            .await?;
        Ok(pool)
    }
}

/// Get the database URL for the singleton container
pub async fn get_test_database_url() -> Result<String, Box<dyn std::error::Error + Send + Sync>> {
    // Check if we should use an existing database instead of Docker
    if let Ok(database_url) = std::env::var("TEST_DATABASE_URL") {
        return Ok(database_url);
    }

    let mut instance = POSTGRES_INSTANCE.lock().await;

    if instance.is_none() {
        println!("\n🐘 Starting shared PostgreSQL container for BDD tests...");
        let container = PostgresContainer::start().await?;
        let url = container.database_url().to_string();
        println!("✓ PostgreSQL ready for BDD tests\n");
        *instance = Some(container);
        Ok(url)
    } else {
        let container = instance.as_ref().unwrap();
        Ok(container.database_url().to_string())
    }
}
