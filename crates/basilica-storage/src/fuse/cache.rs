//! In-memory page cache for FUSE filesystem
//!
//! Provides fast read/write access while data syncs to object storage in background.
//!
//! ## Sync Safety
//!
//! Pages use a tri-state model to prevent data loss during sync:
//! - `Clean`: Data matches object storage, safe to evict
//! - `Dirty`: Local modifications not yet synced, cannot evict
//! - `Syncing`: Upload in progress, cannot evict
//!
//! Only `Clean` pages can be evicted by LRU, preventing the race condition
//! where dirty data could be lost if evicted before sync completes.

use bytes::Bytes;
use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::RwLock;

/// Size of a cache page (64KB - good balance for large files)
pub const PAGE_SIZE: usize = 64 * 1024;

/// Sync state of a cached page
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PageState {
    /// Page matches object storage (or loaded from storage)
    /// CAN be evicted by LRU
    Clean,

    /// Page has local modifications not yet synced
    /// CANNOT be evicted - data would be lost
    Dirty,

    /// Page is currently being uploaded to storage
    /// CANNOT be evicted - sync in progress
    Syncing,
}

/// A single page of cached data
#[derive(Debug, Clone)]
pub struct Page {
    /// Page data (always PAGE_SIZE bytes, zero-padded if needed)
    pub data: Bytes,
    /// Sync state - determines if page can be evicted
    pub state: PageState,
    /// Last access timestamp (for LRU eviction of Clean pages)
    pub last_access: Instant,
    /// Last modification timestamp (for quiet period detection)
    pub last_modified: Instant,
}

/// Information about a dirty file ready for sync
#[derive(Debug, Clone)]
pub struct DirtyFile {
    /// File path
    pub path: String,
    /// Current file size
    pub size: u64,
}

/// Snapshot of file data for upload
#[derive(Debug)]
pub struct FileSnapshot {
    /// File path
    pub path: String,
    /// Complete file data
    pub data: Bytes,
    /// Page offsets that were marked as Syncing
    pub syncing_offsets: Vec<u64>,
}

/// Cache entry for a single file
#[derive(Debug)]
pub struct FileCache {
    /// File path
    pub path: String,
    /// File size (may be larger than cached data)
    pub size: u64,
    /// Cached pages, keyed by page offset
    pub pages: BTreeMap<u64, Page>,
    /// File metadata
    pub metadata: FileMetadata,
}

/// File metadata
#[derive(Debug, Clone)]
pub struct FileMetadata {
    /// File mode (permissions)
    pub mode: u32,
    /// Last modified time
    pub mtime: std::time::SystemTime,
    /// Creation time
    pub ctime: std::time::SystemTime,
}

impl Default for FileMetadata {
    fn default() -> Self {
        let now = std::time::SystemTime::now();
        Self {
            mode: 0o644, // rw-r--r--
            mtime: now,
            ctime: now,
        }
    }
}

/// In-memory page cache for fast I/O
pub struct PageCache {
    /// All cached files
    files: HashMap<String, Arc<RwLock<FileCache>>>,
    /// Maximum cache size in bytes
    max_size: usize,
    /// Current cache size in bytes
    current_size: usize,
}

impl PageCache {
    /// Create a new page cache with the given maximum size
    pub fn new(max_size_mb: usize) -> Self {
        Self {
            files: HashMap::new(),
            max_size: max_size_mb * 1024 * 1024,
            current_size: 0,
        }
    }

    /// Get or create a file cache entry
    pub fn get_or_create_file(&mut self, path: &str) -> Arc<RwLock<FileCache>> {
        self.files
            .entry(path.to_string())
            .or_insert_with(|| {
                Arc::new(RwLock::new(FileCache {
                    path: path.to_string(),
                    size: 0,
                    pages: BTreeMap::new(),
                    metadata: FileMetadata::default(),
                }))
            })
            .clone()
    }

