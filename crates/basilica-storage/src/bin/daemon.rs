//! Basilica Storage Daemon
//!
//! FUSE filesystem daemon that provides transparent object storage for stateful workloads.
//!
//! ## Operating Modes
//!
//! ### Sidecar Mode (default)
//! - Runs as a sidecar container alongside user workloads
//! - Single mount point, credentials from environment variables
//! - Use: `basilica-storage-daemon --mode sidecar --namespace u-alice --experiment-id exp1`
//!
//! ### DaemonSet Mode
//! - Runs as a DaemonSet, manages mounts for all user namespaces on the node
//! - Reads credentials from K8s secrets in each namespace
//! - Use: `basilica-storage-daemon --mode daemonset`

use anyhow::{Context, Result};
use clap::{Parser, ValueEnum};
use std::path::PathBuf;
use std::sync::Arc;
use tracing::info;

#[derive(Debug, Clone, Copy, ValueEnum, Default)]
enum DaemonMode {
    #[default]
    Sidecar,
    Daemonset,
}

/// Basilica Storage Daemon - FUSE filesystem with object storage backend
#[derive(Parser, Debug)]
#[command(name = "basilica-storage-daemon")]
#[command(about = "FUSE filesystem daemon with transparent object storage", long_about = None)]
struct Args {
    /// Operating mode
    #[arg(long, value_enum, default_value = "sidecar", env = "DAEMON_MODE")]
    mode: DaemonMode,

    // --- Sidecar mode options ---
    /// Kubernetes namespace (used as prefix in object storage) [sidecar mode]
    #[arg(short = 'n', long, env = "NAMESPACE")]
    namespace: Option<String>,

    /// Experiment ID (used as prefix in object storage) [sidecar mode]
    #[arg(short, long, env = "EXPERIMENT_ID")]
    experiment_id: Option<String>,

    /// Mount point for the FUSE filesystem [sidecar mode]
    #[arg(short, long, default_value = "/data", env = "MOUNT_POINT")]
    mount_point: PathBuf,

    /// Storage backend type (r2, s3, gcs) [sidecar mode]
    #[arg(short = 'b', long, default_value = "r2", env = "STORAGE_BACKEND")]
    backend: String,

    /// S3/R2 bucket name [sidecar mode]
    #[arg(long, env = "STORAGE_BUCKET")]
    bucket: Option<String>,

    /// S3/R2 region [sidecar mode]
    #[arg(long, env = "STORAGE_REGION")]
    region: Option<String>,

    /// S3/R2 endpoint (required for R2, optional for S3) [sidecar mode]
    #[arg(long, env = "STORAGE_ENDPOINT")]
    endpoint: Option<String>,

    /// S3/R2 access key ID [sidecar mode]
    #[arg(long, env = "STORAGE_ACCESS_KEY_ID")]
    access_key_id: Option<String>,

    /// S3/R2 secret access key [sidecar mode]
    #[arg(long, env = "STORAGE_SECRET_ACCESS_KEY")]
    secret_access_key: Option<String>,

    // --- Common options ---
    /// Background sync interval in milliseconds
    #[arg(long, default_value = "1000", env = "SYNC_INTERVAL_MS")]
    sync_interval_ms: u64,

    /// Cache size in MB
    #[arg(long, default_value = "2048", env = "CACHE_SIZE_MB")]
    cache_size_mb: usize,

    /// Storage quota in GB
    #[arg(long, default_value = "100", env = "QUOTA_GB")]
    quota_gb: u64,

    /// Allow other users to access the filesystem
    #[arg(long)]
    allow_other: bool,

    /// Automatically unmount on process exit [sidecar mode]
    #[arg(long, default_value = "false")]
    auto_unmount: bool,

    /// HTTP server port for health and metrics endpoints
    #[arg(long, default_value = "9090", env = "HTTP_PORT")]
    http_port: u16,

    // --- DaemonSet mode options ---
    /// Base path for FUSE mounts [daemonset mode]
    #[arg(long, default_value = "/var/lib/basilica/fuse", env = "FUSE_BASE_PATH")]
    fuse_base_path: PathBuf,
}

#[tokio::main]
async fn main() -> Result<()> {
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive(tracing::Level::INFO.into()),
        )
        .init();

    let args = Args::parse();

    match args.mode {
        DaemonMode::Sidecar => run_sidecar_mode(args).await,
        DaemonMode::Daemonset => run_daemonset_mode(args).await,
    }
}

