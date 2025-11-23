//! FUSE filesystem implementation for Basilica storage
//!
//! Provides transparent file I/O backed by object storage with in-memory caching.

use super::cache::{FileMetadata, PageCache, PAGE_SIZE};
use super::dirty_tracker::DirtyPageTracker;
use super::sync_worker::SyncWorker;
use crate::backend::StorageBackend;
use fuser::{
    FileAttr, FileType, Filesystem, ReplyAttr, ReplyCreate, ReplyData, ReplyDirectory, ReplyEntry,
    ReplyWrite, Request,
};
use libc::ENOENT;
use std::collections::HashMap;
use std::ffi::OsStr;
use std::sync::Arc;
use std::time::{Duration, SystemTime};
use tokio::runtime::Handle;
use tokio::sync::RwLock;
use tracing::{debug, error, info};

const TTL: Duration = Duration::from_secs(1);
const ROOT_INO: u64 = 1;

/// Inode information
#[derive(Debug, Clone)]
struct Inode {
    ino: u64,
    parent: u64,
    name: String,
    kind: FileType,
    size: u64,
    metadata: FileMetadata,
}

impl Inode {
    fn to_file_attr(&self) -> FileAttr {
        FileAttr {
            ino: self.ino,
            size: self.size,
            blocks: self.size.div_ceil(512),
            atime: self.metadata.mtime,
            mtime: self.metadata.mtime,
            ctime: self.metadata.ctime,
            crtime: self.metadata.ctime,
            kind: self.kind,
            perm: self.metadata.mode as u16,
            nlink: 1,
            uid: 1000,
            gid: 1000,
            rdev: 0,
            blksize: PAGE_SIZE as u32,
            flags: 0,
        }
    }
}

/// Basilica FUSE filesystem
pub struct BasilicaFS {
    /// Experiment ID (used as prefix in object storage)
    experiment_id: String,

    /// Object storage backend
    storage: Arc<dyn StorageBackend>,

    /// In-memory page cache
    cache: Arc<RwLock<PageCache>>,

    /// Dirty page tracker
    dirty_tracker: Arc<DirtyPageTracker>,

    /// Background sync worker
    sync_worker: Arc<SyncWorker>,

    /// Tokio runtime handle for async operations
    runtime_handle: Handle,

    /// Inode table
    inodes: Arc<RwLock<HashMap<u64, Inode>>>,

    /// Path to inode mapping
    path_to_ino: Arc<RwLock<HashMap<String, u64>>>,

    /// Next available inode number
    next_ino: Arc<RwLock<u64>>,

    /// Open file handles
    fh_counter: Arc<RwLock<u64>>,
}

impl BasilicaFS {
    /// Create a new Basilica filesystem
    pub fn new(
        experiment_id: String,
        storage: Arc<dyn StorageBackend>,
        sync_interval_ms: u64,
        cache_size_mb: usize,
    ) -> Self {
        let cache = Arc::new(RwLock::new(PageCache::new(cache_size_mb)));
        let dirty_tracker = Arc::new(DirtyPageTracker::new());

        let sync_worker = Arc::new(SyncWorker::new(
            experiment_id.clone(),
            storage.clone(),
            cache.clone(),
            dirty_tracker.clone(),
            sync_interval_ms,
        ));

        // Get handle to current runtime, or try to create one if not in async context
        let runtime_handle = Handle::try_current().unwrap_or_else(|_| {
            // If not in async context, create a new runtime and leak it
            // This is needed for FUSE which runs in a blocking context
            let runtime = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .expect("Failed to create tokio runtime");
            let handle = runtime.handle().clone();
            // Leak the runtime so it doesn't get dropped
            std::mem::forget(runtime);
            handle
        });

        let mut inodes = HashMap::new();
        let now = SystemTime::now();

        // Create root directory
        inodes.insert(
            ROOT_INO,
            Inode {
                ino: ROOT_INO,
                parent: ROOT_INO,
                name: "/".to_string(),
                kind: FileType::Directory,
                size: 0,
                metadata: FileMetadata {
                    mode: 0o755,
                    mtime: now,
                    ctime: now,
                },
            },
        );

        let mut path_to_ino = HashMap::new();
        path_to_ino.insert("/".to_string(), ROOT_INO);

        Self {
            experiment_id,
            storage,
            cache,
            dirty_tracker,
            sync_worker,
            runtime_handle,
            inodes: Arc::new(RwLock::new(inodes)),
            path_to_ino: Arc::new(RwLock::new(path_to_ino)),
            next_ino: Arc::new(RwLock::new(ROOT_INO + 1)),
            fh_counter: Arc::new(RwLock::new(1)),
        }
    }

