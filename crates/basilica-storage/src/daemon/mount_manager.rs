//! Multi-tenant mount manager.
//!
//! Manages FUSE filesystem mounts for multiple user namespaces on a single node.
//! Each namespace gets an isolated mount at `/var/lib/basilica/fuse/{namespace}/`.

use crate::backend::{S3Backend, StorageBackend};
use crate::credentials::{CredentialError, CredentialProvider, StorageCredentials};
use crate::fuse::{BasilicaFS, SharedBasilicaFS};
use crate::metrics::StorageMetrics;
use chrono::{DateTime, Utc};
use fuser::MountOption;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::sync::{mpsc, oneshot, RwLock};

/// Default base path for FUSE mounts.
pub const DEFAULT_BASE_PATH: &str = "/var/lib/basilica/fuse";

/// Default sync interval in milliseconds.
const DEFAULT_SYNC_INTERVAL_MS: u64 = 1000;

/// Default quota in bytes (100GB).
const DEFAULT_QUOTA_BYTES: u64 = 100 * 1024 * 1024 * 1024;

/// Error type for mount operations.
#[derive(Debug, thiserror::Error)]
pub enum MountError {
    #[error("Security violation: {0}")]
    SecurityViolation(String),

    #[error("Mount already exists for namespace: {0}")]
    AlreadyMounted(String),

    #[error("Mount not found for namespace: {0}")]
    NotFound(String),

    #[error("Failed to get credentials: {0}")]
    CredentialError(#[from] CredentialError),

    #[error("Failed to create storage backend: {0}")]
    BackendError(String),

    #[error("Failed to create mount directory: {0}")]
    DirectoryError(String),

    #[error("Failed to mount filesystem: {0}")]
    MountFailed(String),

    #[error("Failed to unmount filesystem: {0}")]
    UnmountFailed(String),

    #[error("Mount worker is unavailable")]
    WorkerUnavailable,

    #[error("Mount operation timed out")]
    Timeout,
}

/// Status of a FUSE mount.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MountStatus {
    /// Mount is being created.
    Creating,
    /// Mount is active and healthy.
    Active,
    /// Mount is unhealthy or degraded.
    Unhealthy,
    /// Mount is being destroyed.
    Destroying,
}

/// Summary information about a mount (thread-safe, no raw pointers).
#[derive(Debug, Clone)]
pub struct MountInfo {
    pub namespace: String,
    pub mount_path: PathBuf,
    pub bucket: String,
    pub status: MountStatus,
    pub created_at: DateTime<Utc>,
    pub is_healthy: bool,
}

/// Internal mount entry with non-Send/Sync fields.
/// This is only accessed from the mount worker thread.
struct MountEntry {
    #[allow(dead_code)]
    fs: SharedBasilicaFS,
    #[allow(dead_code)]
    session: fuser::BackgroundSession,
}

/// Command sent to the mount worker.
enum MountCommand {
    Mount {
        namespace: String,
        respond: oneshot::Sender<Result<(), MountError>>,
    },
    Unmount {
        namespace: String,
        respond: oneshot::Sender<Result<(), MountError>>,
    },
    Shutdown,
}

/// Manager for multi-tenant FUSE mounts.
///
/// Thread-safe registry of active mounts with operations for
/// creating, destroying, and querying mounts.
///
/// The manager uses a dedicated worker thread for mount operations
/// because `fuser::BackgroundSession` contains raw pointers that
/// are not `Send`/`Sync`. The HTTP API only reads mount metadata
/// which is stored in a `Sync`-safe structure.
pub struct MountManager<P: CredentialProvider> {
    /// Mount metadata (thread-safe for reading).
    mount_info: Arc<RwLock<HashMap<String, MountInfo>>>,
    /// Command channel to mount worker.
    command_tx: mpsc::Sender<MountCommand>,
    /// Shared configuration and dependencies.
    base_path: PathBuf,
    credential_provider: Arc<P>,
    metrics: Arc<StorageMetrics>,
}