/// Run in sidecar mode (single tenant, credentials from env)
async fn run_sidecar_mode(args: Args) -> Result<()> {
    use basilica_storage::{
        backend::{S3Backend, StorageBackend},
        config::StorageConfig,
        fuse::{BasilicaFS, SharedBasilicaFS},
        http::HttpServer,
        StorageMetrics,
    };
    use fuser::MountOption;

    let namespace = args
        .namespace
        .context("Namespace is required in sidecar mode (--namespace or NAMESPACE env var)")?;
    let experiment_id = args.experiment_id.context(
        "Experiment ID is required in sidecar mode (--experiment-id or EXPERIMENT_ID env var)",
    )?;
    let bucket = args
        .bucket
        .context("Bucket must be specified via --bucket arg or STORAGE_BUCKET env var")?;

    info!(
        "Starting Basilica Storage Daemon (sidecar mode) for experiment: {}",
        experiment_id
    );
    info!("Mount point: {}", args.mount_point.display());
    info!("Storage backend: {}", args.backend);
    info!("Bucket: {}", bucket);
    info!("Sync interval: {}ms", args.sync_interval_ms);
    info!("Cache size: {}MB", args.cache_size_mb);

    // Create storage backend
    let config = match args.backend.as_str() {
        "r2" => {
            let account_id = args
                .endpoint
                .as_ref()
                .and_then(|e| {
                    let url_without_scheme = e
                        .strip_prefix("https://")
                        .or_else(|| e.strip_prefix("http://"))
                        .unwrap_or(e);
                    url_without_scheme.split('.').next()
                })
                .context("R2 requires account_id from endpoint")?;

            let access_key = args.access_key_id.context("R2 requires access_key_id")?;
            let secret_key = args
                .secret_access_key
                .context("R2 requires secret_access_key")?;

            StorageConfig::r2(account_id, &access_key, &secret_key, &bucket)
        }
        "s3" => {
            let region = args.region.as_deref().unwrap_or("us-east-1");
            let access_key = args.access_key_id.context("S3 requires access_key_id")?;
            let secret_key = args
                .secret_access_key
                .context("S3 requires secret_access_key")?;

            StorageConfig::s3(region, &access_key, &secret_key, &bucket)
        }
        _ => {
            anyhow::bail!("Unsupported storage backend: {}", args.backend);
        }
    };

    let storage: Arc<dyn StorageBackend> = Arc::new(
        S3Backend::from_config(&config)
            .await
            .context("Failed to create storage backend")?,
    );

    info!("Storage backend initialized successfully");

    let metrics = Arc::new(StorageMetrics::new());
    let quota_bytes = args.quota_gb.saturating_mul(1024 * 1024 * 1024);

    let fs = BasilicaFS::new(
        namespace.clone(),
        experiment_id.clone(),
        storage,
        args.sync_interval_ms,
        args.cache_size_mb,
        quota_bytes,
        metrics.clone(),
    );

    info!("Starting background sync worker...");
    fs.start_sync_worker().await;

    let shared_fs = SharedBasilicaFS::new(fs);
    let shared_fs_for_shutdown = shared_fs.clone();

    // Start HTTP server
    let http_addr = format!("0.0.0.0:{}", args.http_port);
    info!("Starting HTTP server at: {}", http_addr);

    let http_server = HttpServer::new(metrics, shared_fs.arc());
    let http_addr_clone = http_addr.clone();
    tokio::spawn(async move {
        if let Err(e) = http_server.serve(&http_addr_clone).await {
            tracing::error!("HTTP server error: {}", e);
        }
    });

    // Prepare mount options
    let mut mount_options = vec![
        MountOption::FSName("basilica-storage".to_string()),
        MountOption::RW,
    ];

    if args.allow_other {
        mount_options.push(MountOption::AllowOther);
    }

    if args.auto_unmount {
        mount_options.push(MountOption::AutoUnmount);
    }

    // Ensure mount point exists
    if !args.mount_point.exists() {
        std::fs::create_dir_all(&args.mount_point).context("Failed to create mount point")?;
    }

    info!("Mounting filesystem at: {}", args.mount_point.display());

    let session = fuser::spawn_mount2(shared_fs, &args.mount_point, &mount_options)
        .context("Failed to mount filesystem")?;

    info!("Filesystem mounted successfully");

    // Create readiness marker
    let ready_file = args.mount_point.join(".fuse_ready");
    if let Err(e) = std::fs::write(&ready_file, "ready") {
        tracing::warn!("Failed to create readiness marker: {}", e);
    } else {
        info!("Created readiness marker at: {}", ready_file.display());
    }

    info!("Press Ctrl+C to unmount and exit");

    wait_for_shutdown().await;
    info!("Received shutdown signal, flushing dirty pages...");

    if let Err(e) = shared_fs_for_shutdown.shutdown().await {
        tracing::warn!("Failed to flush dirty pages: {}", e);
    } else {
        info!("All dirty pages flushed successfully");
    }

    info!("Unmounting filesystem...");
    drop(session);

    info!("Filesystem unmounted successfully");
    info!("Basilica Storage Daemon stopped");

    Ok(())
}

