//! Background worker that syncs dirty files to object storage
//!
//! ## Design
//!
//! The sync worker implements file-level synchronization with these features:
//!
//! 1. **File-level sync**: Uploads complete files instead of individual regions,
//!    reducing S3 operations and improving efficiency.
//!
//! 2. **Quiet period**: Waits for write quiescence before syncing to allow
//!    multiple small writes to coalesce into fewer uploads.
//!
//! 3. **Parallel uploads**: Uses bounded concurrency for uploading multiple
//!    files simultaneously, improving throughput.
//!
//! 4. **Tri-state page tracking**: Coordinates with PageCache's PageState
//!    (Clean/Dirty/Syncing) to prevent data loss during concurrent writes.

use crate::backend::StorageBackend;
use crate::fuse::cache::PageCache;
use futures::stream::{self, StreamExt};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;
use tracing::{debug, error, info, warn};

/// Timeout for individual file uploads
const STORAGE_OP_TIMEOUT_SECS: u64 = 60;

/// Default quiet period before syncing a file
pub const DEFAULT_QUIET_PERIOD_MS: u64 = 500;

/// Default maximum concurrent uploads
pub const DEFAULT_MAX_CONCURRENT_UPLOADS: usize = 4;

/// Background worker for syncing dirty files to object storage
pub struct SyncWorker {
    /// Namespace (used as prefix in object storage)
    namespace: String,

    /// Experiment ID (used as prefix in object storage)
    experiment_id: String,

    /// Storage backend
    storage: Arc<dyn StorageBackend>,

    /// Page cache (source of truth for dirty state)
    cache: Arc<RwLock<PageCache>>,

    /// How often to check for dirty files
    sync_interval: Duration,

    /// How long to wait after last write before syncing a file
    quiet_period: Duration,

    /// Maximum concurrent file uploads
    max_concurrent_uploads: usize,

    /// Whether the worker is running
    running: Arc<tokio::sync::RwLock<bool>>,
}

impl SyncWorker {
    /// Create a new sync worker
    pub fn new(
        namespace: String,
        experiment_id: String,
        storage: Arc<dyn StorageBackend>,
        cache: Arc<RwLock<PageCache>>,
        sync_interval_ms: u64,
    ) -> Self {
        Self::with_options(
            namespace,
            experiment_id,
            storage,
            cache,
            sync_interval_ms,
            DEFAULT_QUIET_PERIOD_MS,
            DEFAULT_MAX_CONCURRENT_UPLOADS,
        )
    }

    /// Create a new sync worker with custom options
    pub fn with_options(
        namespace: String,
        experiment_id: String,
        storage: Arc<dyn StorageBackend>,
        cache: Arc<RwLock<PageCache>>,
        sync_interval_ms: u64,
        quiet_period_ms: u64,
        max_concurrent_uploads: usize,
    ) -> Self {
        Self {
            namespace,
            experiment_id,
            storage,
            cache,
            sync_interval: Duration::from_millis(sync_interval_ms),
            quiet_period: Duration::from_millis(quiet_period_ms),
            max_concurrent_uploads: max_concurrent_uploads.max(1),
            running: Arc::new(tokio::sync::RwLock::new(false)),
        }
    }