    /// Read data from cache
    pub async fn read(&self, path: &str, offset: u64, size: usize) -> Option<Bytes> {
        let file = self.files.get(path)?;
        let file = file.read().await;

        // Calculate which pages we need
        let start_page = offset / PAGE_SIZE as u64;
        let end_page = (offset + size as u64).div_ceil(PAGE_SIZE as u64);

        let mut result = Vec::new();
        let mut current_offset = offset;

        for page_idx in start_page..end_page {
            let page_offset = page_idx * PAGE_SIZE as u64;

            if let Some(page) = file.pages.get(&page_offset) {
                // Calculate slice within this page
                let page_start = if current_offset > page_offset {
                    (current_offset - page_offset) as usize
                } else {
                    0
                };

                let remaining = size - result.len();
                let page_end = (page_start + remaining).min(page.data.len());

                result.extend_from_slice(&page.data[page_start..page_end]);
                current_offset = page_offset + page_end as u64;

                if result.len() >= size {
                    break;
                }
            } else {
                // Page not in cache
                return None;
            }
        }

        if result.len() == size {
            Some(Bytes::from(result))
        } else {
            None
        }
    }

    /// Write data to cache, marking affected pages as Dirty
    pub async fn write(&mut self, path: &str, offset: u64, data: &[u8]) -> Result<(), String> {
        // Ensure we don't exceed cache size
        if self.current_size + data.len() > self.max_size {
            self.evict_pages(data.len()).await;
        }

        let file = self.get_or_create_file(path);
        let mut file = file.write().await;

        // Calculate which pages we need to write
        let start_page = offset / PAGE_SIZE as u64;
        let end_page = (offset + data.len() as u64).div_ceil(PAGE_SIZE as u64);

        let mut data_offset = 0;
        let now = Instant::now();

        for page_idx in start_page..end_page {
            let page_offset = page_idx * PAGE_SIZE as u64;

            // Calculate slice within this page
            let page_start = if offset > page_offset {
                (offset - page_offset) as usize
            } else {
                0
            };

            let remaining = data.len() - data_offset;
            let page_end = (page_start + remaining).min(PAGE_SIZE);
            let chunk_size = page_end - page_start;

            // Check if page already exists to track size correctly
            let page_exists = file.pages.contains_key(&page_offset);

            // Get or create page
            let page = file.pages.entry(page_offset).or_insert_with(|| {
                let new_page = Page {
                    data: Bytes::from(vec![0u8; PAGE_SIZE]),
                    state: PageState::Clean,
                    last_access: now,
                    last_modified: now,
                };
                if !page_exists {
                    self.current_size += PAGE_SIZE;
                }
                new_page
            });

            // Pages are guaranteed to be PAGE_SIZE by insert_page() and or_insert_with()
            let mut page_data = page.data.to_vec();
            page_data[page_start..page_end]
                .copy_from_slice(&data[data_offset..data_offset + chunk_size]);

            page.data = Bytes::from(page_data);
            // Always mark as Dirty after write (even if was Syncing - will need re-sync)
            page.state = PageState::Dirty;
            page.last_access = now;
            page.last_modified = now;

            data_offset += chunk_size;
        }

        // Update file size
        file.size = file.size.max(offset + data.len() as u64);
        file.metadata.mtime = std::time::SystemTime::now();

        Ok(())
    }

    /// Insert a page from object storage (for lazy loading)
    /// Always pads to PAGE_SIZE to ensure write() can safely index
    /// Pages loaded from storage are marked Clean (safe to evict)
    pub async fn insert_page(&mut self, path: &str, offset: u64, data: Bytes) {
        let file = self.get_or_create_file(path);
        let mut file = file.write().await;

        let page_offset = (offset / PAGE_SIZE as u64) * PAGE_SIZE as u64;

        // Always pad to PAGE_SIZE so write() can safely assume page.data.len() >= PAGE_SIZE
        let page_data = if data.len() < PAGE_SIZE {
            let mut buf = data.to_vec();
            buf.resize(PAGE_SIZE, 0);
            Bytes::from(buf)
        } else {
            data
        };

        let now = Instant::now();
        if let Some(old_page) = file.pages.insert(
            page_offset,
            Page {
                data: page_data,
                state: PageState::Clean, // Loaded from storage, safe to evict
                last_access: now,
                last_modified: now,
            },
        ) {
            self.current_size = self.current_size.saturating_sub(old_page.data.len());
        }

        self.current_size += PAGE_SIZE;
    }

    /// Get file metadata
    pub async fn get_metadata(&self, path: &str) -> Option<FileMetadata> {
        let file = self.files.get(path)?;
        let file = file.read().await;
        Some(file.metadata.clone())
    }

    /// Get file size
    pub async fn get_size(&self, path: &str) -> Option<u64> {
        let file = self.files.get(path)?;
        let file = file.read().await;
        Some(file.size)
    }