    /// Start the background sync worker
    pub async fn start_sync_worker(&self) {
        self.sync_worker.start().await;
    }

    /// Stop the sync worker and flush all dirty pages
    pub async fn shutdown(&self) -> Result<(), String> {
        info!("Shutting down BasilicaFS");
        self.sync_worker.stop().await;
        self.sync_worker.flush_all().await
    }

    /// Get full path for an inode
    async fn get_path(&self, ino: u64) -> Option<String> {
        let inodes = self.inodes.read().await;
        let mut path_parts = Vec::new();
        let mut current = ino;

        while current != ROOT_INO {
            let inode = inodes.get(&current)?;
            path_parts.push(inode.name.clone());
            current = inode.parent;
        }

        if path_parts.is_empty() {
            Some("/".to_string())
        } else {
            path_parts.reverse();
            Some(format!("/{}", path_parts.join("/")))
        }
    }

    /// Allocate a new inode number
    async fn alloc_ino(&self) -> u64 {
        let mut next = self.next_ino.write().await;
        let ino = *next;
        *next += 1;
        ino
    }

    /// Lazy load file from object storage
    async fn lazy_load_file(&self, path: &str) -> Result<(), String> {
        let key = format!("{}/{}", self.experiment_id, path.trim_start_matches('/'));

        debug!("Lazy loading file: {} from key: {}", path, key);

        // Check if file exists in storage
        match self.storage.exists(&key).await {
            Ok(true) => {
                // File exists, fetch it
                match self.storage.get(&key).await {
                    Ok(data) => {
                        let size = data.len();
                        debug!("Loaded {} bytes for {}", size, path);

                        // Insert into cache in pages
                        let mut cache = self.cache.write().await;
                        let mut offset = 0;

                        while offset < size {
                            let chunk_size = (size - offset).min(PAGE_SIZE);
                            let chunk = data.slice(offset..offset + chunk_size);
                            cache.insert_page(path, offset as u64, chunk).await;
                            offset += chunk_size;
                        }

                        Ok(())
                    }
                    Err(e) => {
                        error!("Failed to load file {}: {}", path, e);
                        Err(format!("Failed to load file: {}", e))
                    }
                }
            }
            Ok(false) => {
                debug!("File {} does not exist in storage (new file)", path);
                Ok(())
            }
            Err(e) => {
                error!("Failed to check file existence {}: {}", path, e);
                Err(format!("Storage error: {}", e))
            }
        }
    }
}

impl Filesystem for BasilicaFS {
    fn lookup(&mut self, _req: &Request, parent: u64, name: &OsStr, reply: ReplyEntry) {
        let name_str = name.to_string_lossy().to_string();
        debug!("lookup: parent={}, name={}", parent, name_str);

        self.runtime_handle.block_on(async {
            let inodes = self.inodes.read().await;

            // Find child with matching name and parent
            for inode in inodes.values() {
                if inode.parent == parent && inode.name == name_str {
                    reply.entry(&TTL, &inode.to_file_attr(), 0);
                    return;
                }
            }

            reply.error(ENOENT);
        });
    }

    fn getattr(&mut self, _req: &Request, ino: u64, reply: ReplyAttr) {
        debug!("getattr: ino={}", ino);

        self.runtime_handle.block_on(async {
            let inodes = self.inodes.read().await;

            if let Some(inode) = inodes.get(&ino) {
                reply.attr(&TTL, &inode.to_file_attr());
            } else {
                reply.error(ENOENT);
            }
        });
    }

