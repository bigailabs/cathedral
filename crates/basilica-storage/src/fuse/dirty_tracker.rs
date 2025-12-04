//! File-level dirty tracking for FUSE filesystem
//!
//! ## Design Change (v2)
//!
//! The dirty page state is now unified in PageCache with tri-state pages:
//! - Clean: Safe to evict
//! - Dirty: Has local modifications
//! - Syncing: Upload in progress
//!
//! This tracker maintains a simple set of files that have been modified,
//! providing quick lookup without duplicating the dirty state.
//!
//! The actual sync coordination happens through PageCache methods:
//! - get_dirty_files()
//! - mark_file_syncing()
//! - mark_sync_complete()

use std::collections::HashSet;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::debug;

/// Tracks files that have been modified
///
/// This is a lightweight tracker that maintains a set of file paths
/// that may have dirty pages. The actual dirty state is stored in
/// PageCache.Page.state.
pub struct DirtyFileTracker {
    /// Files that have been written to (may or may not still be dirty)
    modified_files: Arc<RwLock<HashSet<String>>>,
}

impl DirtyFileTracker {
    /// Create a new dirty file tracker
    pub fn new() -> Self {
        Self {
            modified_files: Arc::new(RwLock::new(HashSet::new())),
        }
    }

    /// Mark a file as potentially dirty (called on write)
    pub async fn mark_modified(&self, path: &str) {
        self.modified_files.write().await.insert(path.to_string());
        debug!("Marked modified: {}", path);
    }

    /// Remove a file from tracking (called on delete or after full sync verification)
    pub async fn remove(&self, path: &str) {
        self.modified_files.write().await.remove(path);
        debug!("Removed from tracking: {}", path);
    }

    /// Get all files that might be dirty
    pub async fn get_modified_files(&self) -> Vec<String> {
        self.modified_files.read().await.iter().cloned().collect()
    }

    /// Check if a file is being tracked as potentially dirty
    pub async fn is_modified(&self, path: &str) -> bool {
        self.modified_files.read().await.contains(path)
    }

    /// Get count of tracked files
    pub async fn count(&self) -> usize {
        self.modified_files.read().await.len()
    }

    /// Clear all tracking (for testing or reset)
    pub async fn clear(&self) {
        self.modified_files.write().await.clear();
    }
}

impl Default for DirtyFileTracker {
    fn default() -> Self {
        Self::new()
    }
}

// Legacy type alias for backward compatibility during migration
pub type DirtyPageTracker = DirtyFileTracker;

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_mark_modified() {
        let tracker = DirtyFileTracker::new();

        tracker.mark_modified("/test.txt").await;
        tracker.mark_modified("/test2.txt").await;

        let files = tracker.get_modified_files().await;
        assert_eq!(files.len(), 2);
        assert!(tracker.is_modified("/test.txt").await);
        assert!(tracker.is_modified("/test2.txt").await);
    }

    #[tokio::test]
    async fn test_remove() {
        let tracker = DirtyFileTracker::new();

        tracker.mark_modified("/test.txt").await;
        assert!(tracker.is_modified("/test.txt").await);

        tracker.remove("/test.txt").await;
        assert!(!tracker.is_modified("/test.txt").await);
    }

    #[tokio::test]
    async fn test_count() {
        let tracker = DirtyFileTracker::new();

        assert_eq!(tracker.count().await, 0);

        tracker.mark_modified("/file1.txt").await;
        tracker.mark_modified("/file2.txt").await;
        tracker.mark_modified("/file3.txt").await;

        assert_eq!(tracker.count().await, 3);
    }

    #[tokio::test]
    async fn test_clear() {
        let tracker = DirtyFileTracker::new();

        tracker.mark_modified("/test.txt").await;
        assert_eq!(tracker.count().await, 1);

        tracker.clear().await;
        assert_eq!(tracker.count().await, 0);
    }

    #[tokio::test]
    async fn test_duplicate_mark() {
        let tracker = DirtyFileTracker::new();

        // Marking same file multiple times should not duplicate
        tracker.mark_modified("/test.txt").await;
        tracker.mark_modified("/test.txt").await;
        tracker.mark_modified("/test.txt").await;

        assert_eq!(tracker.count().await, 1);
    }
}