    /// Start the sync worker background task
    pub async fn start(&self) -> tokio::task::JoinHandle<()> {
        let namespace = self.namespace.clone();
        let experiment_id = self.experiment_id.clone();
        let storage = self.storage.clone();
        let cache = self.cache.clone();
        let sync_interval = self.sync_interval;
        let quiet_period = self.quiet_period;
        let max_concurrent = self.max_concurrent_uploads;
        let running = self.running.clone();

        *running.write().await = true;

        tokio::spawn(async move {
            info!(
                "Starting sync worker for {}/{} (interval={}ms, quiet={}ms, concurrent={})",
                namespace,
                experiment_id,
                sync_interval.as_millis(),
                quiet_period.as_millis(),
                max_concurrent
            );

            while *running.read().await {
                tokio::time::sleep(sync_interval).await;

                // Get files ready to sync (past quiet period)
                let dirty_files = {
                    let cache_guard = cache.read().await;
                    cache_guard.get_dirty_files(quiet_period).await
                };

                if dirty_files.is_empty() {
                    debug!("No files ready to sync");
                    continue;
                }

                info!("Syncing {} files", dirty_files.len());

                // Sync files in parallel (up to max_concurrent)
                let results: Vec<_> = stream::iter(dirty_files)
                    .map(|file| {
                        let cache = cache.clone();
                        let storage = storage.clone();
                        let ns = namespace.clone();
                        let exp = experiment_id.clone();

                        async move {
                            let path = file.path.clone();
                            let result =
                                Self::sync_file(&ns, &exp, &storage, &cache, &file.path).await;
                            (path, result)
                        }
                    })
                    .buffer_unordered(max_concurrent)
                    .collect()
                    .await;

                // Log results
                let success_count = results.iter().filter(|(_, r)| r.is_ok()).count();
                let fail_count = results.len() - success_count;

                for (path, result) in &results {
                    if let Err(e) = result {
                        error!("Failed to sync {}: {}", path, e);
                    }
                }

                if fail_count > 0 {
                    warn!(
                        "Sync cycle complete: {} succeeded, {} failed",
                        success_count, fail_count
                    );
                } else {
                    info!("Sync cycle complete: {} files synced", success_count);
                }
            }

            info!("Sync worker stopped");
        })
    }

    /// Stop the sync worker
    pub async fn stop(&self) {
        *self.running.write().await = false;
    }

    /// Sync a single file to object storage
    ///
    /// This method:
    /// 1. Marks dirty pages as Syncing
    /// 2. Gets a snapshot of the complete file data
    /// 3. Uploads to object storage
    /// 4. Marks pages as Clean (success) or Dirty (failure)
    async fn sync_file(
        namespace: &str,
        experiment_id: &str,
        storage: &Arc<dyn StorageBackend>,
        cache: &Arc<RwLock<PageCache>>,
        path: &str,
    ) -> Result<(), String> {
        debug!("Syncing file: {}", path);

        // Step 1: Mark pages as Syncing and get file snapshot
        let snapshot = {
            let cache_guard = cache.read().await;
            cache_guard.mark_file_syncing(path).await
        };

        let Some(snapshot) = snapshot else {
            return Err(format!("File not found in cache: {}", path));
        };

        // Skip empty files (no data to upload)
        if snapshot.data.is_empty() {
            debug!("Skipping empty file: {}", path);
            // Mark as clean since there's nothing to sync
            let cache_guard = cache.read().await;
            cache_guard
                .mark_sync_complete(path, &snapshot.syncing_offsets, true)
                .await;
            return Ok(());
        }

        // Step 2: Upload to storage
        let key = format!(
            "{}/{}/{}",
            namespace,
            experiment_id,
            path.trim_start_matches('/')
        );

        debug!(
            "Uploading {} bytes to {} for file {}",
            snapshot.data.len(),
            key,
            path
        );

        let timeout = Duration::from_secs(STORAGE_OP_TIMEOUT_SECS);
        let result = tokio::time::timeout(timeout, storage.put(&key, snapshot.data)).await;

        // Step 3: Update page states based on result
        let success = match result {
            Ok(Ok(_)) => {
                debug!("Successfully synced: {}", path);
                true
            }
            Ok(Err(e)) => {
                warn!("Failed to sync {}: {}", path, e);
                false
            }
            Err(_) => {
                warn!("Timeout syncing {} after {}s", path, STORAGE_OP_TIMEOUT_SECS);
                false
            }
        };

        // Step 4: Mark pages as Clean or Dirty
        {
            let cache_guard = cache.read().await;
            cache_guard
                .mark_sync_complete(path, &snapshot.syncing_offsets, success)
                .await;
        }

        if success {
            Ok(())
        } else {
            Err(format!("Sync failed for {}", path))
        }
    }