    fn read(
        &mut self,
        _req: &Request,
        ino: u64,
        _fh: u64,
        offset: i64,
        size: u32,
        _flags: i32,
        _lock: Option<u64>,
        reply: ReplyData,
    ) {
        debug!("read: ino={}, offset={}, size={}", ino, offset, size);

        self.runtime_handle.block_on(async {
            let path = match self.get_path(ino).await {
                Some(p) => p,
                None => {
                    reply.error(ENOENT);
                    return;
                }
            };

            // Try to read from cache first
            let cache = self.cache.read().await;
            if let Some(data) = cache.read(&path, offset as u64, size as usize).await {
                drop(cache);
                reply.data(&data);
                return;
            }
            drop(cache);

            // Cache miss - lazy load from storage
            if let Err(e) = self.lazy_load_file(&path).await {
                error!("Failed to lazy load {}: {}", path, e);
                reply.error(libc::EIO);
                return;
            }

            // Try reading again after loading
            let cache = self.cache.read().await;
            if let Some(data) = cache.read(&path, offset as u64, size as usize).await {
                reply.data(&data);
            } else {
                reply.error(libc::EIO);
            }
        });
    }

    fn write(
        &mut self,
        _req: &Request,
        ino: u64,
        _fh: u64,
        offset: i64,
        data: &[u8],
        _write_flags: u32,
        _flags: i32,
        _lock: Option<u64>,
        reply: ReplyWrite,
    ) {
        debug!("write: ino={}, offset={}, size={}", ino, offset, data.len());

        self.runtime_handle.block_on(async {
            let path = match self.get_path(ino).await {
                Some(p) => p,
                None => {
                    reply.error(ENOENT);
                    return;
                }
            };

            // Write to cache
            let mut cache = self.cache.write().await;
            if let Err(e) = cache.write(&path, offset as u64, data).await {
                drop(cache);
                error!("Failed to write to cache: {}", e);
                reply.error(libc::EIO);
                return;
            }

            // Update inode size
            let new_size =
                (offset as u64 + data.len() as u64).max(cache.get_size(&path).await.unwrap_or(0));

            drop(cache);

            let mut inodes = self.inodes.write().await;
            if let Some(inode) = inodes.get_mut(&ino) {
                inode.size = new_size;
                inode.metadata.mtime = SystemTime::now();
            }
            drop(inodes);

            // Mark dirty for background sync
            self.dirty_tracker
                .mark_dirty(&path, offset as u64, data.len())
                .await;

            reply.written(data.len() as u32);
        });
    }

    fn create(
        &mut self,
        _req: &Request<'_>,
        parent: u64,
        name: &OsStr,
        mode: u32,
        _umask: u32,
        _flags: i32,
        reply: ReplyCreate,
    ) {
        let name_str = name.to_string_lossy().to_string();
        debug!(
            "create: parent={}, name={}, mode={:o}",
            parent, name_str, mode
        );

        self.runtime_handle.block_on(async {
            let ino = self.alloc_ino().await;
            let now = SystemTime::now();

            let inode = Inode {
                ino,
                parent,
                name: name_str.clone(),
                kind: FileType::RegularFile,
                size: 0,
                metadata: FileMetadata {
                    mode,
                    mtime: now,
                    ctime: now,
                },
            };

            let attr = inode.to_file_attr();

            let mut inodes = self.inodes.write().await;
            inodes.insert(ino, inode);
            drop(inodes);

            // Update path mapping
            let parent_path = self
                .get_path(parent)
                .await
                .unwrap_or_else(|| "/".to_string());
            let full_path = if parent_path == "/" {
                format!("/{}", name_str)
            } else {
                format!("{}/{}", parent_path, name_str)
            };

            let mut path_to_ino = self.path_to_ino.write().await;
            path_to_ino.insert(full_path, ino);
            drop(path_to_ino);

            let fh = {
                let mut fh_counter = self.fh_counter.write().await;
                let fh = *fh_counter;
                *fh_counter += 1;
                fh
            };

            reply.created(&TTL, &attr, 0, fh, 0);
        });
    }

