use bytes::Bytes;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::fs;
use tokio::io::AsyncReadExt;
use tracing::{debug, info, warn};

use crate::backend::StorageBackend;
use crate::error::{Result, StorageError};

/// Type alias for file information: (relative_path, full_path, size)
type FileInfo = (String, PathBuf, u64);

/// Type alias for boxed future returning file list
type BoxedFileFuture<'a> = std::pin::Pin<Box<dyn std::future::Future<Output = Result<Vec<FileInfo>>> + Send + 'a>>;

/// Metadata for a snapshot
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotMetadata {
    /// Unique snapshot ID
    pub snapshot_id: String,

    /// Job ID this snapshot belongs to
    pub job_id: String,

    /// Timestamp when snapshot was created
    pub created_at: String,

    /// Total size of the snapshot in bytes
    pub total_size: u64,

    /// Number of files in the snapshot
    pub file_count: usize,

    /// List of files in the snapshot
    pub files: Vec<FileEntry>,
}

/// Entry for a single file in the snapshot
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileEntry {
    /// Relative path within the snapshot
    pub path: String,

    /// File size in bytes
    pub size: u64,

    /// Object storage key for this file
    pub storage_key: String,
}

/// Manages snapshots of job working directories
pub struct SnapshotManager {
    backend: Arc<dyn StorageBackend>,
}

impl SnapshotManager {
    /// Create a new snapshot manager with the given storage backend
    pub fn new(backend: Arc<dyn StorageBackend>) -> Self {
        Self { backend }
    }

    /// Create a snapshot of a directory and upload to object storage
    ///
    /// # Arguments
    /// * `job_id` - The job ID this snapshot belongs to
    /// * `source_dir` - Local directory to snapshot
    /// * `snapshot_id` - Unique identifier for this snapshot
    ///
    /// # Returns
    /// Metadata about the created snapshot
    pub async fn create_snapshot(
        &self,
        job_id: &str,
        source_dir: &Path,
        snapshot_id: &str,
    ) -> Result<SnapshotMetadata> {
        info!(
            "Creating snapshot {} for job {} from {}",
            snapshot_id,
            job_id,
            source_dir.display()
        );

        if !source_dir.exists() {
            return Err(StorageError::InvalidPath(format!(
                "source directory does not exist: {}",
                source_dir.display()
            )));
        }

        // Collect all files recursively
        let files = self.collect_files(source_dir, source_dir).await?;
        let file_count = files.len();
        let mut total_size = 0u64;
        let mut file_entries = Vec::new();

        // Upload each file
        for (relative_path, full_path, size) in files {
            debug!("Uploading file: {} ({} bytes)", relative_path, size);

            // Read file contents
            let data = self.read_file(&full_path).await?;
            total_size += size;

            // Generate storage key: snapshots/{job_id}/{snapshot_id}/{relative_path}
            let storage_key = format!("snapshots/{}/{}/{}", job_id, snapshot_id, relative_path);

            // Upload to storage
            self.backend.put(&storage_key, data).await?;

            file_entries.push(FileEntry {
                path: relative_path,
                size,
                storage_key,
            });
        }

        // Create metadata
        let metadata = SnapshotMetadata {
            snapshot_id: snapshot_id.to_string(),
            job_id: job_id.to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            total_size,
            file_count,
            files: file_entries,
        };

        // Upload metadata
        let metadata_key = format!("snapshots/{}/{}/metadata.json", job_id, snapshot_id);
        let metadata_json = serde_json::to_vec(&metadata)?;
        self.backend
            .put(&metadata_key, Bytes::from(metadata_json))
            .await?;

        info!(
            "Snapshot {} created successfully: {} files, {} bytes",
            snapshot_id, file_count, total_size
        );

        Ok(metadata)
    }