impl<P: CredentialProvider + 'static> MountManager<P> {
    /// Create a new mount manager and start the worker thread.
    pub fn new(
        base_path: PathBuf,
        credential_provider: Arc<P>,
        metrics: Arc<StorageMetrics>,
    ) -> Self {
        let mount_info = Arc::new(RwLock::new(HashMap::new()));
        let (command_tx, command_rx) = mpsc::channel(32);

        // Clone for the worker
        let worker_base_path = base_path.clone();
        let worker_credential_provider = credential_provider.clone();
        let worker_metrics = metrics.clone();
        let worker_mount_info = mount_info.clone();

        // Spawn worker thread for mount operations
        std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("Failed to create tokio runtime for mount worker");

            rt.block_on(async {
                mount_worker(
                    command_rx,
                    &worker_base_path,
                    worker_credential_provider,
                    worker_metrics,
                    worker_mount_info,
                )
                .await
            });
        });

        Self {
            mount_info,
            command_tx,
            base_path,
            credential_provider,
            metrics,
        }
    }

    /// Validate that the namespace is a user namespace.
    fn validate_namespace(namespace: &str) -> Result<(), MountError> {
        if !namespace.starts_with("u-") {
            return Err(MountError::SecurityViolation(format!(
                "Cannot create mount for non-user namespace '{}'. Only 'u-*' namespaces are allowed.",
                namespace
            )));
        }
        Ok(())
    }

    /// Create a mount for the given namespace.
    pub async fn mount_namespace(&self, namespace: &str) -> Result<(), MountError> {
        Self::validate_namespace(namespace)?;

        let (respond_tx, respond_rx) = oneshot::channel();
        self.command_tx
            .send(MountCommand::Mount {
                namespace: namespace.to_string(),
                respond: respond_tx,
            })
            .await
            .map_err(|_| MountError::WorkerUnavailable)?;

        respond_rx
            .await
            .map_err(|_| MountError::WorkerUnavailable)?
    }

    /// Unmount and remove the mount for the given namespace.
    pub async fn unmount_namespace(&self, namespace: &str) -> Result<(), MountError> {
        Self::validate_namespace(namespace)?;

        let (respond_tx, respond_rx) = oneshot::channel();
        self.command_tx
            .send(MountCommand::Unmount {
                namespace: namespace.to_string(),
                respond: respond_tx,
            })
            .await
            .map_err(|_| MountError::WorkerUnavailable)?;

        respond_rx
            .await
            .map_err(|_| MountError::WorkerUnavailable)?
    }

    /// List all active mounts.
    pub async fn list_mounts(&self) -> Vec<(String, MountStatus, PathBuf)> {
        let info = self.mount_info.read().await;
        info.iter()
            .map(|(ns, m)| (ns.clone(), m.status, m.mount_path.clone()))
            .collect()
    }

    /// Get mount status for a namespace.
    pub async fn get_mount_status(&self, namespace: &str) -> Option<MountStatus> {
        let info = self.mount_info.read().await;
        info.get(namespace).map(|m| m.status)
    }

    /// Check health of a specific mount.
    pub async fn check_mount_health(&self, namespace: &str) -> Option<bool> {
        let info = self.mount_info.read().await;
        info.get(namespace).map(|m| m.is_healthy)
    }

    /// Get detailed information about a mount.
    pub async fn get_mount_info(&self, namespace: &str) -> Option<MountInfo> {
        let info = self.mount_info.read().await;
        info.get(namespace).cloned()
    }

    /// Get the base path for mounts.
    pub fn base_path(&self) -> &PathBuf {
        &self.base_path
    }

    /// Get the credential provider.
    pub fn credential_provider(&self) -> &Arc<P> {
        &self.credential_provider
    }

    /// Get the metrics instance.
    pub fn metrics(&self) -> &Arc<StorageMetrics> {
        &self.metrics
    }

    /// Shutdown the mount manager and all mounts.
    pub async fn shutdown(&self) -> Result<(), MountError> {
        self.command_tx
            .send(MountCommand::Shutdown)
            .await
            .map_err(|_| MountError::WorkerUnavailable)
    }
}