    fn mkdir(
        &mut self,
        _req: &Request<'_>,
        parent: u64,
        name: &OsStr,
        mode: u32,
        _umask: u32,
        reply: ReplyEntry,
    ) {
        let name_str = name.to_string_lossy().to_string();
        debug!(
            "mkdir: parent={}, name={}, mode={:o}",
            parent, name_str, mode
        );

        self.runtime_handle.block_on(async {
            let ino = self.alloc_ino().await;
            let now = SystemTime::now();

            let inode = Inode {
                ino,
                parent,
                name: name_str.clone(),
                kind: FileType::Directory,
                size: 0,
                metadata: FileMetadata {
                    mode: mode | 0o040000, // Directory bit
                    mtime: now,
                    ctime: now,
                },
            };

            let attr = inode.to_file_attr();

            let mut inodes = self.inodes.write().await;
            inodes.insert(ino, inode);
            drop(inodes);

            // Update path mapping
            let parent_path = self
                .get_path(parent)
                .await
                .unwrap_or_else(|| "/".to_string());
            let full_path = if parent_path == "/" {
                format!("/{}", name_str)
            } else {
                format!("{}/{}", parent_path, name_str)
            };

            let mut path_to_ino = self.path_to_ino.write().await;
            path_to_ino.insert(full_path, ino);
            drop(path_to_ino);

            reply.entry(&TTL, &attr, 0);
        });
    }

    fn readdir(
        &mut self,
        _req: &Request,
        ino: u64,
        _fh: u64,
        offset: i64,
        mut reply: ReplyDirectory,
    ) {
        debug!("readdir: ino={}, offset={}", ino, offset);

        self.runtime_handle.block_on(async {
            let inodes = self.inodes.read().await;

            let mut entries = vec![
                (ino, FileType::Directory, ".".to_string()),
                (ino, FileType::Directory, "..".to_string()),
            ];

            // Add children
            for child in inodes.values() {
                if child.parent == ino {
                    entries.push((child.ino, child.kind, child.name.clone()));
                }
            }

            for (i, entry) in entries.iter().enumerate().skip(offset as usize) {
                if reply.add(entry.0, (i + 1) as i64, entry.1, &entry.2) {
                    break;
                }
            }

            reply.ok();
        });
    }

    fn fsync(
        &mut self,
        _req: &Request<'_>,
        _ino: u64,
        _fh: u64,
        _datasync: bool,
        reply: fuser::ReplyEmpty,
    ) {
        debug!("fsync called, flushing dirty pages");

        self.runtime_handle.block_on(async {
            match self.sync_worker.flush_all().await {
                Ok(_) => reply.ok(),
                Err(e) => {
                    error!("fsync failed: {}", e);
                    reply.error(libc::EIO);
                }
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::StorageBackend;
    use crate::error::Result;
    use async_trait::async_trait;
    use bytes::Bytes;
    use std::collections::HashMap as StdHashMap;

    struct MockStorage {
        data: Arc<tokio::sync::RwLock<StdHashMap<String, Bytes>>>,
    }

    impl MockStorage {
        fn new() -> Self {
            Self {
                data: Arc::new(tokio::sync::RwLock::new(StdHashMap::new())),
            }
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
    async fn test_filesystem_creation() {
        let storage = Arc::new(MockStorage::new());
        let fs = BasilicaFS::new("exp-test".to_string(), storage, 1000, 10);

        // Root should exist
        let inodes = fs.inodes.read().await;
        assert!(inodes.contains_key(&ROOT_INO));
        assert_eq!(inodes.get(&ROOT_INO).unwrap().kind, FileType::Directory);
    }

    #[tokio::test]
    async fn test_path_resolution() {
        let storage = Arc::new(MockStorage::new());
        let fs = BasilicaFS::new("exp-test".to_string(), storage, 1000, 10);

        // Root path
        let root_path = fs.get_path(ROOT_INO).await;
        assert_eq!(root_path, Some("/".to_string()));
    }
}