    /// Restore a snapshot to a local directory
    ///
    /// # Arguments
    /// * `job_id` - The job ID
    /// * `snapshot_id` - The snapshot to restore
    /// * `dest_dir` - Local directory to restore to
    pub async fn restore_snapshot(
        &self,
        job_id: &str,
        snapshot_id: &str,
        dest_dir: &Path,
    ) -> Result<SnapshotMetadata> {
        info!(
            "Restoring snapshot {} for job {} to {}",
            snapshot_id,
            job_id,
            dest_dir.display()
        );

        // Download metadata
        let metadata_key = format!("snapshots/{}/{}/metadata.json", job_id, snapshot_id);
        let metadata_bytes = self.backend.get(&metadata_key).await?;
        let metadata: SnapshotMetadata = serde_json::from_slice(&metadata_bytes)?;

        // Create destination directory if it doesn't exist
        fs::create_dir_all(dest_dir).await?;

        // Restore each file
        for file_entry in &metadata.files {
            debug!("Restoring file: {}", file_entry.path);

            // Download file from storage
            let data = self.backend.get(&file_entry.storage_key).await?;

            // Write to local filesystem
            let dest_path = dest_dir.join(&file_entry.path);
            if let Some(parent) = dest_path.parent() {
                fs::create_dir_all(parent).await?;
            }
            fs::write(&dest_path, &data).await?;
        }

        info!(
            "Snapshot {} restored successfully: {} files",
            snapshot_id, metadata.file_count
        );

        Ok(metadata)
    }

    /// List all snapshots for a job
    pub async fn list_snapshots(&self, job_id: &str) -> Result<Vec<String>> {
        let prefix = format!("snapshots/{}/", job_id);
        let keys = self.backend.list(&prefix).await?;

        // Extract snapshot IDs from keys
        let mut snapshot_ids = std::collections::HashSet::new();
        for key in keys {
            if let Some(rest) = key.strip_prefix(&prefix) {
                if let Some(snapshot_id) = rest.split('/').next() {
                    snapshot_ids.insert(snapshot_id.to_string());
                }
            }
        }

        Ok(snapshot_ids.into_iter().collect())
    }

    /// Delete a snapshot
    pub async fn delete_snapshot(&self, job_id: &str, snapshot_id: &str) -> Result<()> {
        info!("Deleting snapshot {} for job {}", snapshot_id, job_id);

        // Get metadata to find all files
        let metadata_key = format!("snapshots/{}/{}/metadata.json", job_id, snapshot_id);
        let metadata_bytes = self.backend.get(&metadata_key).await?;
        let metadata: SnapshotMetadata = serde_json::from_slice(&metadata_bytes)?;

        // Delete all files
        for file_entry in &metadata.files {
            if let Err(e) = self.backend.delete(&file_entry.storage_key).await {
                warn!("Failed to delete file {}: {}", file_entry.storage_key, e);
            }
        }

        // Delete metadata
        self.backend.delete(&metadata_key).await?;

        info!("Snapshot {} deleted successfully", snapshot_id);
        Ok(())
    }