/// Worker function that handles mount operations in a dedicated thread.
async fn mount_worker<P: CredentialProvider>(
    mut command_rx: mpsc::Receiver<MountCommand>,
    base_path: &Path,
    credential_provider: Arc<P>,
    metrics: Arc<StorageMetrics>,
    mount_info: Arc<RwLock<HashMap<String, MountInfo>>>,
) {
    // Store actual mount entries (non-Send/Sync) in this thread only
    let mut mounts: HashMap<String, MountEntry> = HashMap::new();

    while let Some(command) = command_rx.recv().await {
        match command {
            MountCommand::Mount { namespace, respond } => {
                let result = handle_mount(
                    &namespace,
                    base_path,
                    &credential_provider,
                    &metrics,
                    &mount_info,
                    &mut mounts,
                )
                .await;
                let _ = respond.send(result);
            }
            MountCommand::Unmount { namespace, respond } => {
                let result = handle_unmount(&namespace, &metrics, &mount_info, &mut mounts).await;
                let _ = respond.send(result);
            }
            MountCommand::Shutdown => {
                tracing::info!("Mount worker shutting down");
                // Unmount all namespaces
                let namespaces: Vec<String> = mounts.keys().cloned().collect();
                for namespace in namespaces {
                    if let Err(e) =
                        handle_unmount(&namespace, &metrics, &mount_info, &mut mounts).await
                    {
                        tracing::warn!(namespace = %namespace, error = %e, "Failed to unmount during shutdown");
                    }
                }
                break;
            }
        }
    }
}

async fn handle_mount<P: CredentialProvider>(
    namespace: &str,
    base_path: &Path,
    credential_provider: &Arc<P>,
    metrics: &Arc<StorageMetrics>,
    mount_info: &Arc<RwLock<HashMap<String, MountInfo>>>,
    mounts: &mut HashMap<String, MountEntry>,
) -> Result<(), MountError> {
    // Check if already mounted
    if mounts.contains_key(namespace) {
        tracing::debug!(namespace = %namespace, "Mount already exists");
        return Err(MountError::AlreadyMounted(namespace.to_string()));
    }

    tracing::info!(
        target: "security_audit",
        event_type = "mount_request",
        severity = "info",
        namespace = %namespace,
        "Creating FUSE mount for namespace"
    );

    // Get credentials from namespace secret
    let credentials = credential_provider.get_credentials(namespace).await?;

    // Create mount directory
    let mount_path = base_path.join(namespace);
    create_mount_directory(&mount_path)?;

    // Create storage backend
    let storage = create_storage_backend(&credentials).await?;

    // Create and mount filesystem
    let (fs, session) =
        create_and_mount_filesystem(namespace, &mount_path, storage, &credentials, metrics).await?;

    // Create mount info
    let info = MountInfo {
        namespace: namespace.to_string(),
        mount_path: mount_path.clone(),
        bucket: credentials.bucket.clone(),
        status: MountStatus::Active,
        created_at: Utc::now(),
        is_healthy: true,
    };

    // Store mount entry
    let entry = MountEntry { fs, session };
    mounts.insert(namespace.to_string(), entry);

    // Update shared mount info
    {
        let mut info_map = mount_info.write().await;
        info_map.insert(namespace.to_string(), info);
    }

    metrics.record_mount_created();

    tracing::info!(
        target: "security_audit",
        event_type = "mount_created",
        severity = "info",
        namespace = %namespace,
        mount_path = %mount_path.display(),
        bucket = %credentials.bucket,
        "FUSE mount successfully created for namespace"
    );

    Ok(())
}

async fn handle_unmount(
    namespace: &str,
    metrics: &Arc<StorageMetrics>,
    mount_info: &Arc<RwLock<HashMap<String, MountInfo>>>,
    mounts: &mut HashMap<String, MountEntry>,
) -> Result<(), MountError> {
    let entry = mounts.remove(namespace);
    let entry = match entry {
        Some(e) => e,
        None => return Err(MountError::NotFound(namespace.to_string())),
    };

    tracing::info!(
        target: "security_audit",
        event_type = "unmount_request",
        severity = "info",
        namespace = %namespace,
        "Destroying FUSE mount for namespace"
    );

    // Flush dirty pages before unmounting
    if let Err(e) = entry.fs.shutdown().await {
        tracing::warn!(
            namespace = %namespace,
            error = %e,
            "Failed to flush dirty pages during unmount"
        );
    }

    // Remove from shared mount info
    {
        let mut info_map = mount_info.write().await;
        info_map.remove(namespace);
    }

    // Drop the session to unmount (happens automatically when entry is dropped)
    drop(entry);

    metrics.record_mount_destroyed();

    tracing::info!(
        target: "security_audit",
        event_type = "mount_destroyed",
        severity = "info",
        namespace = %namespace,
        "FUSE mount successfully destroyed for namespace"
    );

    Ok(())
}