    /// List all files in cache
    pub fn list_files(&self) -> Vec<String> {
        self.files.keys().cloned().collect()
    }

    /// Truncate a file to the specified size
    pub async fn truncate(&mut self, path: &str, new_size: u64) -> Result<(), String> {
        let file = self.files.get(path).ok_or("File not found")?;
        let mut file = file.write().await;

        let old_size = file.size;
        file.size = new_size;
        file.metadata.mtime = std::time::SystemTime::now();

        if new_size < old_size {
            let new_last_page = new_size / PAGE_SIZE as u64;
            let pages_to_remove: Vec<u64> = file
                .pages
                .keys()
                .filter(|&&offset| offset / PAGE_SIZE as u64 > new_last_page)
                .copied()
                .collect();

            for offset in pages_to_remove {
                if let Some(page) = file.pages.remove(&offset) {
                    self.current_size = self.current_size.saturating_sub(page.data.len());
                }
            }

            if !new_size.is_multiple_of(PAGE_SIZE as u64) {
                let last_page_offset = new_last_page * PAGE_SIZE as u64;
                if let Some(page) = file.pages.get_mut(&last_page_offset) {
                    let new_page_size = (new_size % PAGE_SIZE as u64) as usize;
                    let mut page_data = page.data.to_vec();
                    page_data.truncate(new_page_size);
                    page_data.resize(PAGE_SIZE, 0);
                    page.data = Bytes::from(page_data);
                    page.state = PageState::Dirty;
                    page.last_modified = Instant::now();
                }
            }
        }

        Ok(())
    }

    /// Remove a file from cache
    pub async fn remove_file(&mut self, path: &str) -> Option<()> {
        if let Some(file_lock) = self.files.remove(path) {
            let file = file_lock.read().await;
            for page in file.pages.values() {
                self.current_size = self.current_size.saturating_sub(page.data.len());
            }
            Some(())
        } else {
            None
        }
    }

    /// Rename a file in cache (updates internal path references)
    pub async fn rename(&mut self, old_path: &str, new_path: &str) -> Option<()> {
        let file_lock = self.files.remove(old_path)?;

        {
            let mut file = file_lock.write().await;
            file.path = new_path.to_string();
            file.metadata.mtime = std::time::SystemTime::now();
        }

        self.files.insert(new_path.to_string(), file_lock);
        Some(())
    }

    /// Get list of files with dirty pages that are ready to sync
    ///
    /// A file is ready for sync when:
    /// 1. It has at least one Dirty page
    /// 2. No page has been modified within the quiet_period
    ///
    /// The quiet period prevents syncing files that are actively being written to,
    /// allowing writes to coalesce into fewer, larger uploads.
    pub async fn get_dirty_files(&self, quiet_period: Duration) -> Vec<DirtyFile> {
        let now = Instant::now();
        let mut dirty_files = Vec::new();

        for (path, file_lock) in &self.files {
            let file = file_lock.read().await;

            let mut has_dirty = false;
            let mut latest_modification = Instant::now() - Duration::from_secs(3600);

            for page in file.pages.values() {
                if page.state == PageState::Dirty {
                    has_dirty = true;
                    if page.last_modified > latest_modification {
                        latest_modification = page.last_modified;
                    }
                }
            }

            if has_dirty {
                let time_since_last_write = now.saturating_duration_since(latest_modification);
                if time_since_last_write >= quiet_period {
                    dirty_files.push(DirtyFile {
                        path: path.clone(),
                        size: file.size,
                    });
                }
            }
        }

        dirty_files
    }

    /// Mark all dirty pages in a file as Syncing and return file snapshot
    ///
    /// This atomically transitions Dirty -> Syncing for all dirty pages
    /// and returns the complete file data for upload.
    ///
    /// Returns None if file not found in cache.
    pub async fn mark_file_syncing(&self, path: &str) -> Option<FileSnapshot> {
        let file_lock = self.files.get(path)?;
        let mut file = file_lock.write().await;

        // Build complete file data from all pages
        let file_size = file.size as usize;
        let mut data = vec![0u8; file_size];
        let mut syncing_offsets = Vec::new();

        for (offset, page) in &mut file.pages {
            // Copy page data into file buffer
            let start = *offset as usize;
            let end = (start + page.data.len()).min(file_size);
            if start < file_size {
                data[start..end].copy_from_slice(&page.data[..end - start]);
            }

            // Mark dirty pages as syncing
            if page.state == PageState::Dirty {
                page.state = PageState::Syncing;
                syncing_offsets.push(*offset);
            }
        }

        Some(FileSnapshot {
            path: path.to_string(),
            data: Bytes::from(data),
            syncing_offsets,
        })
    }