    /// Force sync all dirty files immediately (for graceful shutdown)
    ///
    /// This ignores the quiet period and syncs all dirty files.
    pub async fn flush_all(&self) -> Result<(), String> {
        info!(
            "Flushing all dirty files for {}/{}",
            self.namespace, self.experiment_id
        );

        // Get all dirty files (zero quiet period = immediate)
        let dirty_files = {
            let cache_guard = self.cache.read().await;
            cache_guard.get_dirty_files(Duration::ZERO).await
        };

        if dirty_files.is_empty() {
            info!("No dirty files to flush");
            return Ok(());
        }

        info!("Flushing {} dirty files", dirty_files.len());

        // Sync all files in parallel
        let results: Vec<_> = stream::iter(dirty_files)
            .map(|file| {
                let cache = self.cache.clone();
                let storage = self.storage.clone();
                let ns = self.namespace.clone();
                let exp = self.experiment_id.clone();

                async move {
                    let path = file.path.clone();
                    let result = Self::sync_file(&ns, &exp, &storage, &cache, &file.path).await;
                    (path, result)
                }
            })
            .buffer_unordered(self.max_concurrent_uploads)
            .collect()
            .await;

        // Check for failures
        let failures: Vec<_> = results
            .iter()
            .filter_map(|(path, r)| r.as_ref().err().map(|e| format!("{}: {}", path, e)))
            .collect();

        if failures.is_empty() {
            info!("All dirty files flushed successfully");
            Ok(())
        } else {
            let msg = format!("Failed to flush {} files: {}", failures.len(), failures.join(", "));
            error!("{}", msg);
            Err(msg)
        }
    }

    /// Get sync statistics
    pub async fn get_stats(&self) -> SyncStats {
        let cache_guard = self.cache.read().await;
        let dirty_count = cache_guard.dirty_page_count().await;
        let has_dirty = cache_guard.has_dirty_pages().await;

        SyncStats {
            dirty_page_count: dirty_count,
            has_dirty_pages: has_dirty,
            sync_interval_ms: self.sync_interval.as_millis() as u64,
            quiet_period_ms: self.quiet_period.as_millis() as u64,
            max_concurrent_uploads: self.max_concurrent_uploads,
            running: *self.running.read().await,
        }
    }
}

/// Sync worker statistics
#[derive(Debug, Clone)]
pub struct SyncStats {
    /// Number of dirty pages across all files
    pub dirty_page_count: usize,
    /// Whether any file has dirty pages
    pub has_dirty_pages: bool,
    /// Sync check interval in milliseconds
    pub sync_interval_ms: u64,
    /// Quiet period before syncing in milliseconds
    pub quiet_period_ms: u64,
    /// Maximum concurrent uploads
    pub max_concurrent_uploads: usize,
    /// Whether the worker is running
    pub running: bool,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::StorageBackend;
    use crate::error::Result;
    use async_trait::async_trait;
    use bytes::Bytes;
    use std::collections::HashMap;
    use tokio::sync::RwLock as TokioRwLock;

    /// Mock storage backend for testing
    struct MockStorage {
        data: Arc<TokioRwLock<HashMap<String, Bytes>>>,
    }

    impl MockStorage {
        fn new() -> Self {
            Self {
                data: Arc::new(TokioRwLock::new(HashMap::new())),
            }
        }

        async fn get_data(&self, key: &str) -> Option<Bytes> {
            self.data.read().await.get(key).cloned()
        }
    }

    #[async_trait]
    impl StorageBackend for MockStorage {
        async fn put(&self, key: &str, data: Bytes) -> Result<()> {
            self.data.write().await.insert(key.to_string(), data);
            Ok(())
        }

        async fn get(&self, key: &str) -> Result<Bytes> {
            self.data
                .read()
                .await
                .get(key)
                .cloned()
                .ok_or_else(|| crate::error::StorageError::SnapshotNotFound(key.to_string()))
        }

        async fn exists(&self, key: &str) -> Result<bool> {
            Ok(self.data.read().await.contains_key(key))
        }

        async fn delete(&self, key: &str) -> Result<()> {
            self.data.write().await.remove(key);
            Ok(())
        }

        async fn list(&self, _prefix: &str) -> Result<Vec<String>> {
            Ok(self.data.read().await.keys().cloned().collect())
        }
    }