/// Run in DaemonSet mode (multi-tenant, credentials from K8s secrets)
async fn run_daemonset_mode(args: Args) -> Result<()> {
    use basilica_storage::{
        DaemonHttpServer, KubernetesCredentialProvider, MountManager, NamespaceWatcher,
        PerNamespaceMetricsStore, StorageMetrics,
    };

    info!(
        "Starting Basilica Storage Daemon (daemonset mode) at {}",
        args.fuse_base_path.display()
    );
    info!("HTTP port: {}", args.http_port);

    // Ensure base path exists
    if !args.fuse_base_path.exists() {
        std::fs::create_dir_all(&args.fuse_base_path).context("Failed to create FUSE base path")?;
    }

    // Create Kubernetes credential provider
    let credential_provider = Arc::new(
        KubernetesCredentialProvider::new()
            .await
            .context("Failed to create Kubernetes credential provider")?,
    );

    info!("Kubernetes credential provider initialized");

    let metrics = Arc::new(StorageMetrics::new());

    // Create mount manager
    let mount_manager = Arc::new(MountManager::new(
        args.fuse_base_path.clone(),
        credential_provider,
        metrics.clone(),
    ));

    info!("Mount manager initialized");

    // Start namespace watcher
    let (namespace_watcher, mut watcher_ready_rx) = NamespaceWatcher::new(mount_manager.clone());
    let watcher_mount_manager = mount_manager.clone();

    let watcher_handle = tokio::spawn(async move {
        if let Err(e) = namespace_watcher.run().await {
            tracing::error!("Namespace watcher error: {}", e);
        }
    });

    // Wait for watcher to be ready
    if watcher_ready_rx.changed().await.is_ok() && *watcher_ready_rx.borrow() {
        info!("Namespace watcher ready");
    }

    // Create per-namespace metrics store
    let namespace_metrics = PerNamespaceMetricsStore::new();

    // Start HTTP server for mount lifecycle management
    let http_addr = format!("0.0.0.0:{}", args.http_port);
    info!("Starting HTTP server at: {}", http_addr);

    let http_server = DaemonHttpServer::new(watcher_mount_manager, metrics, namespace_metrics);

    // Run HTTP server in a separate task
    let http_handle = tokio::spawn(async move {
        if let Err(e) = http_server.serve(&http_addr).await {
            tracing::error!("HTTP server error: {}", e);
        }
    });

    info!("Daemon ready. Mount/unmount namespaces via HTTP API:");
    info!("  POST /mounts/{{namespace}} - Create mount");
    info!("  DELETE /mounts/{{namespace}} - Destroy mount");
    info!("  GET /mounts - List all mounts");
    info!("  GET /mounts/{{namespace}} - Get mount details");
    info!("Namespace watcher active - mounts created automatically for u-* namespaces");

    wait_for_shutdown().await;
    info!("Received shutdown signal, shutting down...");

    // Abort watcher first
    watcher_handle.abort();

    // Shutdown mount manager (unmounts all namespaces)
    if let Err(e) = mount_manager.shutdown().await {
        tracing::warn!("Failed to shutdown mount manager: {}", e);
    }

    // Abort HTTP server
    http_handle.abort();

    info!("Basilica Storage Daemon stopped");

    Ok(())
}

async fn wait_for_shutdown() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut sigterm =
            signal(SignalKind::terminate()).expect("Failed to register SIGTERM handler");

        tokio::select! {
            _ = tokio::signal::ctrl_c() => {
                info!("Received SIGINT");
            }
            _ = sigterm.recv() => {
                info!("Received SIGTERM");
            }
        }
    }

    #[cfg(not(unix))]
    {
        tokio::signal::ctrl_c()
            .await
            .expect("Failed to listen for Ctrl+C");
        info!("Received SIGINT");
    }
}