/// Create mount directory with restrictive permissions.
/// Handles stale FUSE mounts by unmounting and recreating the directory.
fn create_mount_directory(path: &PathBuf) -> Result<(), MountError> {
    // First, try to detect and clean up stale mounts
    // Note: path.exists() may return false for stale mounts, so we check accessibility
    match std::fs::read_dir(path) {
        Ok(_) => {
            // Directory exists and is accessible - can reuse it
            tracing::debug!(path = %path.display(), "Mount directory exists and is accessible");
        }
        Err(e) => {
            let os_err = e.raw_os_error();
            if os_err == Some(107) || os_err == Some(5) {
                // ENOTCONN (107) or EIO (5) - stale mount
                tracing::warn!(
                    path = %path.display(),
                    "Detected stale FUSE mount, attempting cleanup"
                );
                cleanup_stale_mount(path)?;
                // Create fresh directory after cleanup
                std::fs::create_dir_all(path).map_err(|e| {
                    MountError::DirectoryError(format!(
                        "Failed to create directory '{}': {}",
                        path.display(),
                        e
                    ))
                })?;
            } else if os_err == Some(2) {
                // ENOENT - directory doesn't exist, create it
                std::fs::create_dir_all(path).map_err(|e| {
                    MountError::DirectoryError(format!(
                        "Failed to create directory '{}': {}",
                        path.display(),
                        e
                    ))
                })?;
            } else {
                // Other error - try to create anyway
                tracing::debug!(
                    path = %path.display(),
                    error = %e,
                    os_error = ?os_err,
                    "Unexpected error checking mount directory, attempting create"
                );
                if let Err(create_err) = std::fs::create_dir_all(path) {
                    // If creation fails with EEXIST, the directory exists but was inaccessible
                    // This could be a stale mount - try cleanup
                    if create_err.raw_os_error() == Some(17) {
                        tracing::warn!(
                            path = %path.display(),
                            "Directory exists but inaccessible, attempting stale mount cleanup"
                        );
                        cleanup_stale_mount(path)?;
                        std::fs::create_dir_all(path).map_err(|e| {
                            MountError::DirectoryError(format!(
                                "Failed to create directory '{}': {}",
                                path.display(),
                                e
                            ))
                        })?;
                    } else {
                        return Err(MountError::DirectoryError(format!(
                            "Failed to create directory '{}': {}",
                            path.display(),
                            create_err
                        )));
                    }
                }
            }
        }
    }

    // Set restrictive permissions (0700 = rwx------)
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(path)
            .map_err(|e| {
                MountError::DirectoryError(format!(
                    "Failed to read metadata for '{}': {}",
                    path.display(),
                    e
                ))
            })?
            .permissions();
        perms.set_mode(0o700);
        std::fs::set_permissions(path, perms).map_err(|e| {
            MountError::DirectoryError(format!(
                "Failed to set permissions on '{}': {}",
                path.display(),
                e
            ))
        })?;
    }

    Ok(())
}

/// Clean up a stale FUSE mount by lazy unmounting and removing the directory.
fn cleanup_stale_mount(path: &PathBuf) -> Result<(), MountError> {
    // Try lazy unmount first (fusermount -uz)
    let unmount_result = std::process::Command::new("fusermount")
        .args(["-uz", &path.to_string_lossy()])
        .output();

    match unmount_result {
        Ok(output) => {
            if output.status.success() {
                tracing::info!(
                    path = %path.display(),
                    "Successfully unmounted stale FUSE mount"
                );
            } else {
                tracing::debug!(
                    path = %path.display(),
                    stderr = %String::from_utf8_lossy(&output.stderr),
                    "fusermount returned non-zero (mount may already be gone)"
                );
            }
        }
        Err(e) => {
            tracing::debug!(
                path = %path.display(),
                error = %e,
                "fusermount command failed, trying umount"
            );
            // Fallback to umount -l
            let _ = std::process::Command::new("umount")
                .args(["-l", &path.to_string_lossy()])
                .output();
        }
    }

    // Remove the directory if it still exists
    if path.exists() {
        std::fs::remove_dir(path).map_err(|e| {
            MountError::DirectoryError(format!(
                "Failed to remove stale mount directory '{}': {}",
                path.display(),
                e
            ))
        })?;
        tracing::info!(
            path = %path.display(),
            "Removed stale mount directory"
        );
    }

    Ok(())
}