    /// Mark sync as complete for specified pages
    ///
    /// On success: Syncing -> Clean (page can now be evicted)
    /// On failure: Syncing -> Dirty (will retry on next sync cycle)
    ///
    /// If a page is Dirty (write occurred during sync), it stays Dirty.
    pub async fn mark_sync_complete(&self, path: &str, offsets: &[u64], success: bool) {
        let Some(file_lock) = self.files.get(path) else {
            return;
        };
        let mut file = file_lock.write().await;

        for offset in offsets {
            if let Some(page) = file.pages.get_mut(offset) {
                match page.state {
                    PageState::Syncing => {
                        // Normal case: transition based on success/failure
                        page.state = if success {
                            PageState::Clean
                        } else {
                            PageState::Dirty
                        };
                    }
                    PageState::Dirty => {
                        // Write occurred during sync - leave as Dirty for re-sync
                    }
                    PageState::Clean => {
                        // Should not happen, but safe to ignore
                    }
                }
            }
        }
    }

    /// Check if any file has dirty pages (for shutdown coordination)
    pub async fn has_dirty_pages(&self) -> bool {
        for file_lock in self.files.values() {
            let file = file_lock.read().await;
            for page in file.pages.values() {
                if page.state == PageState::Dirty || page.state == PageState::Syncing {
                    return true;
                }
            }
        }
        false
    }

    /// Get count of dirty pages across all files (for metrics)
    pub async fn dirty_page_count(&self) -> usize {
        let mut count = 0;
        for file_lock in self.files.values() {
            let file = file_lock.read().await;
            for page in file.pages.values() {
                if page.state == PageState::Dirty {
                    count += 1;
                }
            }
        }
        count
    }