    /// Recursively collect all files in a directory
    fn collect_files<'a>(&'a self, dir: &'a Path, base: &'a Path) -> BoxedFileFuture<'a> {
        Box::pin(async move {
            Self::collect_files_impl(dir, base).await
        })
    }

    /// Implementation of recursive file collection
    fn collect_files_impl<'a>(dir: &'a Path, base: &'a Path) -> BoxedFileFuture<'a> {
        Box::pin(async move {
            let mut files = Vec::new();
            let mut entries = fs::read_dir(dir).await?;

            while let Some(entry) = entries.next_entry().await? {
                let path = entry.path();
                let metadata = entry.metadata().await?;

                if metadata.is_file() {
                    let relative_path = path
                        .strip_prefix(base)
                        .map_err(|e| StorageError::InvalidPath(e.to_string()))?
                        .to_string_lossy()
                        .to_string();

                    files.push((relative_path, path, metadata.len()));
                } else if metadata.is_dir() {
                    // Recurse into subdirectories
                    let subfiles = Self::collect_files_impl(&path, base).await?;
                    files.extend(subfiles);
                }
            }

            Ok(files)
        })
    }

    /// Read a file into memory
    async fn read_file(&self, path: &Path) -> Result<Bytes> {
        let mut file = fs::File::open(path).await?;
        let mut buffer = Vec::new();
        file.read_to_end(&mut buffer).await?;
        Ok(Bytes::from(buffer))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use crate::backend::StorageBackend;
    use std::collections::HashMap;
    use tokio::sync::RwLock;

    /// Mock storage backend for testing
    struct MockBackend {
        data: Arc<RwLock<HashMap<String, Bytes>>>,
    }

    impl MockBackend {
        fn new() -> Self {
            Self {
                data: Arc::new(RwLock::new(HashMap::new())),
            }
        }
    }

    #[async_trait]
    impl StorageBackend for MockBackend {
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
                .ok_or_else(|| StorageError::SnapshotNotFound(key.to_string()))
        }

        async fn exists(&self, key: &str) -> Result<bool> {
            Ok(self.data.read().await.contains_key(key))
        }

        async fn delete(&self, key: &str) -> Result<()> {
            self.data.write().await.remove(key);
            Ok(())
        }

        async fn list(&self, prefix: &str) -> Result<Vec<String>> {
            let data = self.data.read().await;
            Ok(data
                .keys()
                .filter(|k| k.starts_with(prefix))
                .cloned()
                .collect())
        }
    }

    #[tokio::test]
    async fn test_snapshot_create_and_restore() {
        let backend = Arc::new(MockBackend::new());
        let manager = SnapshotManager::new(backend.clone());

        // Create a temporary directory with test files
        let temp_dir = tempfile::tempdir().unwrap();
        let source_path = temp_dir.path();

        // Create test files
        fs::write(source_path.join("file1.txt"), b"content1")
            .await
            .unwrap();
        fs::create_dir(source_path.join("subdir")).await.unwrap();
        fs::write(source_path.join("subdir/file2.txt"), b"content2")
            .await
            .unwrap();

        // Create snapshot
        let metadata = manager
            .create_snapshot("job-123", source_path, "snap-001")
            .await
            .unwrap();

        assert_eq!(metadata.job_id, "job-123");
        assert_eq!(metadata.snapshot_id, "snap-001");
        assert_eq!(metadata.file_count, 2);

        // Restore to a new directory
        let restore_dir = tempfile::tempdir().unwrap();
        let restore_path = restore_dir.path();

        let restored_metadata = manager
            .restore_snapshot("job-123", "snap-001", restore_path)
            .await
            .unwrap();

        assert_eq!(restored_metadata.file_count, 2);

        // Verify files were restored
        let content1 = fs::read_to_string(restore_path.join("file1.txt"))
            .await
            .unwrap();
        assert_eq!(content1, "content1");

        let content2 = fs::read_to_string(restore_path.join("subdir/file2.txt"))
            .await
            .unwrap();
        assert_eq!(content2, "content2");
    }

    #[tokio::test]
    async fn test_list_snapshots() {
        let backend = Arc::new(MockBackend::new());
        let manager = SnapshotManager::new(backend.clone());

        let temp_dir = tempfile::tempdir().unwrap();
        let source_path = temp_dir.path();
        fs::write(source_path.join("test.txt"), b"test")
            .await
            .unwrap();

        // Create multiple snapshots
        manager
            .create_snapshot("job-123", source_path, "snap-001")
            .await
            .unwrap();
        manager
            .create_snapshot("job-123", source_path, "snap-002")
            .await
            .unwrap();

        // List snapshots
        let snapshots = manager.list_snapshots("job-123").await.unwrap();
        assert_eq!(snapshots.len(), 2);
        assert!(snapshots.contains(&"snap-001".to_string()));
        assert!(snapshots.contains(&"snap-002".to_string()));
    }

    #[tokio::test]
    async fn test_delete_snapshot() {
        let backend = Arc::new(MockBackend::new());
        let manager = SnapshotManager::new(backend.clone());

        let temp_dir = tempfile::tempdir().unwrap();
        let source_path = temp_dir.path();
        fs::write(source_path.join("test.txt"), b"test")
            .await
            .unwrap();

        // Create snapshot
        manager
            .create_snapshot("job-123", source_path, "snap-001")
            .await
            .unwrap();

        // Delete snapshot
        manager.delete_snapshot("job-123", "snap-001").await.unwrap();

        // Verify it's deleted
        let snapshots = manager.list_snapshots("job-123").await.unwrap();
        assert!(snapshots.is_empty());
    }
}