/// Create storage backend from credentials.
async fn create_storage_backend(
    credentials: &StorageCredentials,
) -> Result<Arc<dyn StorageBackend>, MountError> {
    let config = crate::config::StorageConfig::r2(
        &extract_account_id(&credentials.endpoint)?,
        &credentials.access_key_id,
        &credentials.secret_access_key,
        &credentials.bucket,
    );

    let backend = S3Backend::from_config(&config)
        .await
        .map_err(|e| MountError::BackendError(e.to_string()))?;

    Ok(Arc::new(backend))
}

/// Create and mount the FUSE filesystem.
async fn create_and_mount_filesystem(
    namespace: &str,
    mount_path: &PathBuf,
    storage: Arc<dyn StorageBackend>,
    credentials: &StorageCredentials,
    metrics: &Arc<StorageMetrics>,
) -> Result<(SharedBasilicaFS, fuser::BackgroundSession), MountError> {
    let fs = BasilicaFS::new(
        namespace.to_string(),
        namespace.to_string(),
        storage,
        DEFAULT_SYNC_INTERVAL_MS,
        credentials.cache_size_mb,
        DEFAULT_QUOTA_BYTES,
        metrics.clone(),
    );

    // Start background sync worker
    fs.start_sync_worker().await;

    let shared_fs = SharedBasilicaFS::new(fs);

    // Prepare mount options
    let mount_options = vec![
        MountOption::FSName("basilica-storage".to_string()),
        MountOption::RW,
        MountOption::AllowOther,
    ];

    // Mount the filesystem
    let session = fuser::spawn_mount2(shared_fs.clone(), mount_path, &mount_options)
        .map_err(|e| MountError::MountFailed(e.to_string()))?;

    // Create readiness marker
    let ready_file = mount_path.join(".fuse_ready");
    if let Err(e) = std::fs::write(&ready_file, "ready") {
        tracing::warn!(
            namespace = %namespace,
            error = %e,
            "Failed to create readiness marker"
        );
    }

    Ok((shared_fs, session))
}

/// Extract account ID from R2 endpoint URL.
fn extract_account_id(endpoint: &str) -> Result<String, MountError> {
    // Expected format: https://<account_id>.r2.cloudflarestorage.com
    let url_without_scheme = endpoint
        .strip_prefix("https://")
        .or_else(|| endpoint.strip_prefix("http://"))
        .unwrap_or(endpoint);

    url_without_scheme
        .split('.')
        .next()
        .map(|s| s.to_string())
        .ok_or_else(|| {
            MountError::BackendError("Failed to extract account ID from endpoint".to_string())
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn validate_namespace(namespace: &str) -> Result<(), MountError> {
        MountManager::<MockCredentialProvider>::validate_namespace(namespace)
    }

    #[test]
    fn test_validate_namespace_valid() {
        assert!(validate_namespace("u-alice").is_ok());
        assert!(validate_namespace("u-bob-123").is_ok());
    }

    #[test]
    fn test_validate_namespace_invalid() {
        assert!(validate_namespace("default").is_err());
        assert!(validate_namespace("kube-system").is_err());
        assert!(validate_namespace("basilica-storage").is_err());
    }

    #[test]
    fn test_extract_account_id() {
        let result = extract_account_id("https://abc123.r2.cloudflarestorage.com");
        assert_eq!(result.unwrap(), "abc123");

        let result = extract_account_id("http://abc123.r2.cloudflarestorage.com");
        assert_eq!(result.unwrap(), "abc123");
    }

    // Mock credential provider for tests
    struct MockCredentialProvider;

    #[async_trait::async_trait]
    impl CredentialProvider for MockCredentialProvider {
        async fn get_credentials(
            &self,
            _namespace: &str,
        ) -> Result<StorageCredentials, CredentialError> {
            Ok(StorageCredentials {
                access_key_id: "test-key".to_string(),
                secret_access_key: "test-secret".to_string(),
                endpoint: "https://test.r2.cloudflarestorage.com".to_string(),
                bucket: "test-bucket".to_string(),
                region: None,
                cache_size_mb: 1024,
            })
        }
    }
}