    /// Evict least recently used CLEAN pages to make room
    ///
    /// Only pages with state == Clean can be evicted:
    /// - Dirty pages have local modifications that would be lost
    /// - Syncing pages are being uploaded and cannot be removed
    async fn evict_pages(&mut self, needed: usize) {
        let mut pages_to_evict = Vec::new();

        for (path, file_lock) in &self.files {
            let file = file_lock.read().await;
            for (offset, page) in &file.pages {
                // CRITICAL: Only evict Clean pages to prevent data loss
                if page.state == PageState::Clean {
                    pages_to_evict.push((path.clone(), *offset, page.last_access));
                }
            }
        }

        // Sort by last access time (oldest first for LRU)
        pages_to_evict.sort_by_key(|(_, _, last_access)| *last_access);

        // Evict oldest clean pages until we have enough space
        let mut freed = 0;
        for (path, offset, _) in pages_to_evict {
            if freed >= needed {
                break;
            }

            if let Some(file_lock) = self.files.get(&path) {
                let mut file = file_lock.write().await;
                // Double-check state before eviction (could have changed)
                if let Some(page) = file.pages.get(&offset) {
                    if page.state == PageState::Clean {
                        if let Some(removed) = file.pages.remove(&offset) {
                            freed += removed.data.len();
                            self.current_size = self.current_size.saturating_sub(removed.data.len());
                        }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_cache_write_read() {
        let mut cache = PageCache::new(10); // 10MB cache

        let data = b"Hello, World!";
        cache.write("/test.txt", 0, data).await.unwrap();

        let result = cache.read("/test.txt", 0, data.len()).await.unwrap();
        assert_eq!(&result[..], data);
    }

    #[tokio::test]
    async fn test_cache_large_file() {
        let mut cache = PageCache::new(10);

        // Write 128KB across multiple pages
        let data = vec![42u8; 128 * 1024];
        cache.write("/large.bin", 0, &data).await.unwrap();

        // Read it back
        let result = cache.read("/large.bin", 0, data.len()).await.unwrap();
        assert_eq!(result.len(), data.len());
        assert_eq!(&result[..], &data[..]);
    }

    #[tokio::test]
    async fn test_cache_offset_write() {
        let mut cache = PageCache::new(10);

        // Write at offset
        let data1 = b"Hello";
        let data2 = b"World";

        cache.write("/test.txt", 0, data1).await.unwrap();
        cache.write("/test.txt", 6, data2).await.unwrap();

        // Read entire file
        let result = cache.read("/test.txt", 0, 11).await.unwrap();
        assert_eq!(&result[..5], b"Hello");
        assert_eq!(&result[6..11], b"World");
    }

    #[tokio::test]
    async fn test_write_to_short_page() {
        let mut cache = PageCache::new(10);

        // Insert a short page (simulating what happens when loading from object storage)
        let short_data = Bytes::from(vec![1u8; 1024]); // 1KB page, much less than PAGE_SIZE
        cache.insert_page("/test.txt", 0, short_data.clone()).await;

        // Now try to write at an offset within this page but beyond the short data
        let write_data = b"test";
        cache.write("/test.txt", 1020, write_data).await.unwrap(); // Write near the end

        // Verify the write succeeded and data is correct
        let result = cache.read("/test.txt", 1020, 4).await.unwrap();
        assert_eq!(&result[..], write_data);

        // Also verify writing past the original short page size
        cache.write("/test.txt", 2000, write_data).await.unwrap();
        let result = cache.read("/test.txt", 2000, 4).await.unwrap();
        assert_eq!(&result[..], write_data);
    }

    #[tokio::test]
    async fn test_page_state_after_write() {
        let mut cache = PageCache::new(10);

        cache.write("/test.txt", 0, b"hello").await.unwrap();

        // Verify page is marked Dirty after write
        let file = cache.files.get("/test.txt").unwrap();
        let file = file.read().await;
        let page = file.pages.get(&0).unwrap();
        assert_eq!(page.state, PageState::Dirty);
    }

    #[tokio::test]
    async fn test_page_state_after_insert() {
        let mut cache = PageCache::new(10);

        // Pages loaded from storage should be Clean
        cache
            .insert_page("/test.txt", 0, Bytes::from(vec![0u8; 1024]))
            .await;

        let file = cache.files.get("/test.txt").unwrap();
        let file = file.read().await;
        let page = file.pages.get(&0).unwrap();
        assert_eq!(page.state, PageState::Clean);
    }

    #[tokio::test]
    async fn test_get_dirty_files_with_quiet_period() {
        let mut cache = PageCache::new(10);

        // Write to create dirty page
        cache.write("/test.txt", 0, b"hello").await.unwrap();

        // Immediately after write, should not be ready (quiet period not elapsed)
        let dirty = cache.get_dirty_files(Duration::from_millis(100)).await;
        assert!(dirty.is_empty());

        // Wait for quiet period
        tokio::time::sleep(Duration::from_millis(150)).await;

        // Now should be ready
        let dirty = cache.get_dirty_files(Duration::from_millis(100)).await;
        assert_eq!(dirty.len(), 1);
        assert_eq!(dirty[0].path, "/test.txt");
    }

    #[tokio::test]
    async fn test_get_dirty_files_zero_quiet_period() {
        let mut cache = PageCache::new(10);

        cache.write("/test.txt", 0, b"hello").await.unwrap();

        // With zero quiet period, should be immediately ready
        let dirty = cache.get_dirty_files(Duration::ZERO).await;
        assert_eq!(dirty.len(), 1);
    }

    #[tokio::test]
    async fn test_mark_file_syncing() {
        let mut cache = PageCache::new(10);

        cache.write("/test.txt", 0, b"hello world").await.unwrap();

        // Mark as syncing and get snapshot
        let snapshot = cache.mark_file_syncing("/test.txt").await.unwrap();
        assert_eq!(snapshot.path, "/test.txt");
        assert_eq!(&snapshot.data[..11], b"hello world");
        assert!(!snapshot.syncing_offsets.is_empty());

        // Verify page state changed to Syncing
        let file = cache.files.get("/test.txt").unwrap();
        let file = file.read().await;
        let page = file.pages.get(&0).unwrap();
        assert_eq!(page.state, PageState::Syncing);
    }

    #[tokio::test]
    async fn test_mark_sync_complete_success() {
        let mut cache = PageCache::new(10);

        cache.write("/test.txt", 0, b"hello").await.unwrap();
        let snapshot = cache.mark_file_syncing("/test.txt").await.unwrap();

        // Mark sync as successful
        cache
            .mark_sync_complete("/test.txt", &snapshot.syncing_offsets, true)
            .await;

        // Verify page is now Clean
        let file = cache.files.get("/test.txt").unwrap();
        let file = file.read().await;
        let page = file.pages.get(&0).unwrap();
        assert_eq!(page.state, PageState::Clean);
    }

    #[tokio::test]
    async fn test_mark_sync_complete_failure() {
        let mut cache = PageCache::new(10);

        cache.write("/test.txt", 0, b"hello").await.unwrap();
        let snapshot = cache.mark_file_syncing("/test.txt").await.unwrap();

        // Mark sync as failed
        cache
            .mark_sync_complete("/test.txt", &snapshot.syncing_offsets, false)
            .await;

        // Verify page is back to Dirty for retry
        let file = cache.files.get("/test.txt").unwrap();
        let file = file.read().await;
        let page = file.pages.get(&0).unwrap();
        assert_eq!(page.state, PageState::Dirty);
    }

    #[tokio::test]
    async fn test_write_during_sync_keeps_dirty() {
        let mut cache = PageCache::new(10);

        cache.write("/test.txt", 0, b"hello").await.unwrap();
        let snapshot = cache.mark_file_syncing("/test.txt").await.unwrap();

        // Write during sync (simulates concurrent write)
        cache.write("/test.txt", 0, b"world").await.unwrap();

        // Verify page is Dirty (not Syncing)
        {
            let file = cache.files.get("/test.txt").unwrap();
            let file = file.read().await;
            let page = file.pages.get(&0).unwrap();
            assert_eq!(page.state, PageState::Dirty);
        }

        // Mark sync complete - should stay Dirty because of concurrent write
        cache
            .mark_sync_complete("/test.txt", &snapshot.syncing_offsets, true)
            .await;

        let file = cache.files.get("/test.txt").unwrap();
        let file = file.read().await;
        let page = file.pages.get(&0).unwrap();
        assert_eq!(page.state, PageState::Dirty);
    }

    #[tokio::test]
    async fn test_eviction_only_clean_pages() {
        // Small cache to trigger eviction
        let mut cache = PageCache::new(1); // 1MB cache

        // Write dirty data
        let dirty_data = vec![1u8; PAGE_SIZE];
        cache.write("/dirty.txt", 0, &dirty_data).await.unwrap();

        // Insert clean data
        let clean_data = Bytes::from(vec![2u8; PAGE_SIZE]);
        cache.insert_page("/clean.txt", 0, clean_data).await;

        // Force eviction by writing more data
        let more_data = vec![3u8; PAGE_SIZE * 20]; // Force eviction
        cache.write("/large.txt", 0, &more_data).await.unwrap();

        // Dirty page should still exist
        let dirty_file = cache.files.get("/dirty.txt").unwrap();
        let dirty_file = dirty_file.read().await;
        assert!(
            dirty_file.pages.contains_key(&0),
            "Dirty page should not be evicted"
        );
    }

    #[tokio::test]
    async fn test_has_dirty_pages() {
        let mut cache = PageCache::new(10);

        // No dirty pages initially
        assert!(!cache.has_dirty_pages().await);

        // After write, should have dirty pages
        cache.write("/test.txt", 0, b"hello").await.unwrap();
        assert!(cache.has_dirty_pages().await);

        // After sync complete, should have no dirty pages
        let snapshot = cache.mark_file_syncing("/test.txt").await.unwrap();
        cache
            .mark_sync_complete("/test.txt", &snapshot.syncing_offsets, true)
            .await;
        assert!(!cache.has_dirty_pages().await);
    }

    #[tokio::test]
    async fn test_dirty_page_count() {
        let mut cache = PageCache::new(10);

        assert_eq!(cache.dirty_page_count().await, 0);

        // Write to create one dirty page
        cache.write("/test.txt", 0, b"hello").await.unwrap();
        assert_eq!(cache.dirty_page_count().await, 1);

        // Write to another file
        cache.write("/test2.txt", 0, b"world").await.unwrap();
        assert_eq!(cache.dirty_page_count().await, 2);

        // Sync one file
        let snapshot = cache.mark_file_syncing("/test.txt").await.unwrap();
        cache
            .mark_sync_complete("/test.txt", &snapshot.syncing_offsets, true)
            .await;
        assert_eq!(cache.dirty_page_count().await, 1);
    }
}