    #[tokio::test]
    async fn test_sync_worker_file_level() {
        let storage = Arc::new(MockStorage::new());
        let cache = Arc::new(RwLock::new(PageCache::new(10)));

        // Write some data to cache
        let data = b"Hello, World!";
        cache.write().await.write("/test.txt", 0, data).await.unwrap();

        // Create sync worker with zero quiet period for immediate sync
        let worker = SyncWorker::with_options(
            "u-test".to_string(),
            "exp-123".to_string(),
            storage.clone(),
            cache.clone(),
            50, // 50ms sync interval
            0,  // 0ms quiet period (immediate)
            4,
        );

        let handle = worker.start().await;

        // Wait for sync
        tokio::time::sleep(Duration::from_millis(150)).await;

        // Stop worker
        worker.stop().await;
        handle.abort();

        // Verify complete file was synced
        let synced_data = storage.get_data("u-test/exp-123/test.txt").await;
        assert!(synced_data.is_some());

        // File should contain the complete content
        let synced = synced_data.unwrap();
        assert_eq!(&synced[..data.len()], data);
    }

    #[tokio::test]
    async fn test_sync_worker_quiet_period() {
        let storage = Arc::new(MockStorage::new());
        let cache = Arc::new(RwLock::new(PageCache::new(10)));

        // Write some data
        cache.write().await.write("/test.txt", 0, b"hello").await.unwrap();

        // Create worker with 200ms quiet period
        let worker = SyncWorker::with_options(
            "u-test".to_string(),
            "exp-123".to_string(),
            storage.clone(),
            cache.clone(),
            50,  // 50ms sync interval
            200, // 200ms quiet period
            4,
        );

        let handle = worker.start().await;

        // Check immediately - should not be synced yet (quiet period)
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(storage.get_data("u-test/exp-123/test.txt").await.is_none());

        // Wait for quiet period + sync interval
        tokio::time::sleep(Duration::from_millis(200)).await;

        // Now should be synced
        assert!(storage.get_data("u-test/exp-123/test.txt").await.is_some());

        worker.stop().await;
        handle.abort();
    }

    #[tokio::test]
    async fn test_flush_all() {
        let storage = Arc::new(MockStorage::new());
        let cache = Arc::new(RwLock::new(PageCache::new(10)));

        // Write multiple files
        for i in 0..5 {
            let path = format!("/file-{}.txt", i);
            let data = format!("content-{}", i);
            cache.write().await.write(&path, 0, data.as_bytes()).await.unwrap();
        }

        // Create worker (don't start)
        let worker = SyncWorker::new(
            "u-test".to_string(),
            "exp-123".to_string(),
            storage.clone(),
            cache.clone(),
            1000,
        );

        // Flush all (ignores quiet period)
        worker.flush_all().await.unwrap();

        // Verify all files were synced
        for i in 0..5 {
            let key = format!("u-test/exp-123/file-{}.txt", i);
            let data = storage.get_data(&key).await;
            assert!(data.is_some(), "File {} not synced", i);
        }

        // Verify pages marked clean
        let stats = worker.get_stats().await;
        assert!(!stats.has_dirty_pages);
    }

    #[tokio::test]
    async fn test_sync_marks_pages_clean() {
        let storage = Arc::new(MockStorage::new());
        let cache = Arc::new(RwLock::new(PageCache::new(10)));

        cache.write().await.write("/test.txt", 0, b"hello").await.unwrap();

        // Verify page is dirty
        assert!(cache.read().await.has_dirty_pages().await);

        // Flush
        let worker = SyncWorker::with_options(
            "u-test".to_string(),
            "exp-123".to_string(),
            storage.clone(),
            cache.clone(),
            1000,
            0,
            4,
        );
        worker.flush_all().await.unwrap();

        // Verify page is clean
        assert!(!cache.read().await.has_dirty_pages().await);
    }

    #[tokio::test]
    async fn test_get_stats() {
        let storage = Arc::new(MockStorage::new());
        let cache = Arc::new(RwLock::new(PageCache::new(10)));

        let worker = SyncWorker::with_options(
            "u-test".to_string(),
            "exp-123".to_string(),
            storage,
            cache.clone(),
            1000,
            500,
            8,
        );

        let stats = worker.get_stats().await;
        assert_eq!(stats.sync_interval_ms, 1000);
        assert_eq!(stats.quiet_period_ms, 500);
        assert_eq!(stats.max_concurrent_uploads, 8);
        assert!(!stats.running);
        assert!(!stats.has_dirty_pages);

        // Write some data
        cache.write().await.write("/test.txt", 0, b"hello").await.unwrap();

        let stats = worker.get_stats().await;
        assert!(stats.has_dirty_pages);
        assert_eq!(stats.dirty_page_count, 1);
    }
}
